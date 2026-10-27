"""Per-set sidecar metadata: ``<set stem>.meta.yaml`` beside the set it describes (see :func:`sidecar_path`).

The file has two zones and the split is the whole point. Everything under
``scan`` belongs to abletoolz and is replaced whole every time the set is
scanned. ``status``, ``notes``, and any other key someone puts in the file
belong to the user: they are read, carried across every rescan, and written back
exactly as they came. Nothing here ever invents or edits them.

The scan block doubles as a cache. Reuse requires the same set bytes, scanner
version and scan fingerprint; a run that only checks plugins consults it before
the set is even parsed, and leaves an unchanged set unread. The fingerprint,
computed once per batch, covers shipped Python and JSON support files,
abletoolz's plugin database and the one Live keeps, configuration, the
computer's locations and its identity, so new plugin support refreshes old
answers even when the package version has not changed.

Sample files and installed plugin binaries are not part of it: installing one
alone invalidates nothing, while rebuilding the plugin database, or Live
rescanning its plugins, invalidates cached plugin checks on the next run. A
sample check never answers from the cache, since a deleted or unplugged sample
changes its answer without changing anything the key covers.

``set_hash`` is of the bytes that were read, not of anything written afterwards.
A run that edits and saves a set therefore leaves a sidecar that deliberately no
longer matches the file beside it -- the set is not the one that was scanned,
and the next run has to look for itself.

A field left null was not measured by the run that wrote the block, which is not
the same as measuring zero: a ``--check-samples`` run says nothing about
plugins. A rescan of bytes already in the cache adds what it measured and keeps
what it did not, so passes with different flags accumulate into one record.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import logging
import os
import pathlib
import platform

import pydantic
import yaml

from abletoolz import __version__
from abletoolz.live_set.persistence import is_backup_set
from abletoolz.machine import Locations
from abletoolz.plugins.config import AbletoolzConfig
from abletoolz.plugins.identity.uid_sources import live_databases_newest_first

logger = logging.getLogger(__name__)

SUFFIX = ".meta.yaml"
PACKAGE_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Raised when what a scan measures changes between releases, so a block cached
# before is thrown away. 2: --check-samples counts files inside plugin patches.
# 3: a plugin patch that points at no file is no longer counted as a missing sample.
SCAN_REVISION = 3

# What wrote a scan block, and what a cached block has to match to be reused.
SCANNER = f"abletoolz {__version__} scan {SCAN_REVISION}"

HEADER = """\
# abletoolz sidecar.
#
# status and notes are yours. abletoolz reads them, carries them across every
# rescan and never writes them itself; so is any other key you add here.
# Everything under scan: is abletoolz's, and is replaced whole every time the
# set is scanned.
"""


class SetScan(pydantic.BaseModel):
    """What one scan of a set found; a null field was not measured (see the module docstring).

    ``plugins_fixed`` is keyed by the device name of each plugin device whose
    files a run relinked; ``samples_fixed`` counts Live's own sample references
    it relinked.
    """

    model_config = pydantic.ConfigDict(extra="ignore")

    scanned: datetime.datetime
    scanned_with: str
    set_hash: str
    scan_fingerprint: str | None = None
    live_version: str | None = None
    bars: int | None = None
    bpm: float | None = None
    plugins_missing: dict[str, int] | None = None
    plugins_fixed: dict[str, int] | None = None
    samples_fixed: int | None = None
    samples_missing: int | None = None
    samples_missing_by_name: dict[str, int] | None = None


class SetMeta(pydantic.BaseModel):
    """One sidecar file: the user's zone, then abletoolz's.

    ``extra="allow"`` is the user's zone being the user's: a key nothing here
    knows about survives a rescan the same way ``status`` and ``notes`` do.
    """

    model_config = pydantic.ConfigDict(extra="allow")

    status: str | None = None
    notes: str | None = None
    scan: SetScan | None = None


def sidecar_path(set_path: pathlib.Path) -> pathlib.Path:
    """Where the sidecar for one set lives: ``Song.meta.yaml`` for ``Song.als``, ``Song.alc.meta.yaml`` for a clip.

    Anything but a set keeps its suffix, so ``Song.als`` and ``Song.alc`` in one
    folder do not share a sidecar and overwrite each other's scan.
    """
    name = set_path.stem if set_path.suffix.lower() == ".als" else set_path.name
    return set_path.parent / (name + SUFFIX)


def file_hash(path: pathlib.Path) -> str:
    """Sha256 of a file's bytes, which is what tells one scan's set from another."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def support_fingerprint(root: pathlib.Path) -> str:
    """Hash shipped code and support tables, independent of install location."""
    files = sorted([*root.rglob("*.py"), *root.rglob("*.json")])
    entries = [(path.relative_to(root).as_posix(), file_hash(path)) for path in files]
    return hashlib.sha256(json.dumps(entries, ensure_ascii=True).encode()).hexdigest()


