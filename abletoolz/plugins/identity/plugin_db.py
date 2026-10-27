"""What plugins this machine has, written down once and read many times.

The same idea as the sample database: reading the machine is slow and every
plugin command needs the same answers, so the scan happens once, lands in a
JSON file beside the sample catalog in the user config dir, and everything else
reads the file. ``--plugin-db`` builds it; ``plugin_database.paths`` in
config.yaml adds plugin folders, exactly as ``sample_database.paths`` does.

What it holds is identity, not opinion
--------------------------------------
One record per plugin per place it was found: the name a set would store it
under, its format, its vendor, the VST3 class id or the VST2 UniqueId, and the
module on disk. That is the stable half of a
machine -- a plugin's class id does not change between Live sessions -- which is
what makes a snapshot safe to keep.

Whether Live can *load* a plugin today is the unstable half: it flips when the
user toggles a plug-in folder in preferences, and a stale "yes" would make a
plan leave a device alone that is actually broken. So loadability is read from
Live's database live (see :mod:`abletoolz.plugins.loadable`) and this file
never records it.

Records, not merged rows
------------------------
A plugin found in two places gets two records, one per :class:`PluginSource`.
Keeping them apart is what lets :meth:`PluginDatabase.uid_lookup` still report
two sources claiming different class ids for one name -- a wrong class id does
not fail, it makes Live silently load a different plugin, so a disagreement has
to be shouted about rather than averaged away. :meth:`PluginDatabase.installed`
is the collapsed view, highest priority source winning.

The priority order is :class:`PluginSource`'s own order, and it is the order
measured to be trustworthy: Live's database is the broadest and the only source
carrying a vendor, a probed uid file is a deliberate measurement, and
moduleinfo.json, read out of the VST3 bundles in the plugin folders, is
whatever the vendor shipped.
"""

from __future__ import annotations

import collections
import datetime
import enum
import logging
import pathlib
from collections.abc import Iterable, Mapping, Sequence

import pydantic

from abletoolz.machine import Locations
from abletoolz.plugins.config import AbletoolzConfig
from abletoolz.plugins.identity.ids import PluginKind, UidFields
from abletoolz.plugins.identity.uid_sources import (
    DatabasePlugin,
    UidLookup,
    UidSource,
    harvest_moduleinfo_uids,
    read_live_databases,
    read_uid_db,
)

logger = logging.getLogger(__name__)


class PluginSource(enum.StrEnum):
    """Where one record came from. Declaration order is resolution order."""

    LIVE_DATABASE = "live-database"
    UID_DB = "uid-db"
    MODULEINFO = "moduleinfo"


class PluginEntry(pydantic.BaseModel):
    """One plugin, as one source describes it.

    ``name`` is the name a set stores, which is not always the file name --
    measured 2026-08-12, ``iZOzone9.dll`` is "Ozone 9" to Live. ``uid_fields``
    is the VST3 class id and ``unique_id`` the VST2 UniqueId; a source that
    knows neither leaves both None.
    """

    model_config = pydantic.ConfigDict(extra="forbid", frozen=True)

    name: str
    kind: PluginKind
    source: PluginSource
    vendor: str | None = None
    uid_fields: UidFields | None = None
    unique_id: int | None = None
    module_path: pathlib.Path | None = None


class SourceCount(pydantic.BaseModel):
    """How many plugins of one format one source contributed."""

    model_config = pydantic.ConfigDict(extra="forbid", frozen=True)

    kind: PluginKind
    source: PluginSource
    count: int

    def __str__(self) -> str:
        return f"{self.count} {FORMAT_LABELS[self.kind]} from {self.source}"


# What a musician calls each format, rather than the tag a set stores it under.
FORMAT_LABELS: dict[PluginKind, str] = {PluginKind.VST: "VST2", PluginKind.VST3: "VST3", PluginKind.AU: "AU"}


class PluginDatabase(pydantic.BaseModel):
    """Every plugin this machine knew when the database was built."""

    model_config = pydantic.ConfigDict(extra="forbid")

    built: datetime.datetime
    plugins: tuple[PluginEntry, ...] = ()

    def of_kind(self, kind: PluginKind) -> tuple[PluginEntry, ...]:
        """Every record of one format, in resolution order."""
        return tuple(entry for entry in self.plugins if entry.kind is kind)

    def installed(self, kind: PluginKind) -> tuple[PluginEntry, ...]:
        """One record per name of one format, the highest priority source winning."""
        found: dict[str, PluginEntry] = {}
        for entry in self.of_kind(kind):
            found.setdefault(entry.name, entry)
        return tuple(found.values())

    def counts(self) -> tuple[SourceCount, ...]:
        """How many records each format got from each source."""
        tally = collections.Counter((entry.kind, entry.source) for entry in self.plugins)
        return tuple(SourceCount(kind=kind, source=source, count=count) for (kind, source), count in tally.items())

    def uid_lookup(self, *, extra: Mapping[str, UidFields] | None = None) -> UidLookup:
        """VST3 class ids by display name, one lookup source per place they came from.

        ``extra`` is a caller's own measurement and beats every source here.
        Disagreements between sources are logged at error level, because a wrong
        class id gives Live a device that loads as another plugin entirely.
        """
        sources: list[UidSource] = []
        if extra:
            sources.append(UidSource("caller", dict(extra)))
        for source in PluginSource:
            fields = {
                entry.name: entry.uid_fields
                for entry in self.of_kind(PluginKind.VST3)
                if entry.source is source and entry.uid_fields is not None
            }
            if fields:
                sources.append(UidSource(str(source), fields))
        lookup = UidLookup(tuple(sources))
        for disagreement in lookup.disagreements():
            logger.error(
                "Class id sources disagree about %s -- using %s. A wrong class id makes Live load"
                " another plugin, so check this one by hand. %s",
                disagreement.name,
                disagreement.winner,
                disagreement,
            )
        return lookup


