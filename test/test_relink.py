"""One lookup for every missing file a set points at, and collecting what it finds."""

from __future__ import annotations

import pathlib

import pytest

from abletoolz.samples.catalog import SampleCatalog, SampleValue
from abletoolz.samples.crc import MAX_CRC_SIZE, file_crc
from abletoolz.samples.database import (
    create_or_update_db,
    load_plugin_files_db,
    plugin_files_db_path,
)
from abletoolz.samples.relink import COLLECT_FOLDER, Collector, Evidence, find


def library(*files: tuple[pathlib.Path, bytes, float]) -> SampleCatalog:
    records: dict[str, dict[str, SampleValue]] = {}
    for path, body, mtime in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        records[str(path)] = {"name": path.name, "size": len(body), "last_modified": mtime, "crc": file_crc(path)}
    return SampleCatalog.from_records(records)


def test_a_stored_size_that_disagrees_rules_a_file_out_even_when_its_date_matches(tmp_path: pathlib.Path) -> None:
    candidate = tmp_path / "kick.wav"
    db = library((candidate, b"RIFF-a", 100.0))

    lookup = find(db, Evidence("kick.wav", pathlib.Path("D:/kick.wav"), size=99, mtime=100))

    assert lookup.found is None and lookup.candidates == ()


def test_a_checksum_that_disagrees_rules_a_file_out_even_when_its_date_matches(tmp_path: pathlib.Path) -> None:
    candidate = tmp_path / "kick.wav"
    db = library((candidate, b"RIFF-a", 100.0))
    wrong_crc = (file_crc(candidate) + 1) % 0x10000

    lookup = find(db, Evidence("kick.wav", pathlib.Path("D:/kick.wav"), size=6, mtime=100, crc=wrong_crc))

    assert lookup.found is None


def test_a_reference_with_a_size_or_date_needs_a_file_that_agrees_with_one(tmp_path: pathlib.Path) -> None:
    candidate = tmp_path / "kick.wav"
    db = library((candidate, b"RIFF-a", 100.0))
    original = pathlib.Path("D:/kick.wav")

    assert find(db, Evidence("kick.wav", original, mtime=100)).found == candidate
    assert find(db, Evidence("kick.wav", original, size=6)).found == candidate
    # Neither agrees, nothing contradicts: still not enough to call it the same file.
    assert find(db, Evidence("kick.wav", original, mtime=5)).found is None


def test_a_size_of_zero_is_no_size(tmp_path: pathlib.Path) -> None:
    candidate = tmp_path / "kick.wav"
    db = library((candidate, b"RIFF-a", 100.0))

    evidence = Evidence("kick.wav", pathlib.Path("D:/kick.wav"), size=0, crc=7)

    assert (evidence.size, evidence.crc) == (None, None)
    assert find(db, evidence).found == candidate


def test_a_name_alone_picks_only_a_file_no_other_shares(tmp_path: pathlib.Path) -> None:
    first, second = tmp_path / "A" / "kick.wav", tmp_path / "B" / "kick.wav"
    db = library((first, b"one", 1.0), (second, b"two!", 2.0))

    lookup = find(db, Evidence("kick.wav", pathlib.Path("D:/kick.wav")))

    assert lookup.found is None
    assert set(lookup.candidates) == {first, second}
    assert find(library((first, b"one", 1.0)), Evidence("kick.wav", pathlib.Path("D:/kick.wav"))).found == first


# Loops cut from one recording: the same first 16 KiB, so the same size and checksum, and different files.
SHARED_START = b"RIFF" + bytes(MAX_CRC_SIZE)


def renamed_evidence(body: bytes, crc: int, mtime: int | None = None) -> Evidence:
    return Evidence("hat 1.wav", pathlib.Path("D:/Old/Hats/hat 1.wav"), size=len(body), mtime=mtime, crc=crc)


def test_several_different_renamed_files_with_its_size_and_checksum_are_refused(tmp_path: pathlib.Path) -> None:
    first, second = tmp_path / "Hats" / "hat 01.wav", tmp_path / "Hats" / "hat 02.wav"
    db = library((first, SHARED_START + b"a", 1.0), (second, SHARED_START + b"b", 2.0))
    assert file_crc(first) == file_crc(second)

    lookup = find(db, renamed_evidence(SHARED_START + b"a", file_crc(first)))

    assert lookup.found is None
    assert set(lookup.candidates) == {first, second}


