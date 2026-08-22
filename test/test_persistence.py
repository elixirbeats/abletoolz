"""Save-path behaviors: filename modifications, file times, and re-run stability."""

from __future__ import annotations

import gzip
import os
import pathlib

import pytest

from abletoolz import utils
from abletoolz.live_set import AbletonSet, persistence

SKELETONS = pathlib.Path(__file__).parent / "version_fixtures" / "skeletons"


def test_save_append_bars_bpm_is_self_sufficient_and_rerun_stable(tmp_path: pathlib.Path) -> None:
    """--append-bars-bpm computes bpm/bars itself, and a second run keeps the name stable."""
    copy = tmp_path / "mytune.als"
    copy.write_bytes((SKELETONS / "11.3.42.als").read_bytes())

    ableton_set = AbletonSet(copy)
    assert ableton_set.parse()
    ableton_set.get_file_times()
    ableton_set.save_set(append_bars_bpm=True)  # no prior bpm/furthest_bar calls
    renamed = ableton_set.path
    assert renamed.name == f"mytune_{ableton_set.furthest_bar}bars_{ableton_set.bpm:.2f}bpm.als"
    assert renamed.exists()

    again = AbletonSet(renamed)
    assert again.parse()
    again.get_file_times()
    again.save_set(append_bars_bpm=True)
    assert again.path == renamed  # suffix replaced, not stacked
    assert again.path.exists()


def test_append_bars_bpm_keeps_file_extension(tmp_path: pathlib.Path) -> None:
    """Renaming preserves the original suffix — .alc clips must not become .als."""
    copy = tmp_path / "myclip.alc"
    copy.write_bytes((SKELETONS / "11.3.42.als").read_bytes())
    ableton_set = AbletonSet(copy)
    assert ableton_set.parse()
    ableton_set.get_file_times()
    ableton_set.save_set(append_bars_bpm=True)
    assert ableton_set.path.suffix == ".alc"
    assert ableton_set.path.exists()


def test_parse_plain_xml_set(tmp_path: pathlib.Path) -> None:
    """Hand-unzipped .als files are plain XML; Live opens them, so must we."""
    xml_copy = tmp_path / "unzipped.als"
    xml_copy.write_bytes(gzip.decompress((SKELETONS / "11.3.42.als").read_bytes()))
    ableton_set = AbletonSet(xml_copy)
    assert ableton_set.parse()
    assert ableton_set.version_tuple == (11, 3, 42)


# -- file times -------------------------------------------------------------
# The creation time is the one a set can be missing: a filesystem that reports
# no birth time leaves it None, and every branch that restores it has to say so.

CREATED = 1_600_000_000.0


def timed_set(tmp_path: pathlib.Path) -> AbletonSet:
    """A set on disk with both of its file times read."""
    copy = tmp_path / "times.als"
    copy.write_bytes((SKELETONS / "11.3.42.als").read_bytes())
    ableton_set = AbletonSet(copy)
    assert ableton_set.parse()
    ableton_set.get_file_times()
    return ableton_set


def test_restore_file_times_without_a_creation_time_restores_the_time_it_has(tmp_path: pathlib.Path) -> None:
    """No creation time is not an error: the modification time still goes back."""
    ableton_set = timed_set(tmp_path)
    modified = ableton_set.last_modification_time
    assert modified is not None
    ableton_set.creation_time = None
    os.utime(ableton_set.path, (0, 0))

    persistence.restore_file_times(ableton_set)
    assert ableton_set.path.stat().st_mtime == pytest.approx(modified)


def test_macos_asks_setfile_for_no_date_when_there_is_no_creation_time(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SetFile is only worth calling with a date, and formatting None raises."""
    ableton_set = timed_set(tmp_path)
    ableton_set.creation_time = None
    commands: list[list[str]] = []
    monkeypatch.setattr(persistence.sys, "platform", "darwin")
    monkeypatch.setattr(persistence.subprocess, "run", lambda command, **_: commands.append(command))

    persistence.restore_file_times(ableton_set)
    assert commands == []


def test_macos_hands_setfile_the_creation_date_it_has(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ableton_set = timed_set(tmp_path)
    ableton_set.creation_time = CREATED
    commands: list[list[str]] = []
    monkeypatch.setattr(persistence.sys, "platform", "darwin")
    monkeypatch.setattr(persistence.subprocess, "run", lambda command, **_: commands.append(command))

    persistence.restore_file_times(ableton_set)
    assert commands == [["SetFile", "-d", utils.format_date(CREATED), str(ableton_set.path)]]
