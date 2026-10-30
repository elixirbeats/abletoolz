"""Pro-53's bank, read and edited by control name, against what the hosted plugin printed.

The banks built here follow the measured layout with made-up program names, so no
factory content goes into the tests. ``state_fixtures/pro53_hosted.json`` is the
original instrument's own readings on the legacy desktop: every control's display at
each of its 128 knob steps, and its getter and display after restoring banks from
real sets and banks this module edited.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import pathlib
import re
import struct
from collections.abc import Mapping
from typing import Any

import pytest

from abletoolz.plugins.identity.ids import PluginKind, Vst2Id
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.plugin_map import FormatState
from abletoolz.plugins.state.pro53 import (
    CONTROL_BY_NAME,
    CONTROLS,
    PRO53_VST2,
    PROGRAMS,
    RECORD_IDS,
    SNAPSHOT_FLOATS,
    Curve,
    Exponential,
    Line,
    Pro53Bank,
    Pro53Value,
    Semitones,
    Switch,
)

FIXTURE = pathlib.Path(__file__).parent / "state_fixtures" / "pro53_hosted.json"
# Real sets' Pro-53 banks, named by their SHA-256; only the machine that holds the sets has them.
CORPUS = pathlib.Path(
    os.environ.get(
        "ABLETOOLZ_PRO53_BANKS",
        pathlib.Path(__file__).parents[1] / "unfinished_sandbox/format_translation/pro53_source_20261021/corpus",
    )
)
GLOBALS = (66, 67, 69)
# A plain patch: oscillator A at pitch with its saw on, open filter, release on, everything else off or centred.
PLAIN = {5: 64 / 127, 6: 1.0, 10: 1.0, 14: 70 / 127, 39: 64 / 127, 40: 64 / 127, 57: 1.0, 59: 1.0, 66: 0.5, 67: 0.5}


def name(text: str) -> bytes:
    encoded = text.encode("cp1252")
    return struct.pack("<I", len(encoded)) + encoded


def bank(sound: Mapping[int, float] = PLAIN, *, saved: str = "Test Patch", tail: int = 2076) -> bytes:
    """A Pro-53 bank in the measured layout, its working patch and snapshot both holding ``sound``."""
    values = [sound.get(index, 0.0) for index in range(SNAPSHOT_FLOATS)]
    block = bytearray(304)
    struct.pack_into("<ff", block, 0, values[66], values[67])
    struct.pack_into("<I", block, 16, 0)
    struct.pack_into(f"<{SNAPSHOT_FLOATS}f", block, 20, *values)
    out = bytearray(bytes.fromhex("3a010100000600000050524f2d3533") + struct.pack("<I", 304) + block)
    out += name(saved) + struct.pack("<II", 301, PROGRAMS)
    for program in range(PROGRAMS):
        out += name(f"Program {program}") + struct.pack("<II", 0, 0)
    out += name(saved) + struct.pack("<III", 0, 0, SNAPSHOT_FLOATS)
    for index in range(SNAPSHOT_FLOATS):
        if index in GLOBALS:
            out += struct.pack("<I", 0xFFFFFFFF)
            continue
        out += struct.pack("<I", RECORD_IDS[index]) + bytes(PROGRAMS) + bytes([round(values[index] * 127)])
    return bytes(out + bytes(tail))


def prints(curve: Curve, value: Pro53Value, display: str) -> bool:
    """Whether ``value`` is what the plugin printed as ``display``, to the digits it prints."""
    text = display.strip()
    if isinstance(curve, Switch):
        return text == ("on" if value else "off")
    match = re.match(r"-?[\d.]+", text)
    assert match is not None
    number = float(match.group())
    if isinstance(curve, Semitones):
        return number == value
    assert isinstance(value, float)
    if isinstance(curve, Exponential) and curve.unit == "Hz":
        return abs(value / number - 1) < 3e-5
    if isinstance(curve, Exponential) and curve.low < 0.001:
        return float(f"{value:.1g}") == number
    return number == math.floor(value + 0.5)


@pytest.fixture(scope="module")
def hosted() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


# -- the bank ----------------------------------------------------------------------


def test_a_bank_reads_its_working_patch_both_ways_and_keeps_every_byte() -> None:
    raw = bank()
    parsed = Pro53Bank.parse(raw)
    assert parsed.name == "Test Patch" and parsed.selected == 0
    assert parsed.sound[14] == pytest.approx(70 / 127) and parsed.knobs[14] == 70
    assert [parsed.knobs[index] for index in GLOBALS] == [None, None, None]
    assert parsed.encode() == raw


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"PRO-53" + bank()[6:], "opens with"),
        (bank()[:15] + struct.pack("<I", 300) + bank()[19:], "304 bytes"),
        (bank(tail=2075), "2075 bytes after its records"),
        (bank({**PLAIN, 3: 2.0}), "outside 0 to 1"),
        (bank().replace(struct.pack("<I", 1022), struct.pack("<I", 1999), 1), "control 14 is not saved"),
    ],
    ids=["signature", "block size", "tail", "snapshot", "record id"],
)
def test_a_bank_off_the_measured_layout_is_refused(raw: bytes, message: str) -> None:
    with pytest.raises(StateTransformError, match=message):
        Pro53Bank.parse(raw)


def test_a_pro_53_device_finds_its_map() -> None:
    pro53 = CATALOG.find(PRO53_VST2)
    assert pro53 is not None and pro53.id == "ni.pro-53"
    assert Vst2Id(int.from_bytes(b"P3SF", "big")) == PRO53_VST2
    assert pro53.lift(FormatState(bank()), PluginKind.VST) == bank()


# -- reading and editing by name ---------------------------------------------------


def test_the_plain_patch_reads_in_the_plugin_s_units() -> None:
    values = Pro53Bank.parse(bank()).values()
    assert len(values) == len(CONTROLS) == 69
    assert values["Oscillator A Frequency"] == 0 and values["Oscillator B Frequency"] == 0
    assert values["Oscillator A Sawtooth"] is True and values["Oscillator A Pulse"] is False
    assert values["Filter Cutoff"] == pytest.approx(120 * 70 / 127)
    assert values["Oscillator B Freq Fine"] == pytest.approx(100 * 64 / 127)
    assert values["Master Tune"] == 0.0 and values["Master Volume"] == 50.0
    assert values["Delay Effect Time"] == 1.0


def test_an_edit_writes_the_knob_and_the_snapshot_and_nothing_else() -> None:
    before = Pro53Bank.parse(bank())
    after = before.with_values({"Filter Cutoff": 60.0, "Oscillator A Frequency": -12, "Delay Effect On": True})
    assert after.values()["Filter Cutoff"] == pytest.approx(60.0, abs=120 / 127 / 2)
    assert after.values()["Oscillator A Frequency"] == -12 and after.values()["Delay Effect On"] is True
    for index in (14, 5, 30):
        knob = after.knobs[index]
        assert knob is not None and after.sound[index] == struct.unpack("<f", struct.pack("<f", knob / 127))[0]
    changed = [at for at, (a, b) in enumerate(zip(before.raw, after.raw, strict=True)) if a != b]
    allowed = {after.knob_offsets[index] for index in (14, 5, 30)}
    allowed |= {39 + 4 * index + byte for index in (14, 5, 30) for byte in range(4)}
    assert set(changed) <= allowed


def test_master_tune_and_volume_land_exactly_in_both_their_floats() -> None:
    after = Pro53Bank.parse(bank()).with_values({"Master Tune": 35.0, "Master Volume": 80.0})
    values = after.values()
    assert values["Master Tune"] == pytest.approx(35.0) and values["Master Volume"] == pytest.approx(80.0)
    assert struct.unpack_from("<ff", after.raw, 19) == (after.sound[66], after.sound[67])


@pytest.mark.parametrize("stray", [1.5, math.nan])
def test_master_tune_and_volume_read_from_the_snapshot_the_engine_plays(stray: float) -> None:
    """Rendered: a 1.0 in the other copy left the pitch and level as they were, and the snapshot's moved them."""
    raw = bytearray(bank())
    struct.pack_into("<ff", raw, 19, stray, stray)
    values = Pro53Bank.parse(bytes(raw)).values()
    assert values["Master Tune"] == 0.0 and values["Master Volume"] == 50.0


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"Cutoff": 10.0}, "no control named"),
        ({"Filter Cutoff": 121.0}, "outside 0.0 to 120.0"),
        ({"Delay Effect Time": 0.5}, "outside 1.0 to 1000.0"),
        ({"Oscillator A Frequency": 25}, "outside"),
        ({"Oscillator A Frequency": 1.5}, "whole semitones"),
        ({"Delay Effect On": 1}, "True or False"),
        ({"Filter Cutoff": math.nan}, "finite"),
    ],
)
def test_an_edit_the_plugin_cannot_hold_is_refused(change: dict[str, Pro53Value], message: str) -> None:
    with pytest.raises(StateTransformError, match=message):
        Pro53Bank.parse(bank()).with_values(change)


