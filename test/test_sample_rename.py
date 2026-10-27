"""Repair renamed samples through indexed size/CRC matches and versioned FileRefs."""

from __future__ import annotations

import copy
import gzip
import os
import pathlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal
from unittest.mock import patch
from xml.etree import ElementTree as ET

import pytest
from conftest import skeleton_set, stored_records

from abletoolz.live_set import AbletonSet
from abletoolz.live_set.sample_ref import RelativePathType, SampleRef
from abletoolz.samples import database, relink
from abletoolz.samples.catalog import SampleCatalog, SampleRecord
from abletoolz.samples.crc import file_crc

VERSIONS = ("9.0.1", "10.0.1", "11.2.10")
type CatalogKind = Literal["memory", "sqlite"]
KINDS: tuple[CatalogKind, ...] = ("memory", "sqlite")
DATA = bytes(range(48))


def set_reference_metadata(reference: SampleRef, size: int, crc: int) -> None:
    file_ref = reference.sample_ref.find("FileRef")
    assert file_ref is not None
    modern = reference.version_tuple >= (11, 0, 0)
    for tag, value in (("OriginalFileSize" if modern else "FileSize", size), ("OriginalCrc" if modern else "Crc", crc)):
        element = file_ref.find(f".//{tag}")
        if element is None:
            element = ET.SubElement(file_ref, tag)
        element.set("Value", str(value))
    reference.size, reference.crc = size, crc
    reference.last_modified = 0
    date = reference.sample_ref.find("LastModDate")
    if date is not None:
        date.set("Value", "0")


def reparsed(root: ET.Element, project: pathlib.Path) -> tuple[AbletonSet, SampleRef]:
    """A set in ``project`` whose document is ``root``, its one sample reference read afresh."""
    live_set = AbletonSet.from_root(root, project / "Song.als", project_root=project)
    (reference,) = live_set.samples.iterate()
    return live_set, reference


def missing_reference(version: str, project: pathlib.Path) -> tuple[AbletonSet, SampleRef]:
    """A set in ``project`` holding nothing but the fixture's first missing sample reference."""
    source = skeleton_set(version)
    root = ET.Element("Ableton", Creator=source.root.get("Creator", ""))
    root.append(source.samples.check()[0].sample_ref)
    return reparsed(root, project)


@contextmanager
def catalog_for(root: pathlib.Path, destination: pathlib.Path, kind: CatalogKind) -> Iterator[SampleCatalog]:
    database.create_or_update_db([str(root)], destination)
    if kind == "memory":
        yield SampleCatalog.from_records(stored_records(destination))
    else:
        with database.load_db(destination) as catalog:
            yield catalog


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("collect", [False, True])
def test_renamed_sample_survives_repair_save_and_reload(
    tmp_path: pathlib.Path, version: str, kind: CatalogKind, collect: bool
) -> None:
    project = tmp_path / "Project"
    project.mkdir()
    library = tmp_path / "library"
    library.mkdir()
    replacement = library / "new-sample-name.wav"
    replacement.write_bytes(DATA)
    live_set, reference = missing_reference(version, project)
    set_reference_metadata(reference, len(DATA), file_crc(replacement))
    reference._set_relative_type(RelativePathType.ABSOLUTE)
    with catalog_for(library, tmp_path / "catalog.sqlite3", kind) as catalog:
        collector = relink.Collector(project) if collect else None
        assert live_set.samples.fix(catalog, collector)
        if collector is not None:
            collector.run()
    assert live_set.samples.check() == []
    assert reference.name == replacement.name

    saved = project / "repaired.als"
    with gzip.open(saved, "wb") as file:
        file.write(live_set.generate_xml())
    reloaded = AbletonSet(saved, project)
    assert reloaded.parse()
    repaired = next(ref for ref in reloaded.samples.iterate() if ref.name == replacement.name)
    assert repaired.absolute == replacement
    if collect:
        assert repaired.get_relative_type() == RelativePathType.PROJECT
        assert repaired.relative_exists
        assert (project / "Samples" / "Imported" / replacement.name).read_bytes() == DATA
    else:
        assert repaired.get_relative_type() == RelativePathType.ABSOLUTE
        assert repaired.absolute_exists


