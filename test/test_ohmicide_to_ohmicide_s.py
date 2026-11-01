"""Ohmicide chunks into Ohmicide[S]: the units each control crosses in, the type knob, the mix, what is refused."""

from __future__ import annotations

import math
import struct

import pytest

from abletoolz.plugins.converters import ohmicide as conversion
from abletoolz.plugins.identity.ids import PluginKind
from abletoolz.plugins.plan.machine import Machine
from abletoolz.plugins.plan.model import Route
from abletoolz.plugins.plan.targets import mapped_targets
from abletoolz.plugins.state import ohmicide, ohmicide_s
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.framings import JUCE_PRIVATE_DATA_TAG
from abletoolz.plugins.state.measured import StateEvidence

# A patch at rest: no tone shape, the pre-distortion at its bottom, unity master and full wet.
REST = {"Dry/Wet mix": 1.0, "Master Vol": math.log2(1 + (2**18 - 1) / 100) / 18, "Input trim": 0.5, "Pre Disto": 0.0}


def chunk(named: dict[str, float] | None = None, **values: float) -> bytes:
    """A synthetic chunk: the five records, zeros, the patch at rest, then ``named`` and ``values`` by control name.

    A keyword spells a control's spaces as underscores.
    """
    data = bytearray()
    for tag, size in ohmicide.RECORDS:
        data += struct.pack(">4sI", tag, size) + bytes(size)
    data[8:12] = b"PDT2"
    named = {**(named or {}), **{name.replace("_", " "): value for name, value in values.items()}}
    for name, value in {**REST, **named}.items():
        struct.pack_into(">f", data, 96 + 4 * ohmicide.CONTROLS.index(name), value)
    return bytes(data)


def converted(named: dict[str, float] | None = None, **values: float) -> dict[int, float]:
    return ohmicide_s.settings(conversion.convert(chunk(named, **values)).patch)


def test_the_converter_joins_the_two_maps_and_is_hosted() -> None:
    found = CATALOG.converter("ohm-force.ohmicide", "ohm-force.ohmicide-s")
    assert found is not None and found.measured.evidence == (StateEvidence.HOSTED,)
    assert CATALOG.find(ohmicide.OHMICIDE_VST2) is CATALOG.by_map_id("ohm-force.ohmicide")
    assert CATALOG.find(ohmicide_s.OHMICIDE_S_VST3) is CATALOG.by_map_id("ohm-force.ohmicide-s")


@pytest.mark.parametrize(
    ("step", "kind", "family"),
    [(0, 0, 0), (1, 1, 0), (37, 37, 0), (50, 37, 0), (51, 1, 1), (87, 37, 1), (100, 37, 1), (101, 1, 2), (150, 37, 2)],
)
def test_the_type_knob_holds_a_type_and_its_family_in_steps_of_a_150th(step: int, kind: int, family: int) -> None:
    stored = struct.unpack(">f", struct.pack(">f", step / 150))[0]
    assert conversion.distortion(stored) == (kind, family)
    values = converted(B2_Disto_Type=stored)
    assert (values[conversion.band(2, conversion.TYPE)], values[conversion.band(2, conversion.FAMILY)]) == (
        kind,
        family,
    )


def test_each_control_crosses_as_the_quantity_both_plugins_print() -> None:
    values = converted(
        Tempo=0.5,
        B1_Upper_Freq=0.5,
        B3_Fdbk_Freq=0.5,
        B3_Fdbk_Spread=0.25,
        B1_Disto_Gain=0.5,
        B1_Disto_Bias=0.75,
        B1_Gate_Threshold=0.5,
        B1_Gate_Atk_Time=0.5,
        B1_Gate_Rls_Time=0.5,
        B1_Pan=0.25,
        B1_Volume=0.5,
        B4_Mute=0.75,
    )
    band = conversion.band
    assert values[conversion.TEMPO] == pytest.approx(150)
    assert values[conversion.UPPER[0]] == pytest.approx(640) and values[conversion.SPLIT] == 20
    assert values[band(3, 1)] == pytest.approx(128) and values[band(3, 2)] == pytest.approx(-0.5)
    assert values[band(1, 5)] == pytest.approx(10) and values[band(1, conversion.BIAS)] == pytest.approx(0.5)
    assert values[band(1, 10)] == pytest.approx(7 / 63)
    assert values[band(1, 11)] == pytest.approx(0.0408) and values[band(1, 12)] == pytest.approx(0.0816)
    assert values[band(1, conversion.PAN)] == pytest.approx(-0.5) and values[band(1, 20)] == pytest.approx(90 / 99)
    assert values[band(4, 24)] == 1.0 and values[band(4, 23)] == 0.0
    assert values[conversion.MASTER] == pytest.approx(1, abs=1e-6) and values[conversion.TRIM] == pytest.approx(1)


