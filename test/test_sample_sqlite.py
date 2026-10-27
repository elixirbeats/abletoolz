"""Catalog ingestion must remain atomic and indexed across platforms."""

from __future__ import annotations

import ctypes
import os
import pathlib
import sqlite3
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from ctypes import wintypes
from unittest.mock import patch

import pytest
from conftest import stored_paths, stored_records

from abletoolz.samples import database
from abletoolz.samples.catalog import SampleCatalog, SampleValue
from abletoolz.samples.crc import file_crc
from abletoolz.samples.discovery import AudioFile, DiscoveryBackend, discover_audio_files


@pytest.mark.parametrize("backend", ["auto", "portable"])
def test_native_and_portable_discovery_match_metadata(tmp_path: pathlib.Path, backend: DiscoveryBackend) -> None:
    nested = tmp_path / "音_é"
    nested.mkdir()
    for index, name in enumerate(("kick.WaV", "bass.AIFF", "empty.flac", "voice.mp3", "loop.rx2")):
        path = nested / name
        path.write_bytes(bytes(range(index * 40)))
    (nested / "ignore.txt").touch()
    (nested / "folder.wav").mkdir()
    files = list(discover_audio_files(tmp_path, backend))
    assert len(files) == 5
    for entry in files:
        info = pathlib.Path(entry.path).stat()
        assert (entry.metadata.st_size, entry.metadata.st_mtime_ns, entry.metadata.st_dev, entry.metadata.st_ino) == (
            info.st_size,
            info.st_mtime_ns,
            info.st_dev,
            info.st_ino,
        )
    assert {entry.path for entry in files} == {entry.path for entry in discover_audio_files(tmp_path, "portable")}


def test_discovery_stays_in_root_and_skips_links(tmp_path: pathlib.Path) -> None:
    root, outside = tmp_path / "selected", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "inside.wav").touch()
    (outside / "outside.wav").touch()
    try:
        (root / "loop").symlink_to(root, target_is_directory=True)
        (root / "other").symlink_to(outside, target_is_directory=True)
        (root / "linked.wav").symlink_to(outside / "outside.wav")
    except OSError as error:
        pytest.skip(f"Symlinks unavailable: {error}")
    for backend in ("auto", "portable"):
        assert [pathlib.Path(entry.path).name for entry in discover_audio_files(root, backend)] == ["inside.wav"]


