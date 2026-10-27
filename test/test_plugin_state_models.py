"""The state models plugin maps build on, and a device's patch read and written through one.

Byte maps and mapped states are checked on unit fixtures; the device bridge on
Trash 1's captured default bank.
"""

from __future__ import annotations

import struct
from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from abletoolz.formats import hex_text
from abletoolz.plugins.container.device import PluginDevice
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.izotope_trash import Bank
from abletoolz.plugins.state.model import EnumValue, FieldEncoding, FixedByteMap, StateField

FIXTURES = Path(__file__).parent / "state_fixtures" / "trash"


def test_field_maps_refuse_ambiguous_or_out_of_bounds_coordinates() -> None:
    first = StateField(0, "Frequency", FieldEncoding.FLOAT32_LE, 0)
    for second in (
        replace(first, name="Other", byte_offset=4),
        replace(first, id=1, byte_offset=4),
        replace(first, id=1, name="Other", byte_offset=2),
        replace(first, id=1, name="Other", byte_offset=6),
    ):
        with pytest.raises(StateTransformError):
            FixedByteMap("test/layout1", 8, (first, second))


def test_named_batch_edits_are_atomic_and_preserve_unmodelled_and_nan_bytes() -> None:
    layout = FixedByteMap(
        "test/layout1",
        13,
        (
            StateField(7, "Amount", FieldEncoding.FLOAT32_LE, 0),
            StateField(8, "Mode", FieldEncoding.UINT32_LE, 4),
            StateField(9, "Enable", FieldEncoding.BOOL8, 8),
        ),
    )
    raw = bytes.fromhex("0100c07f") + struct.pack("<I", 7) + b"\x01keep"
    model = layout.decode(raw)
    assert model.encode() == raw
    with pytest.raises(StateTransformError):
        model.with_values({"Amount": 4.0, "Mode": -1})
    assert model.encode() == raw
    changed = model.with_values({"Mode": 5, "Enable": False})
    assert changed.encode()[:4] == raw[:4]
    assert changed.encode()[9:] == b"keep"
    assert changed.value(8) == 5
    assert changed.value("Enable") is False
    with pytest.raises(StateTransformError, match="same field"):
        model.with_values({7: 4.0, "Amount": 5.0})
    with pytest.raises(StateTransformError, match="Boolean"):
        layout.decode(raw[:8] + b"\x02" + raw[9:]).value("Enable")


def test_enum_domain_preserves_unknown_reads_and_refuses_unmapped_writes_atomically() -> None:
    field = StateField(1, "Algorithm", FieldEncoding.UINT32_LE, 0, choices=(EnumValue(7, "Known"),))
    raw = struct.pack("<I", 999) + b"kept"
    state = FixedByteMap("test/enum", 8, (field,)).decode(raw)
    assert state.value("Algorithm") == 999
    assert state.with_values({}).encode() == raw
    assert state.with_values({1: 7}).encode() == struct.pack("<I", 7) + b"kept"
    for invalid in (8, True, 7.0):
        with pytest.raises(StateTransformError):
            state.with_values({1: invalid})
    assert state.encode() == raw
    with pytest.raises(StateTransformError, match="Duplicate enum"):
        replace(field, choices=field.choices * 2)
    with pytest.raises(StateTransformError, match="unsigned-word"):
        replace(field, encoding=FieldEncoding.FLOAT32_LE)
    for value, name in ((-1, "Invalid"), (True, "Invalid"), (0, " ")):
        with pytest.raises(StateTransformError):
            EnumValue(value, name)


def test_ableton_buffer_bridge_preserves_selected_program_and_surrounding_xml() -> None:
    raw = (FIXTURES / "trash1-default.state").read_bytes()
    info = ET.fromstring(
        '<VstPluginInfo><FileName Value="Trash.64.dll"/><UniqueId Value="1767527473"/>'
        '<Preset><VstPreset><ProgramNumber Value="3"/><Buffer/></VstPreset></Preset>'
        '<ParameterList><Unrelated Value="preserve"/></ParameterList></VstPluginInfo>'
    )
    buffer = info.find("Preset/VstPreset/Buffer")
    assert buffer is not None
    buffer.text = hex_text.encode(raw, 5)
    original_xml = ET.tostring(info)
    plugin = PluginDevice(info)
    bank = plugin.decode_state(Bank.parse)
    program = plugin.saved_state.program_number
    assert program == 3
    frequency = "PreFilter: Node 1 Freq"
    edited = bank.with_values(program, {frequency: 823.25})
    plugin.set_state_model(edited)
    assert plugin.decode_state(Bank.parse).programs[3].settings.value(frequency) == 823.25
    assert edited.programs[:3] == bank.programs[:3]
    assert edited.programs[4:] == bank.programs[4:]
    assert hex_text.depth(buffer.text) == 5
    buffer.text = hex_text.encode(raw, 5)
    assert plugin.decode_state(Bank.parse) == bank
    assert ET.tostring(info) == original_xml


def test_buffer_bridge_refuses_missing_buffers_and_does_not_mutate_on_encode_failure() -> None:
    state = Bank.parse((FIXTURES / "trash1-default.state").read_bytes())
    missing = PluginDevice(ET.fromstring("<VstPluginInfo/>"))
    assert missing.saved_state.program_number is None
    with pytest.raises(ValueError):
        missing.decode_state(Bank.parse)
    with pytest.raises(ValueError):
        missing.set_state_model(state)
    info = ET.fromstring("<VstPluginInfo><Preset><VstPreset><Buffer/></VstPreset></Preset></VstPluginInfo>")
    buffer = info.find("Preset/VstPreset/Buffer")
    assert buffer is not None
    buffer.text = "original text"
    plugin = PluginDevice(info)

    class InvalidState:
        def encode(self) -> bytes:
            raise ValueError("invalid state")

    with pytest.raises(ValueError, match="invalid state"):
        plugin.set_state_model(InvalidState())
    assert buffer.text == "original text"


def test_canonical_device_bridge_preserves_vst3_controller() -> None:
    info = ET.fromstring(
        "<Vst3PluginInfo><Preset><Vst3Preset><ProcessorState>01</ProcessorState>"
        "<ControllerState>02</ControllerState></Vst3Preset></Preset></Vst3PluginInfo>"
    )
    for tag, raw in (("ProcessorState", b"\x01"), ("ControllerState", b"\x02")):
        element = info.find(f"Preset/Vst3Preset/{tag}")
        assert element is not None
        element.text = hex_text.encode(raw, 5)

    class BytesState:
        def __init__(self, raw: bytes) -> None:
            self.raw = raw

        def encode(self) -> bytes:
            return self.raw

    device = PluginDevice(info)
    assert device.decode_state(BytesState).encode() == b"\x01"
    device.set_state_model(BytesState(b"\x03"))
    assert device.saved_state.state == b"\x03"
    assert device.saved_state.controller == b"\x02"
