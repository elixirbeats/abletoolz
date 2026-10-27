"""Index sample folders and load a searchable snapshot of their metadata.

One scan fills two databases. Audio goes to the sample database. The files
only a plugin loads -- a Kontakt instrument, a Battery kit, a Maschine group --
go to the plugin files database beside it, so a moved one is found by name the
same way a sample is, and a lookup for one kind never turns up the other.
Which suffixes those are is the plugin maps' to say, not this module's.
"""

from __future__ import annotations

import logging
import os
import pathlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from itertools import islice

import tqdm

from abletoolz.machine import DEFAULT_DB_PATH
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.samples.catalog import SampleCatalog, SampleRecord, SampleValue, initialize, read_stored, store
from abletoolz.samples.crc import file_crc
from abletoolz.samples.discovery import AUDIO_SUFFIXES, AudioFile, DiscoveryBackend, discover_audio_files
from abletoolz.workers import worker_count

logger = logging.getLogger(__name__)

_CRC_VERSION = "crc16-umts-prefix16384-v1"

type Found = dict[pathlib.Path, list[AudioFile]]
"""The files one walk found under each root, for one database."""


def plugin_file_suffixes() -> frozenset[str]:
    """The suffixes of files only a plugin loads: every one a plugin map declares, less audio."""
    return CATALOG.file_suffixes() - AUDIO_SUFFIXES


def plugin_files_db_path(db_path: pathlib.Path | None = None) -> pathlib.Path:
    """The plugin files database that sits beside the sample database at ``db_path``, the default one when None."""
    if db_path is None:
        db_path = DEFAULT_DB_PATH
    return db_path.with_name(f"plugin_files{db_path.suffix}")


def _is_plugin_file(name: str, plugin_suffixes: frozenset[str]) -> bool:
    return os.path.splitext(name)[1].lower() in plugin_suffixes


def _scan_roots(paths: list[str]) -> list[pathlib.Path]:
    roots = sorted({pathlib.Path(path).expanduser().resolve(strict=True) for path in paths}, key=lambda p: len(p.parts))
    selected: list[pathlib.Path] = []
    for root in roots:
        if not root.is_dir():
            raise NotADirectoryError(root)
        if not any(root.is_relative_to(parent) for parent in selected):
            selected.append(root)
    return selected


def create_or_update_db(
    paths: list[str],
    db_path: pathlib.Path | None = None,
    *,
    backend: DiscoveryBackend = "auto",
    workers: int | None = None,
) -> pathlib.Path:
    """Refresh selected roots; retain records for other roots and unavailable drives.

    One walk of each root: audio goes to ``db_path``, files only a plugin loads
    to :func:`plugin_files_db_path` beside it. SQLite commits metadata, checksum
    and search updates together. A failed scan leaves existing records intact.
    Only selected roots are refreshed; their directory links are skipped.
    """
    if db_path is None:
        db_path = DEFAULT_DB_PATH
    workers = worker_count(workers)
    plugin_suffixes = plugin_file_suffixes()
    roots = _scan_roots(paths)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    suffixes = AUDIO_SUFFIXES | plugin_suffixes
    audio: Found = {root: [] for root in roots}
    plugin_files: Found = {root: [] for root in roots}
    for root in roots:
        entries = discover_audio_files(root, backend, suffixes)
        for entry in tqdm.tqdm(entries, desc=f"Scanning {root}", unit="files"):
            (plugin_files if _is_plugin_file(entry.name, plugin_suffixes) else audio)[root].append(entry)
    _update_sqlite(audio, db_path, workers)
    _update_sqlite(plugin_files, plugin_files_db_path(db_path), workers)
    return db_path


def _measure_record(entry: AudioFile, known: SampleRecord) -> dict[str, SampleValue]:
    for _attempt in range(3):
        metadata = entry.stat() if _attempt == 0 else pathlib.Path(entry.path).stat()
        unchanged = (
            known.get("crc_version") == _CRC_VERSION
            and "crc" in known
            and known.get("size") == metadata.st_size
            and known.get("last_modified_ns") == metadata.st_mtime_ns
            and known.get("file_id") == str(metadata.st_ino)
            and known.get("device_id") == str(metadata.st_dev)
        )
        record = dict(known)
        if not unchanged:
            record["crc"] = file_crc(pathlib.Path(entry.path))
            after = pathlib.Path(entry.path).stat()
            if (metadata.st_size, metadata.st_mtime_ns, metadata.st_dev, metadata.st_ino) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_dev,
                after.st_ino,
            ):
                continue
        record.update(
            name=entry.name,
            size=metadata.st_size,
            last_modified=metadata.st_mtime,
            last_modified_ns=metadata.st_mtime_ns,
            crc_version=_CRC_VERSION,
            file_id=str(metadata.st_ino),
            device_id=str(metadata.st_dev),
        )
        return record
    raise OSError(f"Sample kept changing while its checksum was read: {entry.path}")


