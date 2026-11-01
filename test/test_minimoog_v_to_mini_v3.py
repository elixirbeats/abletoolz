"""minimoog V Original chunks into Mini V3: copied, switched and indexed controls, what is refused, both formats."""

from __future__ import annotations

import struct

import pytest

from abletoolz.formats.arturia_archive import TwoArchives
from abletoolz.plugins.converters import minimoog_v as conversion
from abletoolz.plugins.identity.ids import PluginKind
from abletoolz.plugins.plan.machine import Machine
from abletoolz.plugins.plan.model import Route
from abletoolz.plugins.plan.targets import mapped_targets
from abletoolz.plugins.state import mini_v3, minimoog_v
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.measured import StateEvidence

# An unused modulation slot in the original: input Off, output Off, no depth.
EMPTY = {
    **{f"MODULATION{slot:02d}_INPUT": 12.0 for slot in range(1, 21)},
    **{f"MODULATION{slot:02d}_OUTPUT": 32.0 for slot in range(1, 21)},
    **{f"MODULATION{slot:02d}_AMOUNT": 0.5 for slot in range(1, 21)},
    **{f"COURBE{curve}_DEST": 32.0 for curve in range(1, 5)},
    "PITCH_WHEEL_CONTROL": 0.5,
}


def chunk(**values: float) -> bytes:
    """A synthetic chunk: zeros, the unused slots, then ``values`` at their offsets."""
    data = bytearray(minimoog_v.CHUNK_BYTES)
    for name, value in {**EMPTY, **values}.items():
        struct.pack_into("<f", data, minimoog_v.OFFSETS[name], value)
    return bytes(data)


def stored(patch: bytes) -> dict[str, float]:
    archives = TwoArchives.parse(patch).archives
    assert archives[0].values == archives[1].values
    return archives[0].values


def test_the_converter_joins_the_two_maps_and_is_hosted() -> None:
    found = CATALOG.converter("arturia.minimoog-v-original", "arturia.mini-v3")
    assert found is not None and found.measured.evidence == (StateEvidence.HOSTED,)
    assert CATALOG.find(minimoog_v.MINIMOOG_V_ORIGINAL_VST2) is CATALOG.by_map_id("arturia.minimoog-v-original")


# Where minimoog V Original.dll registers each of these controls, read off its registration calls: one or two per run.
REGISTERED = {
    "WIDTH_OSC1": 24,
    "SOFT_DIST": 116,
    "CUT_OFF": 156,
    "LEVEL_SUSTAIN_VCA": 188,
    "LFO_RATE": 432,
    "MODULATION_WHEEL_CONTROL": 528,
    "ON_A440": 588,
    "KEYB_OSC2": 740,
    "ON_MOTION_RECORDER": 816,
    "LFO_WAVE_EXT": 7184,
}


def test_a_control_is_read_at_the_offset_the_plugin_registers_it_at() -> None:
    assert {name: minimoog_v.OFFSETS[name] for name in REGISTERED} == REGISTERED


def test_knobs_and_shared_choices_are_copied_in_the_plugin_s_own_spelling() -> None:
    converted = conversion.convert(chunk(CUT_OFF=0.21312, RANGE_OSC2=0.5669, TYPE_CHORUS=0.4792, VOLUME=0.7953))
    values = stored(converted.patch)
    assert values["CUT_OFF"] == pytest.approx(0.21312, abs=1e-7)
    assert values["RANGE_OSC2"] == pytest.approx(0.5669, abs=1e-7) and values["TYPE_CHORUS"] == pytest.approx(0.4792)
    assert b" 7 CUT_OFF 0.21312 " in converted.patch
    assert converted.notices == (conversion.NOTE,)


def test_the_saved_mod_wheel_is_mini_v3_s_wheel() -> None:
    assert stored(conversion.convert(chunk(MODULATION_WHEEL_CONTROL=0.375)).patch)["VST3_CtrlModWheel"] == 0.375


def test_a_switch_is_on_from_a_half_and_legato_on_is_glide() -> None:
    values = stored(conversion.convert(chunk(ON_OSC1=0.75, ON_OSC2=0.25, MODUL_OSC=0.503937, MODE_LEGATO=0.75)).patch)
    assert (values["ON_OSC1"], values["ON_OSC2"], values["MODUL_OSC"]) == (1.0, 0.0, 1.0)
    assert values["MODE_LEGATO"] == 1.0