@pytest.mark.parametrize("mix", [0.0, 0.2, 0.6, 1.0])
def test_the_dry_wet_keeps_its_ratio_and_the_master_makes_up_the_level(mix: float) -> None:
    values = converted({"Dry/Wet mix": mix})
    wet = 3 * mix**2 - 2 * mix**3
    dry = math.sqrt(1 - wet * wet)
    written = values[conversion.MIX]
    linear_wet = 3 * written**2 - 2 * written**3
    gain = values[conversion.MASTER]
    assert gain * linear_wet == pytest.approx(wet, abs=1e-6) and gain * (1 - linear_wet) == pytest.approx(dry, abs=1e-6)


@pytest.mark.parametrize(
    ("values", "said"),
    [
        ({"Tone Shape": 0.2}, "tone stage"),
        ({"Pre Disto": 0.5}, "pre-distortion at 20.0 dB"),
        ({"Master Vol": 1.0, "Dry/Wet mix": 0.5}, "past Ohmicide"),
    ],
)
def test_what_ohmicide_s_has_no_place_for_is_refused_directly_and_said_by_the_converter(
    values: dict[str, float], said: str
) -> None:
    with pytest.raises(StateTransformError, match=said):
        conversion.plan(chunk(values))
    notices = conversion.convert(chunk(values)).notices
    assert notices[0] == conversion.NOTE and any(said in notice for notice in notices)


def test_a_patch_at_rest_converts_with_the_note_alone() -> None:
    assert conversion.convert(chunk()).notices == (conversion.NOTE,)


@pytest.mark.parametrize(
    "data", [chunk()[:-1], chunk().replace(b"Melo", b"Mela"), chunk().replace(b"PDT2", b"PDT3"), chunk(B1_Body=1.5)]
)
def test_a_chunk_the_original_did_not_write_is_refused(data: bytes) -> None:
    with pytest.raises(StateTransformError):
        conversion.convert(data)


def test_the_patch_is_spelled_as_the_plugin_writes_it() -> None:
    patch = conversion.convert(chunk(B1_Disto_Gain=0.5, B1_Body=0.07)).patch
    assert ohmicide_s.spelled(patch) == patch
    assert b'\r\n    "106": 10.0,\r\n' in patch and b'"105": 0.07000000029802322' in patch
    assert b'"patch_name": "Ohmicide"' in patch and patch.endswith(b"}\x00")
    assert ohmicide_s.spelled(ohmicide_s.baseline()) == ohmicide_s.baseline()
    spelled = ohmicide_s.with_settings(ohmicide_s.baseline(), {105: 3.764955458507302e-9, 106: 0.07})
    assert b'"105": 3.764955458507302e-9' in spelled and b'"106": 0.07000000000000001' in spelled


def test_an_index_the_patch_does_not_hold_is_refused() -> None:
    with pytest.raises(StateTransformError, match="no parameter 5000"):
        ohmicide_s.with_settings(ohmicide_s.baseline(), {5000: 1.0})


def test_the_patch_lowers_to_the_vst2_chunk_and_a_vst3_bank_with_juce_s_trailer() -> None:
    patch = conversion.convert(chunk(B2_Shape=0.25)).patch
    target = CATALOG.by_map_id("ohm-force.ohmicide-s")
    assert target is not None
    assert target.lower(patch, PluginKind.VST).state == patch
    vst3 = target.lower(patch, PluginKind.VST3)
    assert vst3.state.startswith(b"VstW") and vst3.state[32:36] == b"OhcS" and vst3.controller == b""
    assert vst3.state.endswith(bytes(16) + JUCE_PRIVATE_DATA_TAG)
    assert target.lift(vst3, PluginKind.VST3) == patch


def test_a_plan_offers_ohmicide_s_as_a_vst2_and_a_vst3() -> None:
    source = CATALOG.find(ohmicide.OHMICIDE_VST2)
    assert source is not None
    machine = Machine(vst2=(), vst3={}, au=lambda identifier: False, loadable=lambda info: False)
    targets = {target.id: target for target in mapped_targets(source, PluginKind.VST, machine)}
    assert set(targets) == {"ohmicide_s_vst2", "ohmicide_s_vst3"}
    assert targets["ohmicide_s_vst3"].format is PluginKind.VST3 and targets["ohmicide_s_vst3"].route is Route.CONVERTER
