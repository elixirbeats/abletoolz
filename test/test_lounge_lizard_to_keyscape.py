"""Lounge Lizard EP-4 onto Keyscape's Rhodes - Classic: the reader, Keyscape's framing and edits, the converter."""

from __future__ import annotations

import json
import zlib

import pytest

from abletoolz.plugins.converters import lounge_lizard as conversion
from abletoolz.plugins.identity.ids import PluginKind
from abletoolz.plugins.state import keyscape, lounge_lizard
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.measured import StateEvidence
from abletoolz.plugins.state.plugin_map import FormatState

# The EP-4's schema, section by section, as its version 6 table names it.
SCHEMA = {
    "Mallet": ["Stiffness", "Force"],
    "Tremolo": ["Shape", "Speed", "Sync On", "Sync Rate", "Stereo", "Depth", "On"],
    "Effect 1": ["On", "Type", "Param 1"],
    "Effect 2": ["On", "Type", "Param 1"],
    "Reverb": ["Size", "Decay", "Mix", "On"],
    "Global": ["Volume", "Tune"],
}
DEFAULTS: dict[str, float | str] = {
    "Mallet/Stiffness": 161.0,
    "Mallet/Force": 0.132,
    "Tremolo/Shape": 2.0,
    "Tremolo/Speed": 1.3,
    "Tremolo/Sync On": 1.0,
    "Tremolo/Sync Rate": 14.0,
    "Tremolo/Stereo": 2.0,
    "Tremolo/Depth": 0.35,
    "Tremolo/On": 2.0,
    "Effect 1/On": 1.0,
    "Effect 1/Type": 1.0,
    "Effect 1/Param 1": 0.5,
    "Effect 2/On": 1.0,
    "Effect 2/Type": 4.0,
    "Effect 2/Param 1": "none",
    "Reverb/Size": 3.0,
    "Reverb/Decay": 0.95,
    "Reverb/Mix": 0.06,
    "Reverb/On": 2.0,
    "Global/Volume": 1.0,
    "Global/Tune": 0.0,
}


def lua(text: str) -> str:
    return json.dumps(text)


def ep4(name: str = "Synthetic", **changes: float | str) -> bytes:
    """A synthetic EP-4 state: a tuning stub, the program name, and the parameters table."""
    values = {**DEFAULTS, **{key.replace("__", "/").replace("_", " "): value for key, value in changes.items()}}
    schema = ",".join("{" + lua(section) + "}," + ",".join(lua(n) for n in names) for section, names in SCHEMA.items())
    order = [f"{section}/{n}" for section, names in SCHEMA.items() for n in names]
    stored = ",".join(lua(v) if isinstance(v, str) else repr(v) for v in (values[key] for key in order))
    return (
        '{\n\ttuning = {\n\t\ttype = "scala",\n\t},\n'
        f"\tprogramName = {lua(name)},\n"
        "\tparameters = {\n"
        f"\t\tversion = {lua('{6,' + lua('Lounge Lizard EP-4') + ',' + lua('4.0.3') + '}')},\n"
        f"\t\tschema = {lua('{' + schema + '}')},\n"
        f"\t\tvalues = {lua('{' + stored + '}')},\n"
        "\t},\n}"
    ).encode()


def test_the_reader_names_every_control_in_engine_units() -> None:
    state = ep4("Tine at 9", Tremolo__Speed=4.85)
    values = lounge_lizard.values(state)
    assert len(values) == len(DEFAULTS) and values["Tremolo/Speed"] == 4.85
    assert values["Effect 2/Param 1"] == "none" and lounge_lizard.program_name(state) == "Tine at 9"


@pytest.mark.parametrize(
    "state",
    [b"", b"{\n}", ep4().replace(b"Lounge Lizard EP-4", b"Lounge Lizard EP-3"), ep4().replace(b"0.35", b"[1]")],
)
def test_a_state_the_ep4_did_not_write_is_refused(state: bytes) -> None:
    with pytest.raises(StateTransformError):
        lounge_lizard.values(state)


