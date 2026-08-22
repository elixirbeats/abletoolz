"""Sample fixing against the database: matching rules that regressed silently."""

from __future__ import annotations

import pathlib
from xml.etree import ElementTree as ET

import pytest

from abletoolz.live_set import AbletonSet
from abletoolz.utils import parse_hex_path

SKELETONS = pathlib.Path(__file__).parent / "version_fixtures" / "skeletons"
GENERATED = pathlib.Path(__file__).parent / "version_fixtures" / "generated"


def test_fix_matches_float_mtime_db_entry(tmp_path: pathlib.Path) -> None:
    """The DB stores st_mtime as a json float; a name+mtime match must still fix the ref.

    Regression: the matcher only accepted mtime as a digit string, so DB entries
    (floats) never matched and fixing fell back to size alone - which is often 0.
    """
    ableton_set = AbletonSet(SKELETONS / "11.2.10.als")
    assert ableton_set.parse()
    missing = ableton_set.samples.check()
    assert missing  # scrubbed fixture paths never exist on disk
    target = missing[0]

    replacement = tmp_path / target.name
    db = {
        str(replacement): {
            "name": target.name,
            "size": -1,  # force the mtime path
            "last_modified": float(target.last_modified),
        }
    }
    ableton_set.samples.fix(db)
    assert target.absolute_element.get("Value") == str(replacement)


def test_fix_ignores_wrong_mtime(tmp_path: pathlib.Path) -> None:
    ableton_set = AbletonSet(SKELETONS / "11.2.10.als")
    assert ableton_set.parse()
    target = ableton_set.samples.check()[0]
    original_value = target.absolute_element.get("Value")

    db = {
        str(tmp_path / target.name): {
            "name": target.name,
            "size": -1,
            "last_modified": float(target.last_modified) + 999.0,
        }
    }
    ableton_set.samples.fix(db)
    assert target.absolute_element.get("Value") == original_value


def test_fix_returns_false_when_nothing_fixed(tmp_path: pathlib.Path) -> None:
    """A fix run that matched nothing must say so, not report success."""
    ableton_set = AbletonSet(SKELETONS / "11.2.10.als")
    assert ableton_set.parse()
    assert ableton_set.samples.check()
    assert ableton_set.samples.fix({}) is False


@pytest.mark.parametrize("key", ["9.0.1", "10.0.1"])
def test_pre11_set_relative_survives_serialization(key: str) -> None:
    """Pre-11 collect-and-save rewrites RelativePathElement children; the rebuilt
    elements must serialize.

    Regression: the rebuilt attributes carried an int Id, which ElementTree only
    rejects at write time - after the original file was already moved to backup.
    A deeper path than the original must not crash either.
    """
    ableton_set = AbletonSet(SKELETONS / f"{key}.als")
    assert ableton_set.parse()
    ref = next(ableton_set.samples.iterate())
    ref.set_relative("Samples/Imported/Extra/Deep/sample.wav")
    ref.set_relative_type(3)
    ableton_set.generate_xml()  # raised TypeError before the fix


def test_save_set_keeps_original_when_serialization_fails(tmp_path: pathlib.Path) -> None:
    """A tree that cannot serialize must fail before the original is moved to backup.

    Regression: save_set backed up (moved) the original first and serialized inside
    the writer thread - a bad tree left no set file at all.
    """
    copy = tmp_path / "poison.als"
    copy.write_bytes((SKELETONS / "11.2.10.als").read_bytes())
    ableton_set = AbletonSet(copy)
    assert ableton_set.parse()
    ableton_set.get_file_times()
    ableton_set.root.find("LiveSet").set("Poison", 0)  # type: ignore[union-attr, arg-type]
    with pytest.raises(TypeError):
        ableton_set.save_set()
    assert copy.exists()  # original still in place, nothing moved to backup
    assert not (tmp_path / "abletoolz_backup").exists()


# -- references that store no modification date -----------------------------
#
# Measured over 811 sets: 98 SampleRefs in 14 of them have no LastModDate at
# all, and every one of the 14 used to fail to parse outright -- one such ref
# took the whole set down, including the refs that were fine. Every one of the
# 14 is the output of a third-party set generator; no set Live
# wrote is missing a date on any of its 76,906 references. Two shapes make up
# the 98, and they must not be handled alike.
#
# abletoolz writes sets itself, so reading what another tool wrote is in scope
# on its own terms. The fixture lives under ``generated/`` rather than in the
# version matrix, because it can testify to a generator's output and not to
# what any version of Live does.


def test_a_set_of_pack_references_parses_at_all() -> None:
    """A Pack reference has no LastModDate; the set still has to load.

    Regression: SampleRef.from_element required LastModDate and pre-11 Data, so
    all 13 generated sets of this shape in the corpus raised ElementNotFound on
    every sample command instead of reporting what they hold.
    """
    ableton_set = AbletonSet(GENERATED / "set_generator_9_7_7.als")
    assert ableton_set.parse()
    refs = list(ableton_set.samples.iterate())
    assert len(refs) == 4
    packs = [ref for ref in refs if ref.pack_resolved]
    assert len(packs) == 2
    for ref in packs:
        assert ref.last_modified is None
        assert ref.absolute is None  # a Pack reference stores no path at all
        assert ref.live_pack == "Core Library"


def test_a_pack_reference_is_not_reported_missing() -> None:
    """Live loads it out of the Pack, so no path on this machine can condemn it."""
    ableton_set = AbletonSet(GENERATED / "set_generator_9_7_7.als")
    assert ableton_set.parse()
    missing = ableton_set.samples.check()
    assert [ref.pack_resolved for ref in missing] == [False, False]
    # The two ordinary refs in the same set are still reported: the skip is
    # about how a reference is addressed, not about where the set came from.
    assert len(missing) == 2


def test_a_pack_reference_is_never_rewritten_to_a_path(tmp_path: pathlib.Path) -> None:
    """Rewriting one would break a reference that works."""
    ableton_set = AbletonSet(GENERATED / "set_generator_9_7_7.als")
    assert ableton_set.parse()
    pack = next(ref for ref in ableton_set.samples.iterate() if ref.pack_resolved)
    before = ET.tostring(pack.sample_ref)

    db = {str(tmp_path / pack.name): {"name": pack.name, "size": 0, "last_modified": 0.0}}
    ableton_set.samples.fix(db)
    assert ET.tostring(pack.sample_ref) == before


def test_a_reference_with_a_path_but_no_date_is_still_checked_and_fixed(tmp_path: pathlib.Path) -> None:
    """The other shape of the 98: a real path, no date, and repairable by size.

    74 of them sit in one generated set, 55 of those FL Studio imports that kept
    a ``%FLStudioFactoryData%`` path but none of the file's metadata. A missing
    date must not make one look like a Pack reference, because that would skip
    a sample that really is missing and really can be found. Set up here on a
    real Live 9.0.1 skeleton, since the shape has to stay repairable wherever it
    turns up.
    """
    ableton_set = AbletonSet(SKELETONS / "9.0.1.als")
    assert ableton_set.parse()
    for element in ableton_set.root.iter("SampleRef"):
        for date in element.findall("LastModDate"):
            element.remove(date)

    target = next(ref for ref in ableton_set.samples.iterate() if ref.size)
    assert target.last_modified is None
    assert not target.pack_resolved  # it kept its path, so it is an ordinary ref
    assert target in ableton_set.samples.check()

    replacement = tmp_path / target.name
    ableton_set.samples.fix({str(replacement): {"name": target.name, "size": target.size}})
    assert parse_hex_path(target.absolute_element.text or "") == str(replacement)
