"""Save-path behaviors: file times and plain-XML sets."""

from __future__ import annotations

import gzip
import os
import pathlib

import pytest
from conftest import SKELETONS, copy_set

from abletoolz.live_set import AbletonSet, persistence


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
    copy = copy_set(tmp_path, "11.3.42", "times.als")
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
    assert commands == [["SetFile", "-d", persistence.format_date(CREATED), str(ableton_set.path)]]