@pytest.mark.parametrize("control", [c for c in CONTROLS if not isinstance(c.curve, Switch)], ids=lambda c: c.name)
def test_every_knob_step_s_value_writes_back_to_a_step_reading_the_same(control: Any) -> None:
    for step in range(128):
        value = control.curve.value(step / 127)
        written = round(control.curve.normalized(value) * 127)
        assert control.curve.value(written / 127) == pytest.approx(value, rel=1e-9)
        assert written == step or isinstance(control.curve, Semitones)


# -- against the hosted plugin -----------------------------------------------------


def test_every_control_is_named_and_ordered_as_the_plugin_names_it(hosted: dict[str, Any]) -> None:
    assert [control.name for control in CONTROLS] == list(hosted["displays"])
    assert hosted["plugin"]["uid"] == PRO53_VST2.unique_id and hosted["plugin"]["parameters"] == len(CONTROLS)


def test_every_knob_step_of_every_control_reads_as_the_plugin_prints_it(hosted: dict[str, Any]) -> None:
    for control in CONTROLS:
        for step, display in enumerate(hosted["displays"][control.name]):
            assert prints(control.curve, control.curve.value(step / 127), display), (control.name, step, display)


def _agrees_with_the_plugin(parsed: Pro53Bank, readings: Mapping[str, list[Any]]) -> None:
    values = parsed.values()
    for control, getter, display in zip(CONTROLS, readings["getters"], readings["displays"], strict=True):
        assert parsed.normalized(control) == pytest.approx(struct.unpack("<f", bytes.fromhex(getter))[0], abs=1e-7)
        assert prints(control.curve, values[control.name], display), (control.name, values[control.name], display)


