"""CS-80V and CS-80 V2 chunks into CS-80 V3: copied, switched and ported controls, what is said, both formats."""

from __future__ import annotations

import struct

import pytest

from abletoolz.formats.arturia_archive import TwoArchives
from abletoolz.plugins.converters import cs_80_v as conversion
from abletoolz.plugins.identity.ids import PluginKind
from abletoolz.plugins.plan.machine import Machine
from abletoolz.plugins.plan.model import Route
from abletoolz.plugins.plan.targets import mapped_targets
from abletoolz.plugins.state import cs_80_v, cs_80_v3
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.measured import StateEvidence

V2 = cs_80_v.CS_80_V2
V1 = cs_80_v.CS_80V

# What an untouched slot and voice hold in the old plugins: input disconnected, output off, voice tunings centred.
EMPTY = {
    **{f"Patch{p}_MODULATION{s:02d}_INPUT": 14.0 for p in range(1, 9) for s in range(1, 11)},
    **{f"Patch{p}_MODULATION{s:02d}_OUTPUT": 38.0 for p in range(1, 9) for s in range(1, 11)},
    **{f"Patch{p}_MODULATION{s:02d}_AMOUNT": 0.5 for p in range(1, 9) for s in range(1, 11)},
    **{f"CS_80_DETUNE{voice}": 0.5 for voice in range(1, 9)},
    "CS_80_TUNE": 0.5,
    "ZONE1_INF": 24.0,
    "ZONE1_SUP": 120.0,
}


def chunk(generation: cs_80_v.Generation = V2, bend: float = 0.5, expression: float = 0.0, **values: float) -> bytes:
    """A synthetic chunk: zeros, the untouched defaults, then ``values`` at their bytes."""
    data = bytearray(generation.chunk_bytes)
    for name, value in {**EMPTY, **values}.items():
        struct.pack_into("<f", data, cs_80_v.fields()[name][0], value)
    struct.pack_into("<f", data, cs_80_v.PITCH_BEND_OFFSET, bend)
    struct.pack_into("<f", data, cs_80_v.EXPRESSION_OFFSET, expression)
    return bytes(data)


def stored(patch: bytes) -> dict[str, float]:
    archives = TwoArchives.parse(patch).archives
    assert archives[0].values == archives[1].values
    return archives[0].values


def convert(data: bytes, generation: cs_80_v.Generation = V2) -> dict[str, float]:
    return stored(conversion.CONVERSIONS[generation.map_id](data).patch)


def test_all_four_old_plugins_convert_into_cs_80_v3_and_are_hosted() -> None:
    for generation in cs_80_v.GENERATIONS:
        found = CATALOG.converter(generation.map_id, "arturia.cs-80-v3")
        assert found is not None and found.measured.evidence == (StateEvidence.HOSTED,)
        assert CATALOG.find(CATALOG.by_map_id(generation.map_id).vst2.id) is CATALOG.by_map_id(generation.map_id)  # type: ignore[union-attr]


def test_the_map_places_every_cs_80_v3_control_once_inside_the_chunk() -> None:
    fields = cs_80_v.fields()
    assert len(fields) == 1002
    offsets = [offset for offset, _, _ in fields.values()]
    assert len(set(offsets)) == len(offsets) and max(offsets) < 5392
    assert not any(cs_80_v.PRESET_PATH.start <= offset < cs_80_v.PRESET_PATH.stop for offset in offsets)
    assert set(fields) <= set(stored(cs_80_v3.baseline()))


# CS-80 V2's own public parameters, by the name it gives them, at the byte its native getter reads, and the CS-80 V3
# name for the same control.
PUBLIC = [
    ("s1FrLo1", 28, "Patch1_CUTOFF_LOW1"),
    ("s1lf1Sp", 60, "Patch1_LFO1_SPEED"),
    ("s1ev2At", 104, "Patch1_ATTACK_TEMPS_VCA1"),
    ("s1M03Am", 360, "Patch1_MODULATION03_AMOUNT"),
    ("s2lf1Sp", 628, "Patch2_LFO1_SPEED"),
    ("s8FrLo2", 4160, "Patch8_CUTOFF_LOW2"),
    ("panv3", 4712, "PANORAMIQUE_VOIE3"),
    ("volv7", 4760, "VOLUME_VOIE7"),
    ("ringMo", 4960, "RING_MODULATION"),
    ("portam", 5008, "PORTAMENTO"),
    ("depdel", 5032, "DEPTH_DELAI"),
]