@pytest.mark.parametrize("version", VERSIONS)
def test_zero_live_crc_warns_and_cannot_repair_a_renamed_sample(
    tmp_path: pathlib.Path, version: str, caplog: pytest.LogCaptureFixture
) -> None:
    live_set, original = missing_reference(version, tmp_path)
    set_reference_metadata(original, len(DATA), 0)
    live_set, reference = reparsed(live_set.root, tmp_path)
    assert reference.crc is None
    assert "stores CRC 0" in caplog.text
    replacement = tmp_path / "new-name.wav"
    replacement.write_bytes(DATA)
    record: SampleRecord = {"name": replacement.name, "size": len(DATA), "crc": 0, "last_modified": 0}
    assert not live_set.samples.fix(SampleCatalog.from_records({str(replacement): record}))
    assert reference.name == original.name
    assert not reference.absolute_exists


@pytest.mark.parametrize("version", VERSIONS)
def test_zero_live_crc_still_allows_matching_by_name_and_size(tmp_path: pathlib.Path, version: str) -> None:
    live_set, original = missing_reference(version, tmp_path)
    set_reference_metadata(original, len(DATA), 0)
    live_set, reference = reparsed(live_set.root, tmp_path)
    replacement = tmp_path / reference.name
    replacement.write_bytes(DATA)
    assert live_set.samples.fix(
        SampleCatalog.from_records(
            {
                str(replacement): {
                    "name": reference.name,
                    "size": len(DATA),
                    "crc": file_crc(replacement),
                }
            }
        )
    )
    assert reference.absolute_exists


@pytest.mark.parametrize(
    "size,crc", [(None, "right"), (0, "right"), (48, "none"), (48, "zero"), (49, "right"), (48, "wrong")]
)
def test_renamed_lookup_requires_both_size_and_nonzero_crc(tmp_path: pathlib.Path, size: int | None, crc: str) -> None:
    replacement = tmp_path / "renamed.wav"
    replacement.write_bytes(DATA)
    actual = file_crc(replacement)
    catalog = SampleCatalog.from_records(
        {str(replacement): {"name": replacement.name, "size": len(DATA), "crc": actual}}
    )
    original = tmp_path / "original.wav"
    stored = {"right": actual, "wrong": actual ^ 1, "none": None, "zero": 0}[crc]
    assert relink.find(catalog, relink.Evidence(original.name, original, size=size, crc=stored)).candidates == ()
    found = relink.find(catalog, relink.Evidence(original.name, original, size=len(DATA), crc=actual))
    assert found.candidates == (replacement,)


def test_zero_crc_does_not_override_normal_filename_ranking(tmp_path: pathlib.Path) -> None:
    near = tmp_path / "Drums" / "kick.wav"
    far = tmp_path / "Other" / "kick.wav"
    for path in (near, far):
        path.parent.mkdir()
        path.write_bytes(DATA)
    catalog = SampleCatalog.from_records(
        {
            str(near): {"name": near.name, "size": 48, "crc": 17},
            str(far): {"name": far.name, "size": 48, "crc": 0},
        }
    )
    with patch.object(catalog, "candidates", wraps=catalog.candidates) as query:
        assert relink.find(catalog, relink.Evidence("kick.wav", near, size=48, crc=0)).candidates == (near, far)
    assert query.call_args.kwargs["crc"] is None, "Queried an unknown CRC"


