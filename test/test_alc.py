r"""Tests for abletoolz.alc against Live-generated ground-truth .alc fixtures.

``audeka_live_saved.alc`` was saved by Live 12 itself (120 BPM default warp).
``audeka_gridfix_verified.alc`` is the same clip with a 172 BPM grid spliced in, anchored
at 66.88s -- this exact file was drag-tested in Live and the grid was honored perfectly.

Note: that fixture's grid anchor (66.88s) was the *drop* in the original experiment, but
``AlcClip.set_grid`` implements the generic, user-decided *start* convention (beat 0 =
song grid start). Driving ``set_grid`` with ``grid_start_seconds=66.88`` reproduces the
fixture's numbers exactly because the underlying two-marker-grid mechanism is identical --
only the semantic meaning of the anchor differs.
"""

from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from abletoolz.alc import AlcClip, AlcError
from abletoolz.live_set import elements_equal

FIXTURES = Path(__file__).parent / "alc_fixtures"
LIVE_SAVED = FIXTURES / "audeka_live_saved.alc"
GRIDFIX_VERIFIED = FIXTURES / "audeka_gridfix_verified.alc"

GRIDFIX_BPM = 172.0
GRIDFIX_ANCHOR_S = 66.88
GRIDFIX_DURATION_S = 320.94476187874625


# ── Round trip ───────────────────────────────────────────────────────────────


def test_round_trip_semantic(tmp_path: Path) -> None:
    """load() -> save() -> load() reproduces the fixture's XML tree semantically."""
    original = AlcClip.load(LIVE_SAVED)
    out_path = tmp_path / "roundtrip.alc"
    original.save(out_path)
    reloaded = AlcClip.load(out_path)
    assert elements_equal(original.root, reloaded.root)


def test_round_trip_untouched_clip_fields(tmp_path: Path) -> None:
    """A load/save round trip changes none of the clip's own fields."""
    clip = AlcClip.load(LIVE_SAVED)
    assert clip.name == "Audeka & Disprove - Militant - 9A(Em)"
    assert clip.color_index == 24
    markers = clip.markers
    assert len(markers) == 2
    assert markers[0].sec_time == 0.0
    assert markers[0].beat_time == 0.0
    assert markers[1].sec_time == pytest.approx(0.00187687687603574739)
    assert markers[1].beat_time == pytest.approx(0.03125)


# ── set_grid reproduces the Live-verified fixture ───────────────────────────


def test_set_grid_matches_verified_fixture() -> None:
    """Applying the drop-anchored grid values reproduces the verified fixture's numbers."""
    expected = AlcClip.load(GRIDFIX_VERIFIED)
    expected_markers = expected.markers
    assert len(expected_markers) == 2
    expected_current_start = expected.clip.find("CurrentStart")
    expected_current_end = expected.clip.find("CurrentEnd")
    assert expected_current_start is not None and expected_current_end is not None
    expected_start_beats = float(expected_current_start.get("Value", ""))
    expected_end_beats = float(expected_current_end.get("Value", ""))

    clip = AlcClip.load(LIVE_SAVED)
    clip.set_grid(bpm=GRIDFIX_BPM, grid_start_seconds=GRIDFIX_ANCHOR_S, duration_seconds=GRIDFIX_DURATION_S)

    markers = clip.markers
    assert len(markers) == 2
    # Marker Ids are preserved from the original saved clip (2 and 3), matching the fixture.
    assert [m.marker_id for m in markers] == [m.marker_id for m in expected_markers]
    assert markers[0].sec_time == pytest.approx(expected_markers[0].sec_time)
    assert markers[0].beat_time == pytest.approx(expected_markers[0].beat_time)
    assert markers[1].sec_time == pytest.approx(expected_markers[1].sec_time)
    assert markers[1].beat_time == pytest.approx(expected_markers[1].beat_time)

    current_start = clip.clip.find("CurrentStart")
    current_end = clip.clip.find("CurrentEnd")
    assert current_start is not None and current_end is not None
    assert float(current_start.get("Value", "")) == pytest.approx(expected_start_beats)
    assert float(current_end.get("Value", "")) == pytest.approx(expected_end_beats)

    # Exact string match too -- proves our float formatting matches Live's serializer.
    assert current_start.get("Value") == expected_current_start.get("Value")
    assert current_end.get("Value") == expected_current_end.get("Value")

    loop = _get_loop(clip)
    expected_loop = _get_loop(expected)
    for tag in ("LoopStart", "LoopEnd", "OutMarker", "HiddenLoopStart", "HiddenLoopEnd"):
        actual_el = loop.find(tag)
        expected_el = expected_loop.find(tag)
        assert actual_el is not None and expected_el is not None
        assert actual_el.get("Value") == expected_el.get("Value")

    is_warped = clip.clip.find("IsWarped")
    assert is_warped is not None
    assert is_warped.get("Value") == "true"


