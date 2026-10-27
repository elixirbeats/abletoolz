"""Sample fixing against the database: matching rules that regressed silently."""

from __future__ import annotations

import pathlib
from collections.abc import Callable
from xml.etree import ElementTree as ET

import pytest
from conftest import copy_set, skeleton_set

from abletoolz.formats import hex_text, live_document, path_data
from abletoolz.live_set import AbletonSet
from abletoolz.live_set.sample_ref import RelativePathType, Resolution, SampleRef
from abletoolz.samples.catalog import SampleCatalog


def test_fix_matches_float_mtime_db_entry(tmp_path: pathlib.Path) -> None:
    """The DB stores st_mtime as a json float; a name+mtime match must still fix the ref.

    Regression: the matcher only accepted mtime as a digit string, so DB entries
    (floats) never matched and fixing fell back to size alone - which is often 0.
    """
    ableton_set = skeleton_set("11.2.10")
    missing = ableton_set.samples.check()
    assert missing  # scrubbed fixture paths never exist on disk
    target = missing[0]

    replacement = tmp_path / target.name
    replacement.touch()
    db = {
        str(replacement): {
            "name": target.name,
            "last_modified": float(target.last_modified),
        }
    }
    ableton_set.samples.fix(SampleCatalog.from_records(db))
    assert target.absolute_element.get("Value") == replacement.as_posix()


def test_fix_ignores_wrong_mtime(tmp_path: pathlib.Path) -> None:
    ableton_set = skeleton_set("11.2.10")
    target = ableton_set.samples.check()[0]
    original_value = target.absolute_element.get("Value")

    db = {
        str(tmp_path / target.name): {
            "name": target.name,
            "last_modified": float(target.last_modified) + 999.0,
        }
    }
    ableton_set.samples.fix(SampleCatalog.from_records(db))
    assert target.absolute_element.get("Value") == original_value


def test_fix_returns_false_when_nothing_fixed(tmp_path: pathlib.Path) -> None:
    """A fix run that matched nothing must say so, not report success."""
    ableton_set = skeleton_set("11.2.10")
    assert ableton_set.samples.check()
    assert ableton_set.samples.fix(SampleCatalog.from_records({})) == []


def test_fix_skips_offline_catalog_entries(tmp_path: pathlib.Path) -> None:
    ableton_set = skeleton_set("11.2.10")
    target = ableton_set.samples.check()[0]
    available = tmp_path / target.name
    available.touch()
    offline = tmp_path / "Offline" / target.name
    target.absolute = offline
    records = {str(path): {"name": target.name, "size": target.size} for path in [offline, available]}

    gone: set[pathlib.Path] = set()
    assert ableton_set.samples.fix(SampleCatalog.from_records(records), gone=gone)
    assert target.absolute_element.get("Value") == available.as_posix()
    assert gone == {offline}