def test_a_control_is_read_at_the_byte_cs_80_v2_s_own_getter_reads() -> None:
    assert [(name, offset, cs_80_v.fields()[v3][0]) for name, offset, v3 in PUBLIC] == [
        (name, offset, offset) for name, offset, _ in PUBLIC
    ]
    assert (cs_80_v.PITCH_BEND_OFFSET, cs_80_v.EXPRESSION_OFFSET) == (4832, 4840)  # its "Bend" and "exp"


def test_knobs_are_copied_and_indexes_go_over_cs_80_v3_s_range() -> None:
    values = convert(chunk(Patch2_CUTOFF_LOW1=0.4321, VOLUME=0.7, Patch1_MODULATION01_OUTPUT=19.0, NUM_PATCH_VOIE3=1.0))
    assert values["Patch2_CUTOFF_LOW1"] == pytest.approx(0.4321) and values["VOLUME"] == pytest.approx(0.7)
    assert values["Patch1_MODULATION01_OUTPUT"] == pytest.approx(0.5) and values["NUM_PATCH_VOIE3"] == pytest.approx(
        1 / 7
    )
    assert values["ZONE1_INF"] == 0.0 and values["ZONE1_SUP"] == 1.0


def test_a_switch_is_on_from_a_half_and_written_zero_or_one() -> None:
    values = convert(chunk(Patch1_OSC1_SQR=0.75, Patch1_ON_HIGHT1=0.25, ON_CHORUS=0.5, RING_MOD_VOIE4=0.49))
    assert (values["Patch1_OSC1_SQR"], values["Patch1_ON_HIGHT1"]) == (1.0, 0.0)
    assert (values["ON_CHORUS"], values["RING_MOD_VOIE4"]) == (1.0, 0.0)


def test_a_sine_switched_off_is_on_at_no_level_and_one_switched_on_keeps_its_level() -> None:
    values = convert(
        chunk(Patch1_OSC1_ON_SIN=0.25, Patch1_OSC1_SINUS=0.3, Patch1_OSC2_ON_SIN=0.75, Patch1_OSC2_SINUS=0.4)
    )
    assert (values["Patch1_OSC1_ON_SIN"], values["Patch1_OSC1_SINUS"]) == (1.0, 0.0)
    assert (values["Patch1_OSC2_ON_SIN"], values["Patch1_OSC2_SINUS"]) == (1.0, pytest.approx(0.4))


def test_a_ring_modulator_switched_off_plays_with_its_modulation_knob_at_full() -> None:
    off = convert(chunk(ON_RINGMOD=0.25, RING_MODULATION=0.2))
    on = convert(chunk(ON_RINGMOD=0.75, RING_MODULATION=0.2))
    assert (off["ON_RINGMOD"], off["RING_MODULATION"]) == (1.0, 1.0)
    assert (on["ON_RINGMOD"], on["RING_MODULATION"]) == (1.0, pytest.approx(0.2))


def test_modulation_slots_are_written_as_the_ports_write_them() -> None:
    values = convert(
        chunk(Patch1_MODULATION01_INPUT=10.0, Patch1_MODULATION01_OUTPUT=8.0, Patch1_MODULATION01_AMOUNT=1.1)
    )
    assert values["Patch1_MODULATION01_AMOUNT"] == 1.0
    assert values["Patch1_MODULATION01_INPUT"] == pytest.approx(10 / 13)
    assert values["Patch1_MODULATION02_INPUT"] == 1.0 and values["Patch1_MODULATION02_OUTPUT"] == 1.0
    assert convert(chunk(TRAPPE_MULTI=2.0))["TRAPPE_MULTI"] == 1.0


def test_the_saved_mod_wheel_is_both_of_cs_80_v3_s_names_for_it() -> None:
    values = convert(chunk(MODULATION_WHEEL_CONTROL=0.375))
    assert values["MODULATION_WHEEL_CONTROL"] == values["VST3_CtrlModWheel"] == 0.375


def test_voice_tunings_cs_80_v3_does_not_play_are_centred_and_said() -> None:
    first = chunk(V1, **dict.fromkeys(conversion.DETUNES, 0.0))
    assert conversion.CONVERSIONS[V1.map_id](first).notices == (conversion.NOTE,)
    assert convert(first, V1)["CS_80_DETUNE1"] == 0.5
    tuned = conversion.CONVERSIONS[V2.map_id](chunk(CS_80_DETUNE3=0.7))
    assert "fine tuning" in tuned.notices[-1] and convert(chunk(CS_80_DETUNE3=0.7))["CS_80_DETUNE3"] == 0.5