def test_banks_this_module_edited_read_as_the_plugin_read_them(hosted: dict[str, Any]) -> None:
    if not hosted["edited"]:
        pytest.skip("the plugin has not restored an edited bank yet")
    for edit in hosted["edited"]:
        parsed = Pro53Bank.parse(bank()).with_values(edit["changes"])
        assert hashlib.sha256(parsed.raw).hexdigest() == edit["sha256"]
        _agrees_with_the_plugin(parsed, edit)


@pytest.mark.skipif(not CORPUS.is_dir(), reason="real sets' Pro-53 banks live on the machine that holds the sets")
def test_banks_from_real_sets_round_trip_and_read_as_the_plugin_read_them(hosted: dict[str, Any]) -> None:
    for digest, readings in hosted["real_banks"].items():
        raw = (CORPUS / f"{digest}.state").read_bytes()
        parsed = Pro53Bank.parse(raw)
        assert parsed.encode() == raw
        _agrees_with_the_plugin(parsed, readings)


@pytest.mark.skipif(not CORPUS.is_dir(), reason="real sets' Pro-53 banks live on the machine that holds the sets")
def test_banks_from_real_sets_edited_here_read_as_the_plugin_read_them(hosted: dict[str, Any]) -> None:
    if not hosted["edited_real"]:
        pytest.skip("the plugin has not restored an edited bank yet")
    for edit in hosted["edited_real"]:
        parsed = Pro53Bank.parse((CORPUS / f"{edit['source']}.state").read_bytes()).with_values(edit["changes"])
        assert hashlib.sha256(parsed.raw).hexdigest() == edit["sha256"]
        _agrees_with_the_plugin(parsed, edit)


def test_every_control_has_a_name_the_lookup_finds() -> None:
    assert all(CONTROL_BY_NAME[control.name] is control for control in CONTROLS)
    assert isinstance(CONTROL_BY_NAME["Filter Cutoff"].curve, Line)
