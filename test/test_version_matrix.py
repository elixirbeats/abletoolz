"""Core features exercised against every supported Live version.

Fixtures are KB-scale skeletons of real sets (9.0.1 through 12.4b), generated
by ``test/tools/extract_version_fixture.py``; ``expected.json`` holds ground
truth harvested by direct XPath, independent of the code under test.

Known breakage on a version gets marked ``xfail(strict=True)`` via the
``xfail_when`` hook with the root cause as the reason — fixing the code flips
the mark into a hard failure, forcing its removal. All current versions pass;
the hook is ready for whatever Live 13 renames next.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Callable
from typing import Any

import pytest

from abletoolz.live_set import AbletonSet, plugins
from abletoolz.misc import ElementNotFound, SetError

SKELETONS = pathlib.Path(__file__).parent / "version_fixtures" / "skeletons"
EXPECTED: dict[str, dict[str, Any]] = json.loads((SKELETONS / "expected.json").read_text(encoding="utf-8"))

def _params(
    xfail_when: Callable[[dict[str, Any]], str | None] | None = None,
) -> list[Any]:
    params: list[Any] = []
    for key in sorted(EXPECTED):
        marks: list[pytest.MarkDecorator] = []
        reason = xfail_when(EXPECTED[key]) if xfail_when else None
        if reason:
            marks.append(pytest.mark.xfail(reason=reason, strict=True))
        params.append(pytest.param(key, marks=marks, id=key))
    return params


def make_set(key: str) -> AbletonSet:
    ableton_set = AbletonSet(SKELETONS / f"{key}.als")
    assert ableton_set.parse()
    return ableton_set


@pytest.mark.parametrize("key", _params())
def test_version_tuple(key: str) -> None:
    assert make_set(key).version_tuple == tuple(EXPECTED[key]["version"])


@pytest.mark.parametrize("key", _params())
def test_get_bpm(key: str) -> None:
    assert make_set(key).transport.bpm() == pytest.approx(EXPECTED[key]["bpm"])


@pytest.mark.parametrize("key", _params())
def test_load_tracks_names_and_types(key: str) -> None:
    ableton_set = make_set(key)
    tracks = ableton_set.tracks.load()
    expected_tracks = EXPECTED[key]["tracks"]
    assert [t.type for t in tracks] == [t["tag"] for t in expected_tracks]
    assert [t.name for t in tracks] == [t["name"] for t in expected_tracks]


@pytest.mark.parametrize("key", _params())
def test_track_colors(key: str) -> None:
    ableton_set = make_set(key)
    tracks = ableton_set.tracks.load()
    expected_colors = [
        t["color"] if t["color"] is not None else t["color_index"] for t in EXPECTED[key]["tracks"]
    ]
    assert [t.color for t in tracks] == expected_colors


@pytest.mark.parametrize("key", _params())
def test_set_track_widths(key: str) -> None:
    """Every width element in the set gets the new value, whichever spelling the version uses."""
    ableton_set = make_set(key)
    ableton_set.tracks.set_widths(100)
    widths = [
        el.get("Value")
        for tag in ("ViewStateSesstionTrackWidth", "ViewStateSessionTrackWidth")
        for el in ableton_set.root.iter(tag)
    ]
    assert widths
    assert all(value == "100" for value in widths)


@pytest.mark.parametrize("key", _params())
def test_set_master_audio_output(key: str) -> None:
    """Routing the master out must at least find the master track on every version."""
    try:
        make_set(key).tracks.set_audio_output(1, element_string="MasterTrack")
    except (ElementNotFound, SetError) as exc:
        pytest.fail(f"set_audio_output failed on {key}: {exc}")


@pytest.mark.parametrize("key", _params())
def test_set_cue_audio_output(key: str) -> None:
    """Cue routing must not crash, even on sets that carry no PreHearTrack at all."""
    try:
        make_set(key).tracks.set_audio_output(1, element_string="PreHearTrack")
    except (ElementNotFound, SetError) as exc:
        pytest.fail(f"cue-out failed on {key}: {exc}")


@pytest.mark.parametrize("key", _params())
def test_gradient_tracks_invariants(key: str) -> None:
    """Gradient assigns a valid Live color index to every track (values are seeded random)."""
    ableton_set = make_set(key)
    ableton_set.tracks.gradient()
    assert all(0 <= track.color <= 69 for track in ableton_set.tracks.load())


@pytest.mark.parametrize("key", _params())
def test_iterate_samples(key: str) -> None:
    """Sample references parse on every version's FileRef shape (Path vs hex Data)."""
    ableton_set = make_set(key)
    parsed = list(ableton_set.samples.iterate())
    assert len(parsed) == EXPECTED[key]["sample_ref_count"]
    with_abs = sum(1 for p in parsed if p.absolute is not None)
    assert with_abs == EXPECTED[key]["sample_refs_with_abs"]


