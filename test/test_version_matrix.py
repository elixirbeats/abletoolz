"""Core features exercised against every supported Live version.

Fixtures are KB-scale skeletons of real sets (9.0.1 through 12.4b), generated
by ``test/tools/extract_version_fixture.py``; ``expected.json`` holds ground
truth harvested by direct XPath, independent of the code under test.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any
from xml.etree import ElementTree as ET

import pytest
from conftest import SKELETONS, skeleton_set, writable_set

from abletoolz.elements import get_element
from abletoolz.errors import ElementNotFound, SetError
from abletoolz.live_set import AbletonSet
from abletoolz.live_set.tracks import STEREO_OUTPUTS
from abletoolz.machine import Locations
from abletoolz.plugins.container.device import PluginDevices, set_supports
from abletoolz.plugins.identity.ids import PluginKind
from abletoolz.plugins.loadable import LoadableOracle
from abletoolz.plugins.plan import Machine, PluginChoice, Route, Target, carry
from abletoolz.schema import Tag

EXPECTED: dict[str, dict[str, Any]] = json.loads((SKELETONS / "expected.json").read_text(encoding="utf-8"))


KEYS = sorted(EXPECTED)


def unique_id_of(ableton_set: AbletonSet, plug_name: str) -> int:
    """The VST2 UniqueId the set stores for the device named ``plug_name``."""
    (info,) = [
        info
        for info in ableton_set.root.iter("VstPluginInfo")
        if get_element(info, "PlugName", attribute="Value") == plug_name
    ]
    return int(get_element(info, "UniqueId", attribute="Value"))


@pytest.mark.parametrize("key", KEYS)
def test_version_tuple(key: str) -> None:
    assert skeleton_set(key).version_tuple == tuple(EXPECTED[key]["version"])


@pytest.mark.parametrize("key", KEYS)
def test_get_bpm(key: str) -> None:
    assert skeleton_set(key).transport.bpm() == pytest.approx(EXPECTED[key]["bpm"])


@pytest.mark.parametrize("key", KEYS)
def test_load_tracks_names_and_types(key: str) -> None:
    ableton_set = skeleton_set(key)
    tracks = ableton_set.tracks.load()
    expected_tracks = EXPECTED[key]["tracks"]
    assert [t.type for t in tracks] == [t["tag"] for t in expected_tracks]
    assert [t.name for t in tracks] == [t["name"] for t in expected_tracks]


@pytest.mark.parametrize("key", KEYS)
def test_track_colors(key: str) -> None:
    ableton_set = skeleton_set(key)
    tracks = ableton_set.tracks.load()
    expected_colors = [t["color"] if t["color"] is not None else t["color_index"] for t in EXPECTED[key]["tracks"]]
    assert [t.color for t in tracks] == expected_colors


@pytest.mark.parametrize("key", KEYS)
def test_set_track_widths(key: str) -> None:
    """Every width element in the set gets the new value, whichever spelling the version uses."""
    ableton_set = skeleton_set(key)
    ableton_set.tracks.set_widths(100)
    widths = [
        el.get("Value")
        for tag in ("ViewStateSesstionTrackWidth", "ViewStateSessionTrackWidth")
        for el in ableton_set.root.iter(tag)
    ]
    assert widths
    assert all(value == "100" for value in widths)


@pytest.mark.parametrize("key", KEYS)
def test_set_master_audio_output(key: str) -> None:
    """Routing the master out must at least find the master track on every version."""
    try:
        skeleton_set(key).tracks.set_audio_output(1, element_string="MasterTrack")
    except (ElementNotFound, SetError) as exc:
        pytest.fail(f"set_audio_output failed on {key}: {exc}")


@pytest.mark.parametrize("key", ["9.6.0", "11.3.42", "12.2.6"])
def test_either_main_track_name_routes_the_main_track_on_any_version(key: str) -> None:
    """MainTrack is Live 12's name for MasterTrack; either one finds the main track whatever the set's version."""
    for name in (Tag.MAIN_TRACK, Tag.MASTER_TRACK):
        live_set = skeleton_set(key)
        main = live_set.tracks.main()
        assert main is not None
        live_set.tracks.set_audio_output(3, element_string=name)
        assert STEREO_OUTPUTS[3]["target"] in ET.tostring(main.track_root, encoding="unicode")


def test_set_audio_output_refuses_a_name_that_is_not_the_main_or_cue_track() -> None:
    with pytest.raises(ValueError, match="neither the main track nor the cue track"):
        skeleton_set("12.2.6").tracks.set_audio_output(1, element_string="AudioTrack")


@pytest.mark.parametrize("key", KEYS)
def test_set_cue_audio_output(key: str) -> None:
    """Cue routing must not crash, even on sets that carry no PreHearTrack at all."""
    try:
        skeleton_set(key).tracks.set_audio_output(1, element_string="PreHearTrack")
    except (ElementNotFound, SetError) as exc:
        pytest.fail(f"cue-out failed on {key}: {exc}")


@pytest.mark.parametrize("key", KEYS)
def test_gradient_tracks_invariants(key: str) -> None:
    """Gradient assigns a valid Live color index to every track (values are seeded random)."""
    ableton_set = skeleton_set(key)
    ableton_set.tracks.gradient()
    assert all(0 <= track.color <= 69 for track in ableton_set.tracks.load())


@pytest.mark.parametrize("key", KEYS)
def test_iterate_samples(key: str) -> None:
    """Sample references parse on every version's FileRef shape (Path vs hex Data)."""
    ableton_set = skeleton_set(key)
    parsed = list(ableton_set.samples.iterate())
    assert len(parsed) == EXPECTED[key]["sample_ref_count"]
    with_abs = sum(1 for p in parsed if p.absolute is not None)
    assert with_abs == EXPECTED[key]["sample_refs_with_abs"]


