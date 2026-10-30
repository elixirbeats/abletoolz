"""The legacy KORG MS-20's program, read and edited by control name, against what the hosted plugin printed.

``state_fixtures/korg_ms20/ms20_hosted.json`` is the original plugin's own readings:
every control's display after restoring each distinct working program of
MS-20 and Legacy Cell devices in real sets, and each line slot's display at 21 restored values.
The banks built here follow the measured layout around those programs, with made-up
program names, so no factory content goes into the tests.
"""

from __future__ import annotations

import json
import os
import pathlib
import struct
from typing import Any

import pytest

from abletoolz.plugins.identity.ids import PluginKind
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.korg_ms20 import (
    BANK_BYTES,
    CONTROL_BY_NAME,
    CONTROLS,
    MS20_VST2,
    PROGRAM_BYTES,
    PROGRAMS,
    SLOTS,
    VERSION,
    Cable,
    Choice,
    Line,
    Ms20Bank,
    Ms20Program,
    Ms20Value,
)
from abletoolz.plugins.state.plugin_map import FormatState

FIXTURES = pathlib.Path(__file__).parent / "state_fixtures" / "korg_ms20"
HOSTED: dict[str, Any] = json.loads((FIXTURES / "ms20_hosted.json").read_text(encoding="utf-8"))
# Real sets' MS-20 and Legacy Cell banks, named by their SHA-256; only the machine that holds the sets has them.
CORPUS = pathlib.Path(
    os.environ.get(
        "ABLETOOLZ_MS20_BANKS",
        pathlib.Path(__file__).parents[1] / "unfinished_sandbox/format_translation/ms20_instrument/corpus",
    )
)


def program(entry: dict[str, Any], name: str = "Test Program") -> bytes:
    """A program in the measured layout holding a fixture entry's slots and cables."""
    head = b"prog" + struct.pack("<I", PROGRAM_BYTES - 8) + VERSION + bytes(4) + name.encode().ljust(32, b"\0")
    body = head + bytes.fromhex(entry["slots"]) + bytes.fromhex(entry["cables"])
    return body.ljust(PROGRAM_BYTES, b"\0")


def bank(working: bytes, *, selected: int = 0) -> bytes:
    """An MS-20 bank in the measured layout: a global block, 32 copies of ``working`` and the working pair."""
    globals_ = b"bolg" + bytes(5) + bytes.fromhex("040201") + bytes(0x800 - 12)
    tail = struct.pack("<II", selected, 1) + bytes(8)
    return globals_ + working * PROGRAMS + tail + working * 2


def plain() -> Ms20Program:
    return Ms20Program.parse(program(HOSTED["programs"][0]))


def shows(control_name: str, value: Ms20Value, display: str) -> bool:
    """Whether ``value`` is what the plugin printed: two decimals for a line, the label for a choice."""
    curve = CONTROL_BY_NAME[control_name].curve
    text = display.replace(" ", "")
    if isinstance(curve, Line):
        text = text.replace("+", "").replace("CENTER", "0")
        number = -float(text[1:]) if text.startswith("L") else float(text.lstrip("R"))
        return abs(number - float(value)) <= 0.0051 * max(1.0, (curve.high - curve.low) / 10)
    try:
        return int(text) == value
    except ValueError:
        return display.strip() == value


@pytest.mark.parametrize("entry", HOSTED["programs"], ids=lambda entry: entry["slots"][:12])
def test_every_real_working_program_reads_as_the_plugin_printed_it(entry: dict[str, Any]) -> None:
    values = Ms20Program.parse(program(entry)).values()
    for control in CONTROLS:
        assert shows(control.name, values[control.name], entry["displays"][str(control.index)]), control.name


@pytest.mark.parametrize("control", [c for c in CONTROLS if isinstance(c.curve, Line)], ids=lambda c: c.name)
def test_every_line_slot_reads_as_the_plugin_printed_it_at_21_values(control: Any) -> None:
    for written, display in HOSTED["line_restores"][str(control.slot)]:
        assert shows(control.name, control.curve.value(written), display), (written, display)


@pytest.mark.parametrize("control", [c for c in CONTROLS if isinstance(c.curve, Choice)], ids=lambda c: c.name)
def test_each_choice_item_s_own_value_reads_as_that_item(control: Any) -> None:
    curve: Choice = control.curve
    for stored, label in curve.items:
        assert curve.value(stored) == label
        assert curve.normalized(label) == stored


def test_a_value_inside_a_boundary_s_unresolved_stretch_is_refused() -> None:
    curve = CONTROL_BY_NAME["VCO 1 Wave Form"].curve
    assert isinstance(curve, Choice)
    low, high = curve.cuts[0]
    with pytest.raises(StateTransformError, match="between two MS-20 items"):
        curve.value((low + high) / 2)


def test_every_slot_is_a_control_or_named_as_unmeasured_or_unused() -> None:
    from abletoolz.plugins.state.korg_ms20 import UNMEASURED, UNUSED

    slots = sorted([control.slot for control in CONTROLS] + list(UNMEASURED) + list(UNUSED))
    assert slots == list(range(SLOTS))