@pytest.mark.parametrize("key", _params())
def test_fold_and_unfold_tracks(key: str) -> None:
    ableton_set = make_set(key)
    ableton_set.tracks.fold()
    folded = [el.get("Value") for el in ableton_set.root.iter("TrackUnfolded")]
    assert len(folded) == EXPECTED[key]["track_unfolded_count"]
    assert folded and all(value == "false" for value in folded)
    group_folds = [
        group.find("DeviceChain/Mixer/IsFolded").get("Value")
        for group in ableton_set.root.findall("LiveSet/Tracks/GroupTrack")
        if group.find("DeviceChain/Mixer/IsFolded") is not None
    ]
    assert len(group_folds) == EXPECTED[key]["group_mixer_isfolded_count"]
    assert all(value == "true" for value in group_folds)  # groups collapse on fold
    ableton_set.tracks.unfold()
    assert all(el.get("Value") == "true" for el in ableton_set.root.iter("TrackUnfolded"))
    assert all(
        group.find("DeviceChain/Mixer/IsFolded").get("Value") == "false"
        for group in ableton_set.root.findall("LiveSet/Tracks/GroupTrack")
        if group.find("DeviceChain/Mixer/IsFolded") is not None
    )


@pytest.mark.parametrize("key", _params())
def test_set_track_heights(key: str) -> None:
    ableton_set = make_set(key)
    ableton_set.tracks.set_heights(100)
    heights = [el.get("Value") for el in ableton_set.root.iter("LaneHeight")]
    assert len(heights) == EXPECTED[key]["lane_height_count"]
    assert heights and all(value == "100" for value in heights)


@pytest.mark.parametrize("key", _params())
def test_find_furthest_bar(key: str) -> None:
    assert make_set(key).transport.furthest_bar() == EXPECTED[key]["furthest_bar"]


@pytest.mark.parametrize("key", _params())
def test_scan_vst3_refs(key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Vst3PluginInfo devices surface as vst3 PluginRefs on every version that carries them."""
    monkeypatch.setattr(plugins, "default_vst_dirs", lambda: [])
    monkeypatch.setattr(plugins, "default_live_database_dir", lambda: None)
    refs = make_set(key).plugins.scan([])
    vst3_names = [ref.name for ref in refs if ref.kind == "vst3"]
    assert vst3_names == EXPECTED[key]["vst3_plugin_names"]
    assert all(not ref.exists for ref in refs if ref.kind == "vst3")  # nothing resolvable hermetically


@pytest.mark.parametrize("key", _params())
def test_save_and_reload_roundtrip(key: str, tmp_path: pathlib.Path) -> None:
    """A set survives save: the written file reparses with the same version and bpm."""
    copy = tmp_path / f"{key}.als"
    copy.write_bytes((SKELETONS / f"{key}.als").read_bytes())
    ableton_set = AbletonSet(copy)
    assert ableton_set.parse()
    ableton_set.get_file_times()
    original_bpm = ableton_set.transport.bpm()
    ableton_set.save_set()
    reloaded = AbletonSet(copy)
    assert reloaded.parse()
    assert reloaded.version_tuple == tuple(EXPECTED[key]["version"])
    assert reloaded.transport.bpm() == pytest.approx(original_bpm)