@pytest.mark.parametrize("key", KEYS)
def test_fold_and_unfold_tracks(key: str) -> None:
    ableton_set = skeleton_set(key)
    ableton_set.tracks.fold()
    folded = [el.get("Value") for el in ableton_set.root.iter("TrackUnfolded")]
    assert len(folded) == EXPECTED[key]["track_unfolded_count"]
    assert folded and all(value == "false" for value in folded)
    group_folds = [
        get_element(group, "DeviceChain/Mixer/IsFolded").get("Value")
        for group in ableton_set.root.findall("LiveSet/Tracks/GroupTrack")
        if group.find("DeviceChain/Mixer/IsFolded") is not None
    ]
    assert len(group_folds) == EXPECTED[key]["group_mixer_isfolded_count"]
    assert all(value == "true" for value in group_folds)  # groups collapse on fold
    ableton_set.tracks.unfold()
    assert all(el.get("Value") == "true" for el in ableton_set.root.iter("TrackUnfolded"))
    assert all(
        get_element(group, "DeviceChain/Mixer/IsFolded").get("Value") == "false"
        for group in ableton_set.root.findall("LiveSet/Tracks/GroupTrack")
        if group.find("DeviceChain/Mixer/IsFolded") is not None
    )


@pytest.mark.parametrize("key", KEYS)
def test_set_track_heights(key: str) -> None:
    ableton_set = skeleton_set(key)
    ableton_set.tracks.set_heights(100)
    heights = [el.get("Value") for el in ableton_set.root.iter("LaneHeight")]
    assert len(heights) == EXPECTED[key]["lane_height_count"]
    assert heights and all(value == "100" for value in heights)


@pytest.mark.parametrize("key", KEYS)
def test_find_furthest_bar(key: str) -> None:
    assert skeleton_set(key).transport.furthest_bar() == EXPECTED[key]["furthest_bar"]