def test_a_stored_date_picks_one_of_several_renamed_files(tmp_path: pathlib.Path) -> None:
    first, second = tmp_path / "Hats" / "hat 01.wav", tmp_path / "Hats" / "hat 02.wav"
    db = library((first, SHARED_START + b"a", 1.0), (second, SHARED_START + b"b", 2.0))

    assert find(db, renamed_evidence(SHARED_START + b"b", file_crc(second), mtime=2)).found == second


def test_copies_of_one_renamed_file_are_all_the_file(tmp_path: pathlib.Path) -> None:
    first, second = tmp_path / "A" / "hat 01.wav", tmp_path / "B" / "hat 01 copy.wav"
    db = library((first, SHARED_START + b"a", 1.0), (second, SHARED_START + b"a", 2.0))

    assert find(db, renamed_evidence(SHARED_START + b"a", file_crc(first))).found in (first, second)


def test_collecting_one_sample_for_many_references_copies_it_once(tmp_path: pathlib.Path) -> None:
    source = tmp_path / "Library" / "kick.wav"
    library((source, b"RIFF-a", 1.0))
    collector = Collector(tmp_path / "Project")

    first = collector.plan(source, COLLECT_FOLDER)
    again = collector.plan(source, COLLECT_FOLDER)

    assert first == again == tmp_path / "Project" / "Samples" / "Imported" / "kick.wav"
    assert first is not None and not first.exists()
    collector.run()
    assert first.read_bytes() == b"RIFF-a"
    # A rerun finds the copy already in place and plans nothing.
    rerun = Collector(tmp_path / "Project")
    assert rerun.plan(source, COLLECT_FOLDER) == first and rerun.copies == {}


def test_two_sets_collecting_different_files_of_one_name_never_overwrite_each_other(tmp_path: pathlib.Path) -> None:
    """Two sets of one project, collected by two workers: both plan the free name, only the first copy lands."""
    source, other = tmp_path / "PackA" / "kick.wav", tmp_path / "PackB" / "kick.wav"
    library((source, b"RIFF-a", 1.0), (other, b"RIFF-b", 1.0))
    first, second = Collector(tmp_path / "Project"), Collector(tmp_path / "Project")
    destination = first.plan(source, COLLECT_FOLDER)
    assert destination is not None and second.plan(other, COLLECT_FOLDER) == destination

    first.run()
    with pytest.raises(FileExistsError, match="a different kick.wav"):
        second.run()
    assert destination.read_bytes() == b"RIFF-a"


def test_collecting_never_replaces_a_different_file_of_the_same_name(tmp_path: pathlib.Path) -> None:
    source, other = tmp_path / "Library" / "kick.wav", tmp_path / "Elsewhere" / "kick.wav"
    library((source, b"RIFF-a", 1.0), (other, b"RIFF-b", 1.0))
    in_place = tmp_path / "Project" / "Samples" / "Imported" / "kick.wav"
    in_place.parent.mkdir(parents=True)
    in_place.write_bytes(b"RIFF-b")

    clashing = Collector(tmp_path / "Project")
    assert clashing.plan(source, COLLECT_FOLDER) is None
    (refusal,) = clashing.refused
    assert str(source) in refusal.what and "a different file named kick.wav" in refusal.why

    planned = Collector(tmp_path / "Fresh")
    assert planned.plan(source, COLLECT_FOLDER) is not None
    assert planned.plan(other, COLLECT_FOLDER) is None


def test_a_moved_plugin_kit_is_found_by_name_in_the_plugin_files_database(tmp_path: pathlib.Path) -> None:
    library = tmp_path / "moved" / "Kits"
    library.mkdir(parents=True)
    kit = library / "Fastplant Kit.mxgrp"
    kit.write_bytes(b"kit")
    db_path = tmp_path / "catalog.sqlite3"
    create_or_update_db([str(tmp_path / "moved")], db_path)
    old = pathlib.Path("C:/Old Drive/Kits/Fastplant Kit.mxgrp")

    with load_plugin_files_db(plugin_files_db_path(db_path)) as plugin_files:
        lookup = find(plugin_files, Evidence(kit.name, old))

    assert lookup.found == kit.resolve()