def test_the_saved_pitch_bend_and_expression_are_reported() -> None:
    notices = conversion.CONVERSIONS[V2.map_id](chunk(bend=1.0, expression=0.3)).notices
    assert "pitch bend" in notices[-1] and "expression" in notices[-1]


def test_a_value_outside_cs_80_v3_s_range_is_refused_directly_and_said_by_the_converter() -> None:
    data = chunk(Patch1_MODULATION01_OUTPUT=40.0)
    with pytest.raises(StateTransformError, match="MODULATION01_OUTPUT"):
        conversion.plan(data, V2)
    values = convert(data)
    assert values["Patch1_MODULATION01_OUTPUT"] == 1.0


def test_only_a_zone_may_hold_a_fraction_and_only_its_ends_lie_outside_cs_80_v3_s_range() -> None:
    values = convert(chunk(ZONE1_INF=23.5, ZONE1_SUP=130.0))
    assert (values["ZONE1_INF"], values["ZONE1_SUP"]) == (0.0, 1.0)
    # The 0.5 the old plugins leave in an unused zone, kept as CS-80 V3's factory presets keep it.
    values = convert(chunk(ZONE3_INPUT=0.5, ZONE3_MODE=0.5))
    assert (values["ZONE3_INPUT"], values["ZONE3_MODE"]) == pytest.approx((0.5 / 16, 0.5 / 6))
    with pytest.raises(StateTransformError, match="NUM_PATCH_VOIE1 holds 2.5, not an item index"):
        conversion.CONVERSIONS[V2.map_id](chunk(NUM_PATCH_VOIE1=2.5))
    with pytest.raises(StateTransformError, match="ZONE2_MODE"):
        conversion.plan(chunk(ZONE2_MODE=9.0), V2)
    notices = conversion.CONVERSIONS[V2.map_id](chunk(ZONE2_MODE=9.0)).notices
    assert any("ZONE2_MODE stores 9, outside CS-80 V3's 0 to 6" in notice for notice in notices)


@pytest.mark.parametrize(
    "data", [chunk()[:-4], chunk(V1), chunk(Patch1_MODULATION01_OUTPUT=3.5), chunk(VOLUME=float("nan"))]
)
def test_a_chunk_the_plugin_did_not_write_is_refused(data: bytes) -> None:
    with pytest.raises(StateTransformError):
        conversion.CONVERSIONS[V2.map_id](data)


def test_the_first_generation_is_four_bytes_shorter_and_read_at_the_same_places() -> None:
    assert convert(chunk(V1, Patch2_RES_LOW1=0.25), V1)["Patch2_RES_LOW1"] == pytest.approx(0.25)
    with pytest.raises(StateTransformError, match="5392 bytes"):
        conversion.CONVERSIONS[V1.map_id](chunk())


def test_the_patch_lowers_to_a_vst2_chunk_and_a_vst3_preset_of_two_halves() -> None:
    patch = conversion.CONVERSIONS[V2.map_id](chunk()).patch
    target = CATALOG.by_map_id("arturia.cs-80-v3")
    assert target is not None
    assert target.lower(patch, PluginKind.VST).state == patch
    vst3 = target.lower(patch, PluginKind.VST3)
    assert vst3.state == vst3.controller == TwoArchives.split(patch)[0]


def test_the_baseline_is_the_plugin_s_default_and_unknown_names_are_refused() -> None:
    values = stored(cs_80_v3.baseline())
    assert len(values) == 1004 and not any(name.startswith(("Macro", "__Macro")) for name in values)
    with pytest.raises(StateTransformError, match="did not take the edit"):
        cs_80_v3.with_values(cs_80_v3.baseline(), {"NO_SUCH_CONTROL": 0.5})


def test_a_plan_offers_cs_80_v3_as_a_vst2_a_vst3_and_an_au() -> None:
    source = CATALOG.find(CATALOG.by_map_id(V2.map_id).vst2.id)  # type: ignore[union-attr]
    assert source is not None
    machine = Machine(vst2=(), vst3={}, au=lambda identifier: False, loadable=lambda info: False)
    targets = {target.id: target for target in mapped_targets(source, PluginKind.VST, machine)}
    assert set(targets) == {"cs_80_v3_vst2", "cs_80_v3_vst3", "cs_80_v3_au"}
    assert targets["cs_80_v3_vst2"].route is Route.CONVERTER