def scan_fingerprint(
    config: AbletoolzConfig,
    locations: Locations,
    *,
    support_root: pathlib.Path = PACKAGE_ROOT,
    machine: tuple[str, str] | None = None,
) -> str | None:
    """Identify scan inputs once per batch; unreadable inputs disable cache reuse.

    ``support_root`` is the shipped code and tables, this package by default.
    ``machine`` is the computer's name and the working directory relative
    config paths resolve against, this process's when None.
    """
    try:
        database_path = locations.plugin_db
        database_hash = file_hash(database_path) if database_path.exists() else None
        uid_path = config.plugin_uid_db
        uid_hash = file_hash(uid_path) if uid_path is not None and uid_path.exists() else None
        # Size and time, not a hash: Live's file database alone can run to hundreds of megabytes.
        live_folder = locations.live_database
        live_databases = (
            [
                (str(db.resolve()), db.stat().st_size, db.stat().st_mtime_ns)
                for db in live_databases_newest_first(live_folder)
            ]
            if live_folder is not None and live_folder.is_dir()
            else []
        )
        inputs = {
            "support": support_fingerprint(support_root),
            "plugin_database": {"path": str(database_path.resolve()), "hash": database_hash},
            "uid_database": uid_hash,
            "live_database": sorted(live_databases),
            "config": dataclasses.asdict(config),
            "locations": dataclasses.asdict(locations),
            "machine": list((platform.node(), str(pathlib.Path.cwd())) if machine is None else machine),
        }
        encoded = json.dumps(inputs, sort_keys=True, default=str, ensure_ascii=True).encode()
    except OSError as error:
        logger.warning("Cannot fingerprint scan inputs; cached scans will not be reused: %s", error)
        return None
    return hashlib.sha256(encoded).hexdigest()


def describes_a_real_set(set_path: pathlib.Path) -> bool:
    """Whether a sidecar beside this file would describe anything worth describing.

    A backup is a copy of a set the user already has and a ``._`` file is macOS
    resource-fork litter that only looks like one; neither gets a file written
    next to it. Same predicate the set collector uses, so the two cannot drift
    into writing sidecars the collector would never have visited.
    """
    return not is_backup_set(set_path)


def read(set_path: pathlib.Path) -> SetMeta | None:
    """The sidecar beside ``set_path``, or None when there is no usable one.

    A sidecar nothing can parse counts as no sidecar: nothing is cached from it
    and the next write replaces it, which loses whatever the user had written in
    a file that no longer says it. That is loud on purpose.
    """
    path = sidecar_path(set_path)
    if not path.is_file():
        return None
    try:
        return SetMeta.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, pydantic.ValidationError) as error:
        logger.warning("Ignoring unreadable sidecar %s: %s", path, error)
        return None


def cached_scan(set_path: pathlib.Path, digest: str, *, fingerprint: str | None) -> SetScan | None:
    """Reuse a scan only for the same set, scanner, support and machine inputs."""
    if fingerprint is None:
        return None
    document = read(set_path)
    if document is None or document.scan is None:
        return None
    scan = document.scan
    if scan.set_hash != digest or scan.scanned_with != SCANNER or scan.scan_fingerprint != fingerprint:
        return None
    return scan


def carry_forward(previous: SetScan, current: SetScan) -> SetScan:
    """Fill what this run did not measure from a scan of the same bytes.

    Only reached on a cache hit, so ``previous`` describes the same file this
    run read, and anything it measured is still true of it.
    """
    kept = {name: value for name, value in previous.model_dump().items() if getattr(current, name) is None}
    return current.model_copy(update=kept)


def write(set_path: pathlib.Path, scan: SetScan, *, human_source: pathlib.Path | None = None) -> pathlib.Path | None:
    """Write the sidecar for ``set_path``, keeping every user-written value.

    ``human_source`` is the set whose sidecar the user's zone comes from, for
    the ``--output`` case where the scanned set and the written one are
    different files. Answers None when the file is one nothing should be written
    beside.
    """
    if not describes_a_real_set(set_path):
        logger.debug("No sidecar written for %s", set_path)
        return None
    existing = read(human_source if human_source is not None else set_path)
    document = SetMeta() if existing is None else existing
    document = document.model_copy(update={"scan": scan})
    path = sidecar_path(set_path)
    body = yaml.safe_dump(document.model_dump(mode="json"), sort_keys=False, allow_unicode=True)
    # Through a temp file, so a run killed mid-write never leaves half a sidecar and loses the user's notes.
    partial = path.with_name(path.name + ".tmp")
    partial.write_text(HEADER + body, encoding="utf-8")
    os.replace(partial, path)
    logger.debug("Wrote sidecar %s", path)
    return path


def now() -> datetime.datetime:
    """Scan time, with this machine's offset on it so it reads the same anywhere."""
    return datetime.datetime.now().astimezone()