@pytest.mark.parametrize("kind", KINDS)
def test_exact_renamed_match_beats_weaker_same_name_and_skips_offline_entries(
    tmp_path: pathlib.Path, kind: CatalogKind
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    named = library / "original.wav"
    renamed = library / "renamed.wav"
    named.write_bytes(DATA[:-1] + b"other")
    renamed.write_bytes(DATA)
    with catalog_for(library, tmp_path / "catalog.sqlite3", kind) as catalog:
        evidence = relink.Evidence(named.name, named, size=len(DATA), crc=file_crc(renamed))
        # The same-named file stores another size, so only the renamed one is the reference's file.
        assert relink.find(catalog, evidence).candidates == (renamed,)
        named.unlink()
        assert relink.find(catalog, evidence).found == renamed


@pytest.mark.parametrize("kind", KINDS)
def test_checksum_lookup_uses_indexes_and_prefers_original_name_for_duplicate_content(
    tmp_path: pathlib.Path, kind: CatalogKind
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    renamed = library / "renamed.wav"
    named = library / "original.wav"
    for path in (renamed, named):
        path.write_bytes(DATA)
    with catalog_for(library, tmp_path / "catalog.sqlite3", kind) as catalog:
        lookup = relink.find(catalog, relink.Evidence(named.name, renamed, size=len(DATA), crc=file_crc(named)))
        assert lookup.candidates == (named, renamed)
        assert lookup.found == named


def test_renamed_duplicates_use_folder_context(tmp_path: pathlib.Path) -> None:
    near = tmp_path / "Packs" / "Drums" / "renamed.wav"
    far = tmp_path / "Other" / "alias.wav"
    for path in (near, far):
        path.parent.mkdir(parents=True)
        path.write_bytes(DATA)
    crc = file_crc(near)
    records: dict[str, SampleRecord] = {
        str(path): {"name": path.name, "size": len(DATA), "crc": crc} for path in (far, near)
    }
    evidence = relink.Evidence("original.wav", pathlib.Path("D:/Old/Packs/Drums/original.wav"), size=len(DATA), crc=crc)
    assert relink.find(SampleCatalog.from_records(records), evidence).candidates == (near, far)


def test_a_previous_weak_match_cannot_hide_a_later_renamed_crc_match(tmp_path: pathlib.Path) -> None:
    source, first = missing_reference("11.2.10", tmp_path)
    source.root.append(copy.deepcopy(first.sample_ref))
    live_set = AbletonSet.from_root(source.root, tmp_path / "Song.als", project_root=tmp_path)
    first, second = live_set.samples.iterate()
    library = tmp_path / "library"
    library.mkdir()
    named = library / first.name
    renamed = library / "renamed.wav"
    named.write_bytes(DATA)
    renamed.write_bytes(DATA[:-1] + b"X")
    for path in (named, renamed):
        os.utime(path, ns=(0, 0))
    set_reference_metadata(first, len(DATA), file_crc(named))
    set_reference_metadata(second, len(DATA), file_crc(renamed))
    with catalog_for(library, tmp_path / "catalog.sqlite3", "sqlite") as catalog:
        assert live_set.samples.fix(catalog)
    assert first.absolute == named
    assert second.absolute == renamed


def test_collecting_renamed_sample_refuses_same_size_different_content(tmp_path: pathlib.Path) -> None:
    project = tmp_path / "Project"
    project.mkdir()
    live_set, reference = missing_reference("11.2.10", project)
    reference._set_relative_type(RelativePathType.ABSOLUTE)
    library = tmp_path / "library"
    library.mkdir()
    replacement = library / "renamed.wav"
    replacement.write_bytes(DATA)
    set_reference_metadata(reference, len(DATA), file_crc(replacement))
    destination = project / "Samples" / "Imported" / replacement.name
    destination.parent.mkdir(parents=True)
    wrong_content = DATA[:-1] + b"X"
    destination.write_bytes(wrong_content)
    original_xml = ET.tostring(reference.sample_ref)
    with catalog_for(library, tmp_path / "catalog.sqlite3", "sqlite") as catalog:
        assert not live_set.samples.fix(catalog, relink.Collector(project))
    assert destination.read_bytes() == wrong_content
    assert ET.tostring(reference.sample_ref) == original_xml


@pytest.mark.parametrize("change", ["size", "content"])
def test_repair_rechecks_renamed_file_after_catalog_changes(tmp_path: pathlib.Path, change: str) -> None:
    live_set, reference = missing_reference("11.2.10", tmp_path)
    library = tmp_path / "library"
    library.mkdir()
    replacement = library / "renamed.wav"
    replacement.write_bytes(DATA)
    set_reference_metadata(reference, len(DATA), file_crc(replacement))
    original_xml = ET.tostring(reference.sample_ref)
    with catalog_for(library, tmp_path / "catalog.sqlite3", "sqlite") as catalog:
        replacement.write_bytes(DATA + b"longer" if change == "size" else DATA[:-1] + b"X")
        assert not live_set.samples.fix(catalog)
    assert ET.tostring(reference.sample_ref) == original_xml


@pytest.mark.parametrize("size,crc", [(48.9, 17), (48, float("inf")), (48, "17"), ("48", 17)])
def test_invalid_checksum_metadata_cannot_identify_a_renamed_file(
    tmp_path: pathlib.Path, size: str | int | float, crc: str | int | float
) -> None:
    record: SampleRecord = {"name": "renamed.wav", "size": size, "crc": crc}
    assert not relink.Evidence("original.wav", tmp_path / "original.wav", size=48, crc=17).same_bytes(record)