def test_indexes_become_the_index_over_the_top_index() -> None:
    values = stored(
        conversion.convert(chunk(MODULATION01_INPUT=4.0, MODULATION01_OUTPUT=11.0, MODE_ZONE=2.0, NBR_VOIES=5.0)).patch
    )
    assert values["MODULATION01_INPUT"] == pytest.approx(4 / 15) and values["MODULATION01_OUTPUT"] == pytest.approx(
        11 / 35
    )
    assert values["MODE_ZONE"] == 1.0 and values["NBR_VOIES"] == pytest.approx(5 / 9)
    assert stored(conversion.convert(chunk(NBR_VOIES=15.0)).patch)["NBR_VOIES"] == pytest.approx(8 / 9)


def test_the_last_unused_slot_carries_mini_v3_s_lfo_to_cutoff_route_at_no_depth() -> None:
    values = stored(conversion.convert(chunk(MODULATION08_OUTPUT=38.0, MODULATION08_AMOUNT=0.7)).patch)
    assert values["MODULATION08_INPUT"] == pytest.approx(8 / 15) and values["MODULATION08_OUTPUT"] == pytest.approx(
        11 / 35
    )
    assert values["MODULATION08_AMOUNT"] == 0.5
    assert values["MODULATION07_INPUT"] == pytest.approx(12 / 15) and values["MODULATION07_OUTPUT"] == pytest.approx(
        32 / 35
    )
    routed = stored(conversion.convert(chunk(MODULATION02_INPUT=8.0, MODULATION02_OUTPUT=11.0)).patch)
    assert routed["MODULATION08_INPUT"] == pytest.approx(12 / 15)


@pytest.mark.parametrize(
    ("values", "said"),
    [
        ({"MODULATION09_INPUT": 8.0}, "modulation slot 9"),
        ({"MODULATION03_INPUT": 8.0, "MODULATION03_OUTPUT": 37.0}, "past Mini V3's"),
        ({"ON_MOTION_RECORDER": 1.0}, "motion recorder"),
        ({"CANAL_MIDI": 3.0}, "MIDI channel"),
        ({"ON_EXT": 0.75}, "external input"),
        ({"ARP_HOLD": 0.75}, "arpeggiator hold"),
        ({"NBR_VOIES": 9.0}, "10 voices"),
        ({"LEVEL_OSC1": 1.1}, "LEVEL_OSC1"),
    ],
)
def test_what_mini_v3_has_no_place_for_is_refused_directly_and_said_by_the_converter(
    values: dict[str, float], said: str
) -> None:
    with pytest.raises(StateTransformError, match=said):
        conversion.plan(chunk(**values))
    assert any(said in notice for notice in conversion.convert(chunk(**values)).notices)


def test_the_saved_pitch_bend_is_reported() -> None:
    assert "pitch bend" in conversion.convert(chunk(PITCH_WHEEL_CONTROL=1.0)).notices[-1]


@pytest.mark.parametrize("data", [chunk()[:-4], chunk(MODE_ZONE=1.5), chunk(MODULATION01_INPUT=16.0)])
def test_a_chunk_the_original_did_not_write_is_refused(data: bytes) -> None:
    with pytest.raises(StateTransformError):
        conversion.convert(data)


def test_the_patch_lowers_to_a_vst2_chunk_and_a_vst3_preset_of_two_halves() -> None:
    patch = conversion.convert(chunk(CUT_OFF=0.25)).patch
    target = CATALOG.by_map_id("arturia.mini-v3")
    assert target is not None
    assert target.lower(patch, PluginKind.VST).state == patch
    vst3 = target.lower(patch, PluginKind.VST3)
    assert vst3.state == vst3.controller == TwoArchives.split(patch)[0]


def test_the_baseline_is_the_plugin_s_default_and_unknown_names_are_refused() -> None:
    values = stored(mini_v3.baseline())
    assert len(values) == 174 and values["MODULATION08_INPUT"] == pytest.approx(0.8)
    with pytest.raises(StateTransformError, match="did not take the edit"):
        mini_v3.with_values(mini_v3.baseline(), {"NO_SUCH_CONTROL": 0.5})


def test_a_plan_offers_mini_v3_as_a_vst2_and_a_vst3() -> None:
    source = CATALOG.find(minimoog_v.MINIMOOG_V_ORIGINAL_VST2)
    assert source is not None
    machine = Machine(vst2=(), vst3={}, au=lambda identifier: False, loadable=lambda info: False)
    targets = {target.id: target for target in mapped_targets(source, PluginKind.VST, machine)}
    assert set(targets) == {"mini_v3_vst2", "mini_v3_vst3"}
    assert targets["mini_v3_vst2"].format is PluginKind.VST and targets["mini_v3_vst2"].route is Route.CONVERTER