# -- building ---------------------------------------------------------------


def _from_live_database(plugins: Iterable[DatabasePlugin]) -> list[PluginEntry]:
    """Live's own rows, the broadest source and the only one carrying a vendor."""
    return [
        PluginEntry(
            name=plugin.name,
            kind=plugin.kind,
            source=PluginSource.LIVE_DATABASE,
            vendor=plugin.vendor or None,
            uid_fields=plugin.uid_fields,
            unique_id=plugin.unique_id,
            module_path=plugin.module_path,
        )
        for plugin in plugins
    ]


def _from_uid_fields(fields: Mapping[str, UidFields], source: PluginSource) -> list[PluginEntry]:
    """Class ids on their own -- a probed file or an installed bundle's moduleinfo."""
    return [
        PluginEntry(name=name, kind=PluginKind.VST3, source=source, uid_fields=uid_fields)
        for name, uid_fields in fields.items()
    ]


def build_plugin_db(
    *,
    database_dir: pathlib.Path | None,
    vst_dirs: Sequence[pathlib.Path],
    uid_db: pathlib.Path | None = None,
    built: datetime.datetime | None = None,
) -> PluginDatabase:
    """Read every plugin a machine knows about, from every source that knows.

    ``database_dir`` is the folder holding Live's plugin database, None where
    there is none; ``vst_dirs`` are the plugin folders whose VST3 bundles'
    moduleinfo.json is read.
    """
    entries = _from_live_database(read_live_databases(database_dir) if database_dir is not None else ())
    if uid_db is not None:
        entries.extend(_from_uid_fields(read_uid_db(uid_db), PluginSource.UID_DB))
    entries.extend(_from_uid_fields(harvest_moduleinfo_uids(vst_dirs), PluginSource.MODULEINFO))

    deduplicated: dict[tuple[PluginSource, PluginKind, str], PluginEntry] = {}
    for entry in entries:
        deduplicated.setdefault((entry.source, entry.kind, entry.name), entry)
    order = list(PluginSource)
    ordered = sorted(deduplicated.values(), key=lambda entry: (order.index(entry.source), entry.kind, entry.name))
    return PluginDatabase(
        built=built if built is not None else datetime.datetime.now(tz=datetime.UTC),
        plugins=tuple(ordered),
    )


# -- the file ---------------------------------------------------------------


def write_plugin_db(database: PluginDatabase, db_path: pathlib.Path) -> pathlib.Path:
    """Write the database, creating the config dir on a machine that has none yet."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_text(database.model_dump_json(indent=2), encoding="utf-8")
    logger.info("Updated plugin database at %s", db_path.resolve())
    return db_path


def read_plugin_db(db_path: pathlib.Path) -> PluginDatabase:
    """Load the database from JSON."""
    if not db_path.exists():
        raise FileNotFoundError(f"Plugin database {db_path} doesn't exist! Run --plugin-db first.")
    return PluginDatabase.model_validate_json(db_path.read_text(encoding="utf-8"))


def create_or_update_db(
    config: AbletoolzConfig, locations: Locations, db_path: pathlib.Path | None = None
) -> PluginDatabase:
    """Read the plugins at ``locations`` and write the result down, to ``locations.plugin_db`` when no path is given.

    A rebuild replaces rather than merges: a plugin that has been uninstalled
    since the last run must leave the database, the same way the sample database
    drops sample paths that are no longer there.
    """
    database = _build(config, locations)
    write_plugin_db(database, locations.plugin_db if db_path is None else db_path)
    return database


def load_plugin_db(
    config: AbletoolzConfig, locations: Locations, db_path: pathlib.Path | None = None
) -> PluginDatabase:
    """The written database, or a freshly built one when there is no file yet or it is one this version cannot read.

    Building reads Live's database and every plugin folder, so a machine that
    has run ``--plugin-db`` once is faster -- but a command must never refuse to
    run just because nobody has built it yet, or an older abletoolz built it.
    """
    path = locations.plugin_db if db_path is None else db_path
    if not path.exists():
        logger.info("No plugin database at %s yet, reading this machine instead. Run --plugin-db to keep it.", path)
        return _build(config, locations)
    try:
        return read_plugin_db(path)
    except pydantic.ValidationError:
        logger.warning("Cannot read the plugin database at %s, reading this machine instead. Run --plugin-db.", path)
        return _build(config, locations)


def _build(config: AbletoolzConfig, locations: Locations) -> PluginDatabase:
    """Every source at ``locations``, its standard plugin folders plus whatever config adds."""
    return build_plugin_db(
        database_dir=locations.live_database,
        vst_dirs=list(locations.with_plugin_folders(config.plugin_paths).vst_dirs),
        uid_db=config.plugin_uid_db,
    )