@pytest.mark.parametrize("key", KEYS)
def test_scan_vst3_refs(key: str, locations: Locations) -> None:
    """Vst3PluginInfo devices surface as vst3 PluginRefs on every version that carries them."""
    refs = skeleton_set(key).plugins.scan(LoadableOracle(locations))
    vst3_names = [ref.name for ref in refs if ref.kind == "vst3"]
    assert vst3_names == EXPECTED[key]["vst3_plugin_names"]
    assert all(not ref.exists for ref in refs if ref.kind == "vst3")  # nothing resolvable hermetically

    au_refs = [ref for ref in refs if ref.kind == "au"]
    expected_au = EXPECTED[key].get("au_plugins", [])
    assert [(ref.name, ref.manufacturer) for ref in au_refs] == [
        (plugin["name"], plugin["manufacturer"]) for plugin in expected_au
    ]
    assert all(not ref.exists for ref in au_refs)


@pytest.mark.parametrize("key", KEYS)
def test_a_vst3_target_is_refused_below_the_schema_that_declares_vst3(key: str) -> None:
    """Every VST2 device in every fixture, offered a VST3 guess, on that version's terms.

    Below the measured floor the answer must be a refusal: a set whose schema
    never declared ``Vst3PluginInfo`` is rejected by Live outright once one is
    written into it, so the whole document is at stake, not the one device. At
    or above the floor, configured controls still need a parameter map, and a
    guess cannot rebuild Live's own list of them either.
    """
    ableton_set = skeleton_set(key)
    version = tuple(EXPECTED[key]["version"])
    devices = [device for device in PluginDevices(ableton_set.root) if device.kind is PluginKind.VST]
    if not devices:
        pytest.skip(f"{key} carries no VST2 devices to map")
    guess = Target(
        id="try X (VST3)",
        route=Route.EXPERIMENT,
        plugin="X",
        format=PluginKind.VST3,
        evidence="",
        proved=False,
        installed=True,
        uid_fields=(1, 2, 3, 4),
    )
    choice = PluginChoice(plugin="X", format=PluginKind.VST, name="X", broken=True, devices=[])
    machine = Machine(vst2=(), vst3={}, au=lambda identifier: False, loadable=lambda info: True)
    answers = [carry(device, choice, guess, machine) for device in devices]
    reasons = [answer if isinstance(answer, str) else None for answer in answers]
    if set_supports(PluginKind.VST3, version):
        configured_devices = {
            "10.1.18": 8,
            "10.1.30": 3,
            "11.0.0": 4,
            "11.0.12": 9,
            "11.3.42": 0,
            "12.2.6": 1,
            "12.4.5b": 2,
        }
        controls = ("parameter map", "Live's own list")
        assert sum(any(text in (reason or "") for text in controls) for reason in reasons) == configured_devices[key]
        assert not any(reason is not None and "too old" in reason for reason in reasons)
    else:
        assert all(reason is not None and "too old" in reason for reason in reasons)


@pytest.mark.parametrize("key", KEYS)
def test_midi_note_counts(key: str) -> None:
    """Every MidiNoteEvent under a track's session/arrangement clips is surfaced."""
    ableton_set = skeleton_set(key)
    total_notes = sum(len(clip.notes) for clip in ableton_set.clips.midi())
    assert total_notes == EXPECTED[key]["midi_note_counts"]


@pytest.mark.parametrize("key", KEYS)
def test_save_and_reload_roundtrip(key: str, tmp_path: pathlib.Path) -> None:
    """A set survives save: the written file reparses with the same version and bpm."""
    ableton_set = writable_set(key, tmp_path)
    ableton_set.get_file_times()
    original_bpm = ableton_set.transport.bpm()
    ableton_set.save_set()
    reloaded = AbletonSet(ableton_set.path)
    assert reloaded.parse()
    assert reloaded.version_tuple == tuple(EXPECTED[key]["version"])
    assert reloaded.transport.bpm() == pytest.approx(original_bpm)