def _get_loop(clip: AlcClip) -> ET.Element:
    loop = clip.clip.find("Loop")
    assert loop is not None
    return loop


# ── Start-convention grid (beat 0 = song grid start, not the drop) ─────────


def test_start_convention_grid_marker_and_range_math() -> None:
    """A fresh start-convention grid produces the documented marker/range formulas."""
    bpm = 140.0
    grid_start_s = 2.5
    duration_s = 180.0

    clip = AlcClip.load(LIVE_SAVED)
    clip.set_grid(bpm=bpm, grid_start_seconds=grid_start_s, duration_seconds=duration_s)

    markers = clip.markers
    assert markers[0].sec_time == pytest.approx(grid_start_s)
    assert markers[0].beat_time == 0.0
    assert markers[1].sec_time == pytest.approx(duration_s)

    expected_end_beats = (duration_s - grid_start_s) * bpm / 60.0
    expected_start_beats = -(grid_start_s * bpm / 60.0)
    assert markers[1].beat_time == pytest.approx(expected_end_beats)

    current_start = clip.clip.find("CurrentStart")
    current_end = clip.clip.find("CurrentEnd")
    assert current_start is not None and current_end is not None
    assert float(current_start.get("Value", "")) == pytest.approx(expected_start_beats)
    assert float(current_end.get("Value", "")) == pytest.approx(expected_end_beats)


def test_set_grid_rejects_bad_inputs() -> None:
    """set_grid validates bpm, grid_start_seconds, and duration_seconds."""
    clip = AlcClip.load(LIVE_SAVED)
    with pytest.raises(ValueError, match="bpm"):
        clip.set_grid(bpm=0.0, grid_start_seconds=0.0, duration_seconds=10.0)
    with pytest.raises(ValueError, match="grid_start_seconds"):
        clip.set_grid(bpm=120.0, grid_start_seconds=-1.0, duration_seconds=10.0)
    with pytest.raises(ValueError, match="duration_seconds"):
        clip.set_grid(bpm=120.0, grid_start_seconds=10.0, duration_seconds=5.0)


# ── drop_alignment ───────────────────────────────────────────────────────────


def test_drop_alignment_on_grid_residual_near_zero() -> None:
    """A drop exactly on a bar boundary has ~0 residual."""
    bpm = 174.0
    grid_start_s = 1.0
    bars_to_drop = 16
    drop_s = grid_start_s + (bars_to_drop * 4.0) * 60.0 / bpm

    result = AlcClip.drop_alignment(bpm=bpm, grid_start_seconds=grid_start_s, drop_seconds=drop_s)
    assert result.bars == bars_to_drop
    assert result.residual_beats == pytest.approx(0.0, abs=1e-9)


def test_drop_alignment_off_grid_residual_nonzero() -> None:
    """A drop half a beat off a bar boundary reports that residual."""
    bpm = 174.0
    grid_start_s = 1.0
    bars_to_drop = 16
    half_beat_s = 0.5 * 60.0 / bpm
    drop_s = grid_start_s + (bars_to_drop * 4.0) * 60.0 / bpm + half_beat_s

    result = AlcClip.drop_alignment(bpm=bpm, grid_start_seconds=grid_start_s, drop_seconds=drop_s)
    assert result.residual_beats == pytest.approx(0.5, abs=1e-9)


# ── set_clip_start ───────────────────────────────────────────────────────────


def test_set_clip_start_uses_existing_grid() -> None:
    """set_clip_start converts a time to beats via the grid and touches only CurrentStart."""
    clip = AlcClip.load(LIVE_SAVED)
    clip.set_grid(bpm=172.0, grid_start_seconds=66.88, duration_seconds=320.94476187874625)

    end_before = clip.clip.find("CurrentEnd")
    assert end_before is not None
    end_value_before = end_before.get("Value")

    hotcue_seconds = 66.88 + (4.0 * 60.0 / 172.0)  # one bar after grid start
    clip.set_clip_start(hotcue_seconds)

    current_start = clip.clip.find("CurrentStart")
    assert current_start is not None
    assert float(current_start.get("Value", "")) == pytest.approx(4.0)

    end_after = clip.clip.find("CurrentEnd")
    assert end_after is not None
    assert end_after.get("Value") == end_value_before  # untouched


