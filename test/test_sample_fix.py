"""Sample fixing against the database: matching rules that regressed silently."""

from __future__ import annotations

import pathlib

import pytest

from abletoolz.live_set import AbletonSet

SKELETONS = pathlib.Path(__file__).parent / "version_fixtures" / "skeletons"


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