def test_the_converter_joins_the_two_maps_and_is_hosted() -> None:
    found = CATALOG.converter("aas.lounge-lizard-ep-4", "spectrasonics.keyscape")
    assert found is not None and found.measured.evidence == (StateEvidence.HOSTED,)
    assert CATALOG.find(lounge_lizard.LOUNGE_LIZARD_EP4_VST2) is CATALOG.by_map_id("aas.lounge-lizard-ep-4")
    assert CATALOG.find(keyscape.KEYSCAPE_VST3) is CATALOG.by_map_id("spectrasonics.keyscape")


def test_keyscape_s_vst3_state_is_its_chunk_behind_a_header() -> None:
    target = CATALOG.by_map_id("spectrasonics.keyscape")
    assert target is not None
    patch = keyscape.rhodes_classic()
    vst3 = target.lower(patch, PluginKind.VST3)
    assert vst3.state[:24] == bytes.fromhex("ffc99a3b000000000100000000000000") + len(patch).to_bytes(8, "little")
    assert vst3.state.endswith(patch + bytes(4)) and vst3.controller == b""
    assert target.lift(vst3, PluginKind.VST3) == patch == target.lower(patch, PluginKind.VST).state
    with pytest.raises(StateTransformError):
        target.lift(FormatState(vst3.state[:-1]), PluginKind.VST3)


def test_an_edit_writes_the_attributes_a_control_drives_and_its_knob() -> None:
    patch = keyscape.edit(keyscape.rhodes_classic(), conversion.DEPTH, lambda *_: 0.25)
    assert keyscape.driven(patch, conversion.DEPTH) == [("swing", 0.25), ("swing", 0.25)]
    assert keyscape.knobs(patch)[conversion.DEPTH] == 0.25
    with pytest.raises(StateTransformError, match="drives nothing"):
        keyscape.edit(patch, 70, lambda *_: 0.0)


def test_a_choice_takes_each_attribute_s_switch_value_for_the_item() -> None:
    chorus = keyscape.choose(keyscape.rhodes_classic(), conversion.MODULATION_FX, conversion.CHORUS_1)
    actives = [value for attribute, value in keyscape.driven(chorus, conversion.MODULATION_FX) if attribute == "Active"]
    assert actives == [1.0, 0.0, 0.0]
    with pytest.raises(StateTransformError, match="no item 9"):
        keyscape.choose(chorus, conversion.MODULATION_FX, 9)


def settings(state: bytes) -> dict[str, float]:
    patch = conversion.convert(state).patch
    return {
        "tremolo": keyscape.knobs(patch)[conversion.TREMOLO],
        "pan": keyscape.knobs(patch)[conversion.AUTO_PAN],
        **{
            f"{c}:{a}": v
            for c in (conversion.DEPTH, conversion.SPEED, conversion.REVERB_AMOUNT)
            for a, v in keyscape.driven(patch, c)
        },
        "studio": dict(keyscape.driven(patch, conversion.REVERB_TYPE))["pauxrl"],
    }


def test_the_tremolo_crosses_switch_by_switch_and_by_calibration() -> None:
    plain = settings(ep4(Tremolo__Stereo=1.0, Tremolo__On=1.0))
    assert (plain["tremolo"], plain["pan"]) == (0.0, 0.0)
    slow, fast = settings(ep4(Tremolo__Speed=1.3)), settings(ep4(Tremolo__Speed=4.0))
    assert slow["39:rate"] < fast["39:rate"] and slow["39:damp0"] > fast["39:damp0"]
    shallow, deep = settings(ep4(Tremolo__Depth=0.1)), settings(ep4(Tremolo__Depth=0.6))
    assert shallow["2:swing"] < deep["2:swing"]


def test_the_reverb_is_the_studio_reverb_at_a_send_that_grows_with_the_mix() -> None:
    dry = settings(ep4(Reverb__On=1.0))
    assert dry["studio"] == 0.0 and dry["25:pauxl"] == 0.0
    small, large = settings(ep4(Reverb__Mix=0.05)), settings(ep4(Reverb__Mix=0.15))
    assert small["studio"] == 1.0 and 0 < small["25:pauxl"] < large["25:pauxl"]
    notices: list[str] = []
    assert conversion.plan(ep4(Reverb__Mix=0.4), notices)["reverb_amount"] == 1.0
    assert "wetter than Keyscape's full send" in notices[-1]