def test_set_clip_start_requires_grid() -> None:
    """set_clip_start refuses to guess a grid that hasn't been authored."""
    clip = AlcClip.load(LIVE_SAVED)
    # The as-saved fixture has a (degenerate, near-zero-duration) 2-marker grid, so use a
    # clip whose WarpMarkers we clear to exercise the "< 2 markers" guard directly.
    warp_markers_el = clip.clip.find("WarpMarkers")
    assert warp_markers_el is not None
    for el in list(warp_markers_el):
        warp_markers_el.remove(el)
    with pytest.raises(AlcError, match="set_grid"):
        clip.set_clip_start(10.0)


# ── Name / color / retarget ──────────────────────────────────────────────────


def test_name_and_color_accessors() -> None:
    """Name and color_index read the saved fixture and are settable."""
    clip = AlcClip.load(LIVE_SAVED)
    assert clip.name == "Audeka & Disprove - Militant - 9A(Em)"
    assert clip.color_index == 24

    clip.name = "renamed clip"
    clip.color_index = 5
    assert clip.name == "renamed clip"
    assert clip.color_index == 5


def test_retarget_sample_updates_paths_and_size(tmp_path: Path) -> None:
    """retarget_sample rewrites the FileRef's absolute/relative paths and file size."""
    new_audio = tmp_path / "new_track.wav"
    new_audio.write_bytes(b"\x00" * 128)

    clip = AlcClip.load(LIVE_SAVED)
    clip.retarget_sample(new_audio, relative_path=Path("Samples/Imported/new_track.wav"))

    file_ref = clip.clip.find("SampleRef/FileRef")
    assert file_ref is not None
    path_el = file_ref.find("Path")
    rel_el = file_ref.find("RelativePath")
    size_el = file_ref.find("OriginalFileSize")
    assert path_el is not None and rel_el is not None and size_el is not None
    assert path_el.get("Value") == new_audio.as_posix()
    assert rel_el.get("Value") == "Samples/Imported/new_track.wav"
    assert size_el.get("Value") == "128"


# ── Loading errors ───────────────────────────────────────────────────────────


def test_load_rejects_non_gzip(tmp_path: Path) -> None:
    """load() raises AlcError for a file that isn't gzip-compressed."""
    bad = tmp_path / "not_gzip.alc"
    bad.write_bytes(b"not a gzip file")
    with pytest.raises(AlcError, match="gzip"):
        AlcClip.load(bad)


def test_save_gzip_header_matches_ableton(tmp_path: Path) -> None:
    """save() must emit gzip FLG=0x00 like Ableton itself.

    Regression: gzip.open() embeds the temp filename via the FNAME flag (FLG=0x08),
    and Live's browser silently refuses to index such .alc files.
    """
    clip = AlcClip.load(LIVE_SAVED)
    out = tmp_path / "header_check.alc"
    clip.save(out)
    header = out.read_bytes()[:4]
    assert header[:2] == b"\x1f\x8b"
    assert header[3] == 0, f"gzip FLG must be 0x00 (Ableton-style), got 0x{header[3]:02x}"


def test_add_grid_marker_colinear_and_sorted() -> None:
    """add_grid_marker inserts a sorted, fresh-Id marker; explicit beat_time wins."""
    clip = AlcClip.load(LIVE_SAVED)
    clip.set_grid(bpm=174.0, grid_start_seconds=0.000069, duration_seconds=320.0)
    clip.add_grid_marker(44.138, beat_time=128.0)
    markers = clip.markers
    assert len(markers) == 3
    drop = markers[1]
    assert drop.sec_time == 44.138
    assert drop.beat_time == 128.0
    assert len({m.marker_id for m in markers}) == 3
    # computed (colinear) variant lands within float noise of the explicit beat
    clip2 = AlcClip.load(LIVE_SAVED)
    clip2.set_grid(bpm=174.0, grid_start_seconds=0.000069, duration_seconds=320.0)
    clip2.add_grid_marker(44.138)
    assert abs(clip2.markers[1].beat_time - 128.0) < 1e-6