def _measure_job(job: tuple[AudioFile, SampleRecord]) -> tuple[str, dict[str, SampleValue] | None]:
    entry, known = job
    try:
        return entry.path, _measure_record(entry, known)
    except FileNotFoundError:
        return entry.path, None


def _update_sqlite(found: Found, db_path: pathlib.Path, workers: int) -> None:
    with closing(sqlite3.connect(db_path)) as connection:
        initialize(connection)
        connection.execute("PRAGMA cache_size=-65536")
        connection.execute("PRAGMA temp_store=MEMORY")
        with connection:
            connection.execute("CREATE TEMP TABLE seen(path TEXT PRIMARY KEY)")
            for root, found_in_root in found.items():
                connection.execute("INSERT OR IGNORE INTO roots(path) VALUES (?)", (str(root),))
                root_id = connection.execute("SELECT id FROM roots WHERE path=?", (str(root),)).fetchone()[0]
                entries = iter(found_in_root)
                with (
                    ThreadPoolExecutor(max_workers=workers) as pool,
                    tqdm.tqdm(total=len(found_in_root), desc=f"Indexing {db_path.name}", unit="files") as progress,
                ):
                    while batch := list(islice(entries, 512)):
                        known_records = {}
                        root_ids: dict[str, int | None] = {}
                        for entry in batch:
                            stored = read_stored(connection, entry.path)
                            known_records[entry.path], root_ids[entry.path] = stored or ({}, None)
                        jobs = [(entry, known_records[entry.path]) for entry in batch]
                        for path, record in pool.map(_measure_job, jobs):
                            if record is None:
                                continue
                            connection.execute("INSERT INTO seen(path) VALUES (?)", (path,))
                            if record == known_records[path]:
                                if root_ids[path] != root_id:
                                    connection.execute("UPDATE samples SET root_id=? WHERE path=?", (root_id, path))
                                continue
                            store(connection, path, root_id, record)
                        progress.update(len(batch))
                prefix = str(root).rstrip(os.sep) + os.sep
                connection.execute(
                    """DELETE FROM samples WHERE path>=? AND path<?
                       AND NOT EXISTS (SELECT 1 FROM seen WHERE seen.path=samples.path)""",
                    (prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)),
                )
        count = connection.execute("SELECT count(*) FROM samples").fetchone()[0]
    logger.info("Database at %s contains %s files", db_path.resolve(), count)


def prune_db(db_path: pathlib.Path | None = None) -> int:
    """Remove every record whose file is gone, from the sample database and the plugin files database beside it.

    A file on a drive that isn't connected counts as gone; ``--db`` over that
    drive adds it back. Answers how many records were removed.
    """
    if db_path is None:
        db_path = DEFAULT_DB_PATH
    if not db_path.exists():
        raise FileNotFoundError(f"Database {db_path} doesn't exist! Run --db with sample dir(s) first.")
    plugin_files = plugin_files_db_path(db_path)
    return sum(_prune(path) for path in (db_path, plugin_files) if path.exists())


def _prune(db_path: pathlib.Path) -> int:
    with closing(sqlite3.connect(db_path)) as connection:
        paths = [row[0] for row in connection.execute("SELECT path FROM samples")]
        gone = [
            (path,)
            for path in tqdm.tqdm(paths, desc=f"Checking {db_path.name}", unit="files")
            if not os.path.isfile(path)
        ]
        with connection:
            connection.executemany("DELETE FROM samples WHERE path=?", gone)
    logger.info("Removed %s of %s files from %s that no longer exist", len(gone), len(paths), db_path.resolve())
    return len(gone)


def load_plugin_files_db(db_path: pathlib.Path | None = None) -> SampleCatalog:
    """Open the plugin files database; by default the one beside the default sample database."""
    return load_db(plugin_files_db_path() if db_path is None else db_path)


def load_db(db_path: pathlib.Path | None = None) -> SampleCatalog:
    """Open an indexed read-only snapshot; close it after use."""
    if db_path is None:
        db_path = DEFAULT_DB_PATH
    if not db_path.exists():
        raise FileNotFoundError(f"Database {db_path} doesn't exist! Run --db with sample dir(s) first.")
    return SampleCatalog.open(db_path)