@pytest.mark.parametrize(("mix", "send"), [(0.002, 0.00607), (0.01623, 0.05), (0.06191, 0.2), (0.2481, 1.0)])
def test_the_send_puts_the_wet_level_at_the_mix_s_wet_to_dry_ratio_however_dry(mix: float, send: float) -> None:
    notices: list[str] = []
    assert conversion.plan(ep4(Reverb__Mix=mix), notices)["reverb_amount"] == pytest.approx(send, abs=1e-3)
    assert not any("reverb mix" in notice for notice in notices)


def test_a_chorus_or_phaser_is_keyscape_s_and_the_rest_is_said() -> None:
    converted = conversion.convert(ep4(Effect_1__On=2.0, Effect_1__Type=2.0, Effect_2__On=2.0, Effect_2__Type=4.0))
    item = [v for a, v in keyscape.driven(converted.patch, conversion.MODULATION_FX) if a == "Active"]
    assert item == [1.0, 0.0, 0.0]
    assert "the distort in effect 1" in converted.notices[-1]
    with pytest.raises(StateTransformError, match="distort"):
        conversion.plan(ep4(Effect_1__On=2.0, Effect_1__Type=2.0))


def test_every_device_says_what_it_is_and_to_audition_it() -> None:
    notices = conversion.convert(ep4("BFly Rhodes 1")).notices
    assert notices[0] == "Lounge Lizard EP-4 program 'BFly Rhodes 1'." and notices[1] == conversion.NOTE
    assert "audition this device" in conversion.NOTE


def lounge3(
    on_off: str = "1", depth: str = "0.36", mix: str = "0.2", reverb: str = "1", name: str = "Old School"
) -> bytes:
    """A synthetic Lounge Lizard 3 state: AASZ, the inflated length, then the zlib-compressed bank."""
    patch = (
        "main_synth ll version 2\n{\n"
        f"  tremolo Tremolo version 1\n  {{\n     on_off {on_off}\n     depth {depth}\n  }}\n"
        f"  fx_wrapper Reverb version 1\n  {{\n     on_off {reverb}\n  }}\n"
        f"  reverberator Reverb_Module version 1\n  {{\n     mix {mix}\n     bypass 0\n  }}\n}}\n"
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE mpv-state SYSTEM "mpv-state.dtd">\n'
        '<mpv-state version="3"><mpv-midi-bank current-program="2">'
        '<mpv-midi-program number="1" name="First"></mpv-midi-program>'
        f'<mpv-midi-program number="2" name="{name}">{patch}</mpv-midi-program></mpv-midi-bank>'
        f'<document type="loungelizard" version="2"><locked_data>{patch}</locked_data></document></mpv-state>'
    ).encode() + b"\x00"
    return f"AASZ {len(xml)}".ljust(16).encode() + zlib.compress(xml)


def test_lounge_lizard_3_reads_its_working_patch_and_selected_program() -> None:
    patch = lounge_lizard.lounge3(lounge3())
    assert patch.program == "Old School" and patch.fields["tremolo:Tremolo/depth"] == "0.36"
    with pytest.raises(StateTransformError):
        lounge_lizard.lounge3(lounge3()[:-3])
    with pytest.raises(StateTransformError):
        lounge_lizard.lounge3(b"AASZ 12345678901")


def test_lounge_lizard_3_carries_its_tremolo_and_reverb_switches() -> None:
    converted = conversion.convert_3(lounge3(on_off="0", reverb="0"))
    assert keyscape.knobs(converted.patch)[conversion.TREMOLO] == 0.0
    assert dict(keyscape.driven(converted.patch, conversion.REVERB_TYPE))["pauxrl"] == 0.0
    assert converted.notices == ("Lounge Lizard 3 program 'Old School'.", conversion.NOTE_3)
    wet = conversion.convert_3(lounge3(mix="0.3")).patch
    assert keyscape.knobs(wet)[conversion.TREMOLO] == 1.0 and keyscape.knobs(wet)[conversion.REVERB_AMOUNT] > 0
    with pytest.raises(StateTransformError, match="not off"):
        conversion.convert_3(lounge3(on_off="2"))
    assert CATALOG.converter("aas.lounge-lizard-3", "spectrasonics.keyscape") is not None