def test_a_relink_warns_once_per_set_about_files_gone_from_the_database(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    ableton_set = skeleton_set("11.2.10")
    missing = ableton_set.samples.check()
    records = {str(tmp_path / "Unplugged" / ref.name): {"name": ref.name, "size": ref.size} for ref in missing}

    ableton_set.files.relink(SampleCatalog.from_records(records), collect=False)

    warnings = [record.getMessage() for record in caplog.records if "--db-prune" in record.getMessage()]
    assert len(warnings) == 1
    assert f"{len(records)} file(s)" in warnings[0]


@pytest.mark.parametrize("key", ["9.0.1", "10.0.1"])
def test_pre11_set_relative_survives_serialization(key: str) -> None:
    """Pre-11 collect-and-save rewrites RelativePathElement children; the rebuilt
    elements must serialize.

    Regression: the rebuilt attributes carried an int Id, which ElementTree only
    rejects at write time - after the original file was already moved to backup.
    A deeper path than the original must not crash either.
    """
    ableton_set = skeleton_set(key)
    ref = next(ableton_set.samples.iterate())
    ref.point_relative(pathlib.PurePath("Samples/Imported/Extra/Deep/sample.wav"), RelativePathType.PROJECT)
    ableton_set.generate_xml()  # raised TypeError before the fix


def test_save_set_keeps_original_when_serialization_fails(tmp_path: pathlib.Path) -> None:
    """A tree that cannot serialize must fail before the original is moved to backup.

    Regression: save_set backed up (moved) the original first and serialized inside
    the writer thread - a bad tree left no set file at all.
    """
    copy = copy_set(tmp_path, "11.2.10", "poison.als")
    ableton_set = AbletonSet(copy)
    assert ableton_set.parse()
    ableton_set.get_file_times()
    ableton_set.root.find("LiveSet").set("Poison", 0)  # type: ignore[union-attr, arg-type]
    with pytest.raises(TypeError):
        ableton_set.save_set()
    assert copy.exists()  # original still in place, nothing moved to backup
    assert not (tmp_path / "abletoolz_backup").exists()


def test_a_failed_write_raises_and_leaves_the_original_in_place(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The original is copied to the backup and only replaced in one step, so a failed write leaves it as it was."""
    copy = copy_set(tmp_path, "11.2.10", "full disk.als")
    before = copy.read_bytes()
    ableton_set = AbletonSet(copy)
    assert ableton_set.parse()
    ableton_set.get_file_times()

    def disk_full(path: pathlib.Path, document: bytes) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(live_document, "write", disk_full)
    with pytest.raises(OSError, match="No space"):
        ableton_set.save_set()
    assert copy.read_bytes() == before
    assert (tmp_path / "abletoolz_backup" / "full disk__1.als").read_bytes() == before


def test_two_sets_of_one_name_saved_into_one_output_both_land(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another worker publishing the same name just before this one keeps its copy, and this one takes the next name.

    Regression: both picked out/set.als and the second replace silently overwrote the first.
    """
    copy = copy_set(tmp_path, "11.2.10")
    output = tmp_path / "out"
    other = b"another worker's set"
    raced: list[pathlib.Path] = []

    def racing(publish: Callable[[pathlib.Path, bytes], None]) -> Callable[[pathlib.Path, bytes], None]:
        def publish_after_the_other_worker(path: pathlib.Path, document: bytes) -> None:
            if not raced:
                raced.append(path)
                path.write_bytes(other)
            publish(path, document)

        return publish_after_the_other_worker

    monkeypatch.setattr(live_document, "write", racing(live_document.write))
    monkeypatch.setattr(live_document, "write_new", racing(live_document.write_new))
    ableton_set = AbletonSet(copy)
    assert ableton_set.parse()
    ableton_set.get_file_times()
    ableton_set.save_set(output_dir=output)

    assert raced == [output / "set.als"]
    assert (output / "set.als").read_bytes() == other
    assert ableton_set.path == output / "set__1.als"
    assert AbletonSet(ableton_set.path).parse()


# -- references that store no modification date -----------------------------


def test_a_reference_with_a_path_but_no_date_is_still_checked_and_fixed(tmp_path: pathlib.Path) -> None:
    """A reference with a real path and no date is still checked, and repaired by size.

    A missing date must not make it look like a Pack reference, because that
    would skip a sample that really is missing and really can be found.
    """
    ableton_set = skeleton_set("9.0.1")
    for element in ableton_set.root.iter("SampleRef"):
        for date in element.findall("LastModDate"):
            element.remove(date)

    target = next(ref for ref in ableton_set.samples.iterate() if ref.size)
    assert target.last_modified is None
    assert not target.pack_resolved  # it kept its path, so it is an ordinary ref
    assert target in ableton_set.samples.check()

    replacement = tmp_path / target.name
    replacement.touch()
    ableton_set.samples.fix(SampleCatalog.from_records({str(replacement): {"name": target.name, "size": target.size}}))
    assert path_data.decode(hex_text.decode(target.absolute_element.text)).path == str(replacement)


def test_a_project_reference_found_only_by_its_absolute_path_is_repointed(tmp_path: pathlib.Path) -> None:
    """A set written outside the project its samples came from.

    The absolute path exists, the project-relative one does not, and Live
    reported the sample missing (2026-09-27, recombined songs in a lab
    folder). The check counts it missing and the fixer points Live at the file
    the absolute path names, spelled the way Live spells paths.
    """
    ableton_set = skeleton_set("11.2.10")
    target = next(ref for ref in ableton_set.samples.iterate() if not ref.pack_resolved)
    path_element = target.absolute_element
    assert path_element is not None
    sample = tmp_path / "Samples" / "kick.wav"
    sample.parent.mkdir()
    sample.write_bytes(b"RIFF")
    path_element.set("Value", str(sample))
    target.relative_type_element.set("Value", str(RelativePathType.PROJECT.value))
    root = ET.Element("Ableton", Creator=ableton_set.root.get("Creator", ""))
    root.append(target.sample_ref)
    ableton_set = AbletonSet.from_root(root, ableton_set.path, project_root=tmp_path / "elsewhere")
    (reparsed,) = ableton_set.samples.iterate()
    assert reparsed.resolution is Resolution.ONLY_BY_ABSOLUTE

    assert ableton_set.samples.check() == [reparsed]
    assert ableton_set.samples.fix(SampleCatalog.from_records({}))
    assert path_element.get("Value") == sample.as_posix()
    assert reparsed.get_relative_type() == RelativePathType.ABSOLUTE
    assert reparsed.resolution is Resolution.LOADS
    assert ableton_set.samples.check() == []


def test_a_project_reference_that_resolves_in_its_project_is_left_alone(tmp_path: pathlib.Path) -> None:
    ableton_set = skeleton_set("11.2.10")
    target = next(ref for ref in ableton_set.samples.iterate() if not ref.pack_resolved and ref.relative)
    assert target.relative is not None
    in_project = tmp_path / "project" / target.relative
    in_project.parent.mkdir(parents=True)
    in_project.write_bytes(b"RIFF")
    target.relative_type_element.set("Value", str(RelativePathType.PROJECT.value))
    reparsed = SampleRef(target.sample_ref, ableton_set.version_tuple, tmp_path / "project")
    assert reparsed.relative_exists and reparsed.resolution is Resolution.LOADS


def _same_size_records(
    target_name: str, size: int, near: pathlib.Path, far: pathlib.Path, near_crc: int | None, far_crc: int | None
) -> dict[str, dict[str, str | int | float]]:
    """Two same-name, same-size records; ``near`` wins every heuristic except the crc."""
    db: dict[str, dict[str, str | int | float]] = {}
    for path, crc in ((near, near_crc), (far, far_crc)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        record: dict[str, str | int | float] = {"name": target_name, "size": size, "last_modified": 0.0}
        if crc is not None:
            record["crc"] = crc
        db[str(path)] = record
    return db


def test_fix_prefers_the_file_whose_crc_matches(tmp_path: pathlib.Path) -> None:
    ableton_set = skeleton_set("11.2.10")
    target = ableton_set.samples.check()[0]
    assert target.crc is not None
    near = tmp_path / "a" / target.name
    far = tmp_path / "a_much_longer_folder" / target.name
    db = _same_size_records(target.name, target.size, near, far, target.crc ^ 1, target.crc)

    ableton_set.samples.fix(SampleCatalog.from_records(db))

    assert target.absolute_element is not None
    assert target.absolute_element.get("Value") == far.as_posix()


def test_fix_falls_back_to_size_when_the_database_has_no_crc(tmp_path: pathlib.Path) -> None:
    ableton_set = skeleton_set("11.2.10")
    target = ableton_set.samples.check()[0]
    near = tmp_path / "a" / target.name
    far = tmp_path / "a_much_longer_folder" / target.name
    db = _same_size_records(target.name, target.size, near, far, None, None)

    ableton_set.samples.fix(SampleCatalog.from_records(db))

    assert target.absolute_element is not None
    assert target.absolute_element.get("Value") == near.as_posix()


def test_a_reference_without_a_stored_size_has_no_crc() -> None:
    """Live writes "no checksum" as OriginalCrc 0 beside OriginalFileSize 0 (pre-11: a missing Crc)."""
    ableton_set = skeleton_set("9.0.1")
    unsized = [ref for ref in ableton_set.samples.iterate() if not ref.size]
    assert unsized and all(ref.crc is None for ref in unsized)
