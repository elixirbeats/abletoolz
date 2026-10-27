"""Portable scanning and updates must preserve a usable sample catalog."""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
from contextlib import closing
from unittest.mock import patch

import pytest
from conftest import stored_paths, stored_records

from abletoolz.samples import database
from abletoolz.samples.catalog import SampleCatalog, SampleValue
from abletoolz.samples.discovery import audio_entries


def test_scan_finds_mixed_case_extensions_once_per_file_and_directory(tmp_path: pathlib.Path) -> None:
    folder = tmp_path / "nested"
    folder.mkdir()
    names = ["kick.wav", "hat.WaV", "loop.RX2", "bass.FLAC", "voice.AiFf", "ignore.txt"]
    for name in names:
        (folder / name).touch()
    (folder / "folder.wav").mkdir()

    with patch.object(os, "scandir", wraps=os.scandir) as scan:
        found = [pathlib.Path(entry.path) for entry in audio_entries(tmp_path)]

    assert sorted(path.name for path in found) == sorted(names[:-1])
    assert len(found) == len(set(found))
    assert scan.call_count == 3


def test_scan_does_not_follow_directory_symlink_loops(tmp_path: pathlib.Path) -> None:
    (tmp_path / "kick.wav").touch()
    link = tmp_path / "loop"
    try:
        link.symlink_to(tmp_path, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Directory symlinks are unavailable: {error}")
    assert [pathlib.Path(entry.path) for entry in audio_entries(tmp_path)] == [tmp_path / "kick.wav"]


def test_update_refreshes_existing_metadata_and_preserves_extra_fields(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "samples"
    root.mkdir()
    sample = root / "音.WaV"
    sample.write_bytes(b"old")
    db_path = tmp_path / "catalog.sqlite3"
    database.create_or_update_db([str(root)], db_path)
    sample_key = str(sample.resolve())
    with closing(sqlite3.connect(db_path)) as connection, connection:
        extra = json.loads(connection.execute("SELECT extra FROM samples WHERE path=?", (sample_key,)).fetchone()[0])
        extra["custom_metadata"] = 42
        connection.execute("UPDATE samples SET extra=? WHERE path=?", (json.dumps(extra), sample_key))
    sample.write_bytes(b"new and longer")
    metadata = sample.stat()

    database.create_or_update_db([str(root)], db_path)

    record = stored_records(db_path)[sample_key]
    assert record["size"] == metadata.st_size
    assert record["last_modified"] == metadata.st_mtime
    assert record["last_modified_ns"] == metadata.st_mtime_ns
    assert record["custom_metadata"] == 42


def test_update_deduplicates_repeated_and_nested_roots(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "samples"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (nested / "kick.wav").touch()
    db_path = tmp_path / "catalog.sqlite3"
    with patch.object(os, "scandir", wraps=os.scandir) as scan:
        database.create_or_update_db([str(nested), str(root), str(root)], db_path, backend="portable")
    assert scan.call_count == 2
    assert len(stored_paths(db_path)) == 1


def test_missing_root_does_not_erase_the_existing_catalog(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "samples"
    root.mkdir()
    (root / "old.wav").touch()
    db_path = database.create_or_update_db([str(root)], tmp_path / "catalog.sqlite3")
    with pytest.raises(FileNotFoundError):
        database.create_or_update_db([str(tmp_path / "missing")], db_path)
    assert [pathlib.Path(path).name for path in stored_paths(db_path)] == ["old.wav"]


def test_an_in_memory_catalog_indexes_distinct_same_name_paths(tmp_path: pathlib.Path) -> None:
    records = {
        str(tmp_path / "a" / "kick.wav"): {"name": "kick.wav", "size": 1},
        str(tmp_path / "b" / "kick.wav"): {"name": "kick.wav", "size": 2},
    }

    catalog = SampleCatalog.from_records(records)

    assert catalog.candidates("kick.wav") == records
    assert list(catalog.candidates("kick.wav")) == list(records)
    assert catalog.candidates("missing.wav") == {}


def test_loaded_snapshot_is_independent_of_later_input_changes(tmp_path: pathlib.Path) -> None:
    path = str(tmp_path / "kick.wav")
    record: dict[str, SampleValue] = {"name": "kick.wav", "size": 1}
    source = {path: record}
    catalog = SampleCatalog.from_records(source)
    record["name"] = "changed.wav"
    source.clear()
    assert list(catalog.candidates("kick.wav")) == [path]
    assert catalog.candidates("kick.wav")[path]["name"] == "kick.wav"


def test_one_scan_puts_audio_and_plugin_files_in_their_own_databases(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    (root / "kick.wav").write_bytes(b"audio")
    (root / "Drum Kit.MXGRP").write_bytes(b"kit")
    (root / "notes.txt").write_bytes(b"text")
    db_path = tmp_path / "catalog.sqlite3"

    database.create_or_update_db([str(root)], db_path)

    assert [pathlib.Path(path).name for path in stored_paths(db_path)] == ["kick.wav"]
    plugin_files = stored_paths(database.plugin_files_db_path(db_path))
    assert [pathlib.Path(path).name for path in plugin_files] == ["Drum Kit.MXGRP"]


def test_a_rescan_keeps_each_kind_in_its_own_database(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    (root / "Piano.nki").write_bytes(b"instrument")
    (root / "Piano C3.wav").write_bytes(b"audio")
    db_path = tmp_path / "catalog.sqlite3"
    database.create_or_update_db([str(root)], db_path)

    database.create_or_update_db([str(root)], db_path)

    plugin_files = stored_paths(database.plugin_files_db_path(db_path))
    assert (len(stored_paths(db_path)), len(plugin_files)) == (1, 1)