def test_macos_resource_forks_are_never_samples(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "Pack"
    (root / "__MACOSX" / "Pack").mkdir(parents=True)
    (root / "kick.wav").write_bytes(b"RIFF-kick")
    (root / "._kick.wav").write_bytes(b"\x00\x05\x16\x07")
    (root / "__MACOSX" / "Pack" / "._kick.wav").write_bytes(b"\x00\x05\x16\x07")
    for backend in ("auto", "portable"):
        assert [entry.name for entry in discover_audio_files(root, backend)] == ["kick.wav"]

    db_path = database.create_or_update_db([str(root)], tmp_path / "catalog.sqlite3")

    assert [pathlib.Path(path).name for path in stored_paths(db_path)] == ["kick.wav"]


def test_sqlite_indexes_combined_keys_and_does_not_scan_catalog(tmp_path: pathlib.Path) -> None:
    records: dict[str, dict[str, SampleValue]] = {
        "a/kick.wav": {"name": "kick.wav", "size": 100, "crc": 0, "custom": "音"},
        "b/kick.wav": {"name": "kick.wav", "size": 200, "crc": 1},
        "c/renamed.wav": {"name": "renamed.wav", "size": 100, "crc": 0},
    }
    db_path = tmp_path / "samples.sqlite3"
    with SampleCatalog.from_records(records) as in_memory:
        in_memory.backup_to(db_path)
    assert stored_records(db_path) == records
    with database.load_db(db_path) as catalog:
        assert list(catalog.candidates("renamed.wav")) == ["c/renamed.wav"]
        assert list(catalog.candidates("absent.wav", size=100, crc=0)) == ["a/kick.wav", "c/renamed.wav"]
        assert catalog.candidates("absent.wav") == {}
        assert catalog.candidates("kick.wav", size=100, crc=0) == records
    with closing(sqlite3.connect(db_path)) as connection:
        for query, parameters, indexes in (
            ("name=? AND size=? AND crc=?", ("kick.wav", 100, 0), ["samples_name_size_crc"]),
            ("size=? AND crc=?", (100, 0), ["samples_size_crc"]),
            ("name=? OR (size=? AND crc=?)", ("kick.wav", 100, 0), ["samples_name_size_crc", "samples_size_crc"]),
        ):
            plan = connection.execute("EXPLAIN QUERY PLAN SELECT path FROM samples WHERE " + query, parameters)
            steps = " ".join(str(row) for row in plan)
            assert all(index in steps for index in indexes)


def test_snapshot_is_shared_by_workers_and_survives_update(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "samples"
    root.mkdir()
    sample = root / "kick.wav"
    sample.write_bytes(b"before")
    db_path = database.create_or_update_db([str(root)], tmp_path / "samples.sqlite3")
    with database.load_db(db_path) as catalog:
        before = dict(catalog.candidates("kick.wav")[str(sample)])
        with ThreadPoolExecutor(max_workers=4) as executor:
            found = executor.map(lambda name: list(catalog.candidates(name)), ["kick.wav"] * 50)
            assert list(found) == [[str(sample)]] * 50
        sample.write_bytes(b"after and longer")
        database.create_or_update_db([str(root)], db_path)
        assert dict(catalog.candidates("kick.wav")[str(sample)]) == before
    with database.load_db(db_path) as catalog:
        assert catalog.candidates("kick.wav")[str(sample)]["size"] == sample.stat().st_size
        assert catalog.candidates("kick.wav")[str(sample)]["crc"] == file_crc(sample)


def test_sqlite_update_reuses_crc_and_preserves_unselected_roots(tmp_path: pathlib.Path) -> None:
    root, other = tmp_path / "selected", tmp_path / "offline"
    root.mkdir()
    other.mkdir()
    sample, retained = root / "kick.wav", other / "keep.wav"
    sample.write_bytes(b"before")
    retained.write_bytes(b"offline")
    db_path = tmp_path / "samples.sqlite3"
    with patch.object(database, "file_crc", wraps=file_crc) as measure:
        database.create_or_update_db([str(root), str(other)], db_path)
        assert measure.call_count == 2
        database.create_or_update_db([str(root)], db_path)
        assert measure.call_count == 2
        previous_time = sample.stat().st_mtime_ns
        sample.write_bytes(b"after!")
        os.utime(sample, ns=(previous_time + 1_000_000, previous_time + 1_000_000))
        database.create_or_update_db([str(root)], db_path)
        assert measure.call_count == 3
    sample.unlink()
    retained.unlink()
    other.rmdir()
    database.create_or_update_db([str(root)], db_path)
    assert stored_paths(db_path) == [str(retained)]


def test_partial_scan_rolls_back_metadata_and_pruning(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "samples"
    root.mkdir()
    sample = root / "kick.wav"
    sample.write_bytes(b"before")
    db_path = database.create_or_update_db([str(root)], tmp_path / "samples.sqlite3")
    before = stored_records(db_path)
    sample.write_bytes(b"after and longer")
    real_discovery = discover_audio_files

    def interrupted(path: pathlib.Path, backend: DiscoveryBackend, suffixes: frozenset[str]) -> Iterator[AudioFile]:
        yield from real_discovery(path, backend, suffixes)
        raise PermissionError("Denied subfolder")

    monkeypatch.setattr(database, "discover_audio_files", interrupted)
    with pytest.raises(PermissionError, match="Denied"):
        database.create_or_update_db([str(root)], db_path)
    assert stored_records(db_path) == before


def test_old_crc_is_backfilled_and_changing_file_is_retried(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "samples"
    root.mkdir()
    sample = root / "kick.wav"
    sample.write_bytes(b"original")
    old_record: dict[str, SampleValue] = {
        "name": sample.name,
        "size": sample.stat().st_size,
        "last_modified_ns": sample.stat().st_mtime_ns,
        "crc": 123,
        "custom": "keep",
    }
    db_path = tmp_path / "samples.sqlite3"
    with SampleCatalog.from_records({str(sample): old_record}) as old:
        old.backup_to(db_path)

    def modified_during_read(path: pathlib.Path) -> int:
        crc = file_crc(path)
        if path.read_bytes() == b"original":
            path.write_bytes(b"changed and longer")
        return crc

    with patch.object(database, "file_crc", side_effect=modified_during_read) as measure:
        database.create_or_update_db([str(root)], db_path)
        assert measure.call_count == 2
    record = stored_records(db_path)[str(sample)]
    assert record["crc"] == file_crc(sample)
    assert record["size"] == sample.stat().st_size
    assert record["custom"] == "keep"


def test_fts_search_tracks_added_renamed_and_removed_samples(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "Drum Bass"
    root.mkdir()
    sample = root / "Café_Kick_174.WAV"
    sample.write_bytes(b"kick")
    db_path = database.create_or_update_db([str(root)], tmp_path / "samples.sqlite3")
    with database.load_db(db_path) as catalog:
        assert isinstance(catalog, SampleCatalog)
        assert catalog.search("cafe kick 17") == (str(sample),)
        assert catalog.search("drum bass") == (str(sample),)
        assert catalog.search('" OR * -') == ()
        assert catalog.search("", limit=10) == ()
        assert catalog.search("kick", limit=0) == ()
    renamed = sample.rename(root / "Snare.wav")
    database.create_or_update_db([str(root)], db_path)
    with database.load_db(db_path) as catalog:
        assert isinstance(catalog, SampleCatalog)
        assert catalog.search("kick") == ()
        assert catalog.search("snare") == (str(renamed),)
    renamed.unlink()
    database.create_or_update_db([str(root)], db_path)
    with database.load_db(db_path) as catalog:
        assert isinstance(catalog, SampleCatalog)
        assert catalog.search("snare") == ()


def test_replaced_file_with_same_size_and_mtime_gets_new_crc(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "samples"
    root.mkdir()
    sample = root / "kick.wav"
    sample.write_bytes(b"before")
    db_path = database.create_or_update_db([str(root)], tmp_path / "samples.sqlite3")
    previous = sample.stat()
    replacement = root / "replacement.tmp"
    replacement.write_bytes(b"after!")
    os.utime(replacement, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    os.replace(replacement, sample)
    with patch.object(database, "file_crc", wraps=file_crc) as measure:
        database.create_or_update_db([str(root)], db_path)
        assert measure.call_count == 1
    assert stored_records(db_path)[str(sample)]["crc"] == file_crc(sample)


def test_scanning_nested_root_updates_foreign_key(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "samples"
    nested = root / "child"
    nested.mkdir(parents=True)
    sample = nested / "kick.wav"
    sample.write_bytes(b"kick")
    db_path = database.create_or_update_db([str(root)], tmp_path / "samples.sqlite3")
    database.create_or_update_db([str(nested)], db_path)
    with closing(sqlite3.connect(db_path)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT roots.path FROM samples JOIN roots ON roots.id=samples.root_id WHERE samples.path=?",
            (str(sample),),
        ).fetchone() == (str(nested),)


def unreadable_drive(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """A root with one folder in it nobody may list, like a drive's System Volume Information."""
    root = tmp_path / "Drive"
    blocked = root / "System Volume Information"
    blocked.mkdir(parents=True)
    (blocked / "hidden.wav").touch()
    (root / "Samples").mkdir()
    (root / "Samples" / "kick.wav").touch()
    return root, blocked


def test_an_unreadable_subfolder_is_skipped_and_an_unreadable_root_is_not(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One folder the scan may not open must not end the whole scan; the folder the user named still fails loudly."""
    root, blocked = unreadable_drive(tmp_path)
    real_scandir = os.scandir

    def scandir(path: pathlib.Path) -> Iterator[os.DirEntry[str]]:
        if pathlib.Path(path) == blocked:
            raise PermissionError(13, "Access is denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    assert [entry.name for entry in discover_audio_files(root, "portable")] == ["kick.wav"]
    assert "System Volume Information" in caplog.text
    with pytest.raises(PermissionError):
        list(discover_audio_files(blocked, "portable"))


@pytest.mark.skipif(sys.platform != "win32", reason="the native Windows walker")
def test_the_windows_walker_skips_an_unreadable_subfolder(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, blocked = unreadable_drive(tmp_path)
    real_dll = ctypes.WinDLL

    class Kernel:
        """kernel32, except CreateFileW refuses ``blocked`` the way Windows refuses System Volume Information."""

        def __init__(self, name: str, use_last_error: bool) -> None:
            self._real = real_dll(name, use_last_error=use_last_error)
            real_open = self._real.CreateFileW

            def create_file(path: str, *rest: object) -> object:
                if pathlib.Path(path) == blocked:
                    ctypes.set_last_error(5)  # ERROR_ACCESS_DENIED
                    return wintypes.HANDLE(-1).value
                real_open.argtypes, real_open.restype = create_file.argtypes, create_file.restype  # type: ignore[attr-defined]
                return real_open(path, *rest)

            self.CreateFileW = create_file

        def __getattr__(self, name: str) -> object:
            return getattr(self._real, name)

    monkeypatch.setattr(ctypes, "WinDLL", Kernel)
    assert [entry.name for entry in discover_audio_files(root, "auto")] == ["kick.wav"]
    with pytest.raises(PermissionError):
        list(discover_audio_files(blocked, "auto"))
