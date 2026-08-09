"""Save-path behaviors: filename modifications and re-run stability."""

from __future__ import annotations

import gzip
import pathlib

from abletoolz.live_set import AbletonSet

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