def test_the_cables_read_one_output_to_one_input_by_jack_name() -> None:
    cables = max((Ms20Program.parse(program(entry)).cables for entry in HOSTED["programs"]), key=len)
    assert len(cables) == 8
    assert all(cable.out.endswith(" OUT") and cable.to.endswith(" IN") for cable in cables)
    assert Cable("MG TRI OUT", "SAMPLE HOLD CLOCK IN", 2) in cables


def test_rewriting_a_program_s_own_cables_keeps_every_byte() -> None:
    """Some real records name the input first; rewriting them output first used to change the program."""
    for entry in HOSTED["programs"]:
        parsed = Ms20Program.parse(program(entry))
        assert parsed.with_cables(parsed.cables).raw == parsed.raw


def test_an_edit_writes_the_slot_and_the_cables_and_reads_back() -> None:
    edited = (
        plain()
        .with_values({"LPF Cutoff": 3.5, "VCO 1 Scale": "16'", "Transpose": -12, "Poly Mode": "Poly", "Pan": -2.0})
        .with_cables([Cable("MG TRI OUT", "VCO CV IN", 1), Cable("KBD CV OUT", "HPF CUTOFF IN")])
    )
    values = edited.values()
    assert values["LPF Cutoff"] == pytest.approx(3.5) and values["VCO 1 Scale"] == "16'"
    assert values["Transpose"] == -12 and values["Poly Mode"] == "Poly" and values["Pan"] == pytest.approx(-2.0)
    assert edited.cables == (Cable("MG TRI OUT", "VCO CV IN", 1), Cable("KBD CV OUT", "HPF CUTOFF IN", 0))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"LPF Cutof": 1.0}, "no measured control"),
        ({"LPF Cutoff": 11.0}, "outside"),
        ({"VCO 1 Scale": "64'"}, "no setting"),
        ({"Transpose": "12"}, "no setting"),
        ({"Volume": float("nan")}, "finite"),
    ],
)
def test_an_edit_the_plugin_cannot_hold_is_refused(change: dict[str, Ms20Value], message: str) -> None:
    with pytest.raises(StateTransformError, match=message):
        plain().with_values(change)


@pytest.mark.parametrize(
    ("cables", "message"),
    [
        ([Cable("MG TRI OUT", "VCO CV IN"), Cable("MG PW OUT", "VCO CV IN")], "one cable"),
        ([Cable("MG TRI OUT", "VCO CV IN")] * 18, "at most 17"),
    ],
)
def test_a_patch_panel_the_plugin_cannot_hold_is_refused(cables: list[Cable], message: str) -> None:
    with pytest.raises(StateTransformError, match=message):
        plain().with_cables(cables)


def test_a_cable_needs_an_output_and_an_input() -> None:
    with pytest.raises(StateTransformError, match="output jack"):
        Cable("VCO CV IN", "TOTAL IN")


def test_a_bank_reads_its_working_program_and_edits_only_it() -> None:
    raw = bank(program(HOSTED["programs"][0]), selected=5)
    parsed = Ms20Bank.parse(raw)
    assert len(raw) == BANK_BYTES and parsed.selected == 5
    edited = parsed.with_working(parsed.working.with_values({"Volume": 4.0}))
    assert edited.working.values()["Volume"] == pytest.approx(4.0)
    changed = [at for at in range(len(raw)) if raw[at] != edited.raw[at]]
    assert changed and all(BANK_BYTES - 2 * PROGRAM_BYTES <= at < BANK_BYTES - PROGRAM_BYTES for at in changed)


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (lambda raw: raw[:-1], "71,696 bytes"),
        (lambda raw: b"glob" + raw[4:], "opening with bolg"),
        (lambda raw: raw[: 0x800 * 3] + b"prug" + raw[0x800 * 3 + 4 :], "no program at byte"),
        (lambda raw: raw[: BANK_BYTES - 0x1000 - 16] + struct.pack("<I", 40) + raw[BANK_BYTES - 0x1000 - 12 :], "32"),
    ],
)
def test_a_bank_off_the_measured_layout_is_refused(damage: Any, message: str) -> None:
    with pytest.raises(StateTransformError, match=message):
        Ms20Bank.parse(damage(bank(program(HOSTED["programs"][0]))))


def test_another_program_version_is_refused() -> None:
    raw = bytearray(program(HOSTED["programs"][0]))
    raw[8:12] = bytes.fromhex("2e000000")
    with pytest.raises(StateTransformError, match="version"):
        Ms20Program.parse(bytes(raw))


def test_an_ms20_device_and_its_jbridge_wrapper_find_the_map_by_uniqueid() -> None:
    ms20 = CATALOG.find(MS20_VST2)
    assert ms20 is not None and ms20.id == "korg.ms-20" and ms20.vst2 is not None
    raw = bank(program(HOSTED["programs"][0]))
    assert ms20.lift(FormatState(raw), PluginKind.VST) == raw


@pytest.mark.skipif(
    not CORPUS.is_dir(), reason="the MS-20 banks from real sets live only on the machine that holds them"
)
def test_every_ms20_bank_from_real_sets_reads() -> None:
    banks = [path for path in sorted(CORPUS.glob("*.state")) if path.stat().st_size == BANK_BYTES]
    assert banks
    for path in banks:
        Ms20Bank.parse(path.read_bytes()).working.values()
