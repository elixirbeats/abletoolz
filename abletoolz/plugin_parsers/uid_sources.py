"""Where a VST3 class id comes from, and which source wins when they disagree.

A VST2 device names no class id, so translating one into a VST3 means finding
that id somewhere else. Four places know it, and they are consulted in this
order:

1. an explicit ``uid`` on the mapping entry -- the caller has measured it;
2. Live's own plugin database, the broadest source on a machine that runs Live;
3. a probed JSON file of class ids (config ``plugin_translation.uid_db``);
4. ``moduleinfo.json`` inside installed VST3 bundles.

This module reads each of those; the order is assembled in
:mod:`abletoolz.plugin_parsers.plugin_db`, which reads them all once into the
local plugin database and hands out a :class:`UidLookup` over it.

Order matters only where sources overlap, and overlap is where danger lives: a
wrong class id does not fail, it makes Live silently load a different plugin.
So :class:`UidLookup` also reports every name two sources disagree about, and
the caller logs each disagreement at error level.

Measured on Live 12.4.5b, ``Live-plugins-1.db`` (2026-08-12)
-----------------------------------------------------------
Schema: ``plugins(plugin_id, module_id, dev_identifier, name, vendor, version,
sdk_version, flags, scanstate, subcategories, enabled)``, ``plugin_modules(
module_id, path, arch, processor, scanstate, fingerprint)``, ``plugin_domains(
id, module_id, relpath, enabled)``, ``version(version, platform)``.

* 1300 plugin rows: 390 ``device:vst3:``, 910 ``device:vst:``.
* A VST3 ``dev_identifier`` ends in the class id as a dashed uuid --
  ``Serum`` is ``device:vst3:instr:56535458-6673-5873-6572-756d00000000``.
  Drop the dashes and :func:`~.format_translation.cid_to_uid_fields` gives the
  four Uid fields Live writes into a set. All 390 rows parse, no two VST3 rows
  share a name with different ids, and all eight entries of
  :data:`~.format_translation.KNOWN_TRANSLATIONS` -- measured independently, by
  ear -- match the database exactly.
* A VST2 ``dev_identifier`` ends in the UniqueId int plus the display name:
  ``device:vst:audiofx:1935828326?n=Effectrix``.

What the database says about loadability, and what it does not:

* ``plugins.scanstate`` is 1 for all 1300 rows and ``plugins.enabled`` is 1 for
  all 1300. Neither column carries any information here, so neither can answer
  "can Live load this today".
* ``plugins.flags`` is 1 for every VST3 row and for 53 VST2 rows, all of them
  Waves shells (Bass Rider, H-Comp, OneKnob). It marks a module that hosts
  several plugins, not a loadable one.
* ``plugin_modules.scanstate`` is 1 for 1192 modules, 2 for 3 and 3 for 39. The
  ones that are not 1 are files that failed to scan or were never plugins
  (``libmp3lame.dll``, ``lame_enc.dll``), and they contribute no ``plugins``
  rows at all, so they never reach a lookup.
* ``plugin_domains`` is the column that answers the question. It holds exactly
  one row per module: ``id`` ``'custom'`` with ``enabled`` 0 covered all 905
  ``.dll`` modules, ``id`` ``'global'`` with ``enabled`` 1 covered all 329
  ``.vst3`` modules. That is Live's VST2 custom plug-in folder switched off in
  preferences, and it is why Live 12.4.5b showed this machine's old VST2 devices
  as missing while every one of the 1234 module paths still existed on disk.

So the oracle is ``plugin_domains.enabled`` on a device's module, plus the
module path still being there. See :mod:`abletoolz.plugin_parsers.repair`.

Re-measured 2026-08-13, and the reason it is read live
------------------------------------------------------
The user switched the VST2 folder back on and Live rescanned. The same database
now says ``custom`` ``enabled`` 1 as well: 790 plugin rows over 724 modules, and
every row loadable. Two things did not move with the toggle, and both still make
devices broken:

* 41 of the 395 ``.dll`` modules have ``plugin_modules.scanstate`` 2 or 3 --
  they failed the scan -- and a failed module contributes no ``plugins`` rows at
  all, so no toggle can bring it back. That is this machine's whole iZotope VST2
  set and both Wolfram builds.
* A plugin in a folder Live is not scanning has no row of any kind. Every
  jBridge-wrapped 32-bit dll lives in one of those: zero ``.64.dll`` paths
  appear in ``plugin_modules``, so ``FabFilter Pro-C.64`` and friends are broken
  whatever the preference says.

One preference flip inverted the answer for 400 devices. Identity did not
change. That split is why this module is a file and the oracle is not.

One database, not all of them. The same folder also holds ``Live-files-53.db``,
the combined files-and-plugins database Live 11 wrote, whose ``version`` row
says 53 against the newer file's 1. It carries 709 plugin rows of its own and it
answers the loadability question the opposite way -- ``custom`` enabled, because
VST2 still worked under that Live. Reading every ``*.db`` in the folder and
merging would answer for the Live the user stopped running, so
:func:`read_live_databases` takes the newest file that has the tables and stops.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import pathlib
import re
import sqlite3
from collections.abc import Iterable, Mapping
from typing import cast

import pydantic

from abletoolz.plugin_parsers.base import PluginKind
from abletoolz.plugin_parsers.format_translation import UidFields, cid_to_uid_fields

logger = logging.getLogger(__name__)


# -- Live's plugin database -------------------------------------------------

# One row per plugin the database knows, carrying everything the uid harvest
# and the loadability oracle need. plugin_domains holds exactly one row per
# module, so this join neither drops nor duplicates a plugin.
_DATABASE_QUERY = (
    "SELECT p.name, p.vendor, p.dev_identifier, pm.path, pm.scanstate, pd.id, pd.enabled"
    " FROM plugins p"
    " JOIN plugin_modules pm ON p.module_id = pm.module_id"
    " JOIN plugin_domains pd ON pm.module_id = pd.module_id"
)

# device:vst3:instr:<dashed uuid> or device:vst:audiofx:<UniqueId>?n=<name>.
_DEV_IDENTIFIER = re.compile(r"^device:(?P<format>vst3|vst):(?P<category>[^:]+):(?P<body>.+)$")

_KIND_BY_DEV_FORMAT: dict[str, PluginKind] = {"vst": PluginKind.VST, "vst3": PluginKind.VST3}


@dataclasses.dataclass(frozen=True)
class DatabasePlugin:
    """One plugin as Live's database describes it.

    ``uid_fields`` is set for VST3 rows and ``unique_id`` for VST2 rows, each
    read out of ``dev_identifier``; the other is None. ``vendor`` is populated
    for both formats -- measured 2026-08-12, not one blank row of either -- which
    makes it the only thing that can tell two same-named plugins apart.
    """

    name: str
    kind: PluginKind
    vendor: str
    uid_fields: UidFields | None
    unique_id: int | None
    module_path: pathlib.Path
    domain: str
    domain_enabled: bool
    module_scanstate: int

    @property
    def loadable(self) -> bool:
        """Whether Live could put this plugin in a set right now.

        Its plug-in folder has to be switched on and its module still on disk.
        """
        return self.domain_enabled and self.module_path.exists()


def parse_dev_identifier(dev_identifier: str) -> tuple[PluginKind, UidFields | None, int | None] | None:
    """Split a ``dev_identifier`` into format and format-specific identity.

    Answers None for a row this module has no reader for, which keeps a future
    Live schema from turning every lookup into an exception.
    """
    matched = _DEV_IDENTIFIER.match(dev_identifier)
    if matched is None:
        return None
    kind = _KIND_BY_DEV_FORMAT.get(matched["format"])
    if kind is None:
        return None
    body = matched["body"]
    if kind is PluginKind.VST3:
        cid = body.replace("-", "")
        if len(cid) != 32:
            return None
        try:
            return kind, cid_to_uid_fields(cid), None
        except ValueError:
            logger.debug("Live database class id is not hex: %s", dev_identifier)
            return None
    unique_id = body.split("?", 1)[0]
    if not unique_id.lstrip("-").isdigit():
        return None
    return kind, None, int(unique_id)


def read_live_database(database: pathlib.Path) -> tuple[DatabasePlugin, ...]:
    """Every plugin one Live database file knows, read-only.

    Database files without these tables -- ``Live-files-*.db``, or a Live whose
    schema moved on -- answer with nothing rather than blowing up a scan.
    """
    try:
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    except sqlite3.OperationalError as error:
        logger.debug("Cannot open Live Database %s: %s", database, error)
        return ()
    try:
        rows = connection.execute(_DATABASE_QUERY).fetchall()
    except sqlite3.OperationalError as error:
        logger.debug("Live Database %s has no usable plugin tables: %s", database, error)
        return ()
    finally:
        connection.close()

    found: list[DatabasePlugin] = []
    for row in cast(list[tuple[object, ...]], rows):
        name, vendor, dev_identifier, path, scanstate, domain, enabled = row
        if not isinstance(name, str) or not isinstance(dev_identifier, str) or not isinstance(path, str):
            continue
        parsed = parse_dev_identifier(dev_identifier)
        if parsed is None:
            continue
        kind, uid_fields, unique_id = parsed
        found.append(
            DatabasePlugin(
                name=name,
                vendor=vendor if isinstance(vendor, str) else "",
                kind=kind,
                uid_fields=uid_fields,
                unique_id=unique_id,
                module_path=pathlib.Path(path),
                domain=str(domain),
                domain_enabled=bool(enabled),
                module_scanstate=int(cast(int, scanstate)),
            )
        )
    return tuple(found)


def read_live_databases(database_dir: pathlib.Path) -> tuple[DatabasePlugin, ...]:
    """Every plugin the newest usable database under ``database_dir`` knows.

    One database, not all of them. Measured 2026-08-12: this folder holds
    ``Live-plugins-1.db`` from Live 12 and ``Live-files-53.db``, the combined
    files-and-plugins database Live 11 wrote, and the two disagree about the
    thing that matters -- Live 11 had the custom VST2 folder enabled and Live 12
    has it disabled. Merging them would answer with the Live the user stopped
    running. The newest file wins, which is the Live they are running now.
    """
    for database in sorted(database_dir.rglob("*.db"), key=lambda db: db.stat().st_mtime, reverse=True):
        found = read_live_database(database)
        if found:
            logger.debug("Reading plugins from %s", database)
            return found
    return ()


def _vst3_uids(plugins: Iterable[DatabasePlugin]) -> dict[str, UidFields]:
    """Class ids of the VST3 rows among ``plugins``, first name winning."""
    found: dict[str, UidFields] = {}
    for plugin in plugins:
        if plugin.kind is PluginKind.VST3 and plugin.uid_fields is not None:
            found.setdefault(plugin.name, plugin.uid_fields)
    return found


def harvest_live_database_uids(database: pathlib.Path) -> dict[str, UidFields]:
    """Class ids of every VST3 one Live database file knows, by display name."""
    return _vst3_uids(read_live_database(database))


# -- a probed class id file -------------------------------------------------


class UidDbEntry(pydantic.BaseModel):
    """One class name in a probed uid file. Only ``fields`` is load bearing."""

    model_config = pydantic.ConfigDict(extra="ignore")

    fields: UidFields


def read_uid_db(path: pathlib.Path) -> dict[str, UidFields]:
    """Class ids from a probed JSON file, by display name.

    The file maps a class name to an object whose ``fields`` are the four Uid
    values; everything else it records (vendor, module, canonical hex) is there
    for a human reading it and is ignored here.
    """
    raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    entries = pydantic.TypeAdapter(dict[str, UidDbEntry]).validate_python(raw)
    return {name: entry.fields for name, entry in entries.items()}


# -- resolution order -------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class UidSource:
    """One place class ids were read from, named so a disagreement can say where."""

    origin: str
    fields: Mapping[str, UidFields]


@dataclasses.dataclass(frozen=True)
class UidDisagreement:
    """Two sources claiming different class ids for one display name."""

    name: str
    winner: str
    winning_fields: UidFields
    loser: str
    losing_fields: UidFields

    def __str__(self) -> str:
        return (
            f"{self.name}: {self.winner} says {self.winning_fields}, "
            f"{self.loser} says {self.losing_fields}"
        )


@dataclasses.dataclass(frozen=True)
class UidLookup:
    """Class ids from several sources, highest priority first."""

    sources: tuple[UidSource, ...] = ()

    def resolve(self, name: str) -> UidFields | None:
        """The class id the highest priority source knows for ``name``."""
        for source in self.sources:
            fields = source.fields.get(name)
            if fields is not None:
                return fields
        return None

    def names(self) -> frozenset[str]:
        """Every display name any source knows, for suggesting a mapping."""
        return frozenset(name for source in self.sources for name in source.fields)

    def disagreements(self) -> tuple[UidDisagreement, ...]:
        """Every name where a lower priority source claims another class id."""
        found: list[UidDisagreement] = []
        for index, source in enumerate(self.sources):
            for name, fields in source.fields.items():
                if any(name in earlier.fields for earlier in self.sources[:index]):
                    continue
                for later in self.sources[index + 1 :]:
                    other = later.fields.get(name)
                    if other is not None and other != fields:
                        found.append(UidDisagreement(name, source.origin, fields, later.origin, other))
        return tuple(found)
