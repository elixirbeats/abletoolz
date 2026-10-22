from __future__ import annotations

import hashlib
import json
import struct
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from abletoolz.formats import hex_text
from abletoolz.plugins.container.device import PluginDevice
from abletoolz.plugins.parsers.base import PluginData
from abletoolz.plugins.state import StateTransformError, registered_custom_states
from abletoolz.plugins.state.controls import FieldSamples, Parameter, ParameterMap, ParameterStatus
from abletoolz.plugins.state.izotope_trash import Bank, Program, byte_map, parameter_map
from abletoolz.plugins.state.izotope_trash2 import Trash2State
from abletoolz.plugins.state.model import ByteMap, FieldEncoding, FieldKey, FixedByteMap, StateField

FIXTURES = Path(__file__).parent / "state_fixtures" / "trash"


def test_parameter_map_accepts_a_new_structural_format_without_fixed_offsets() -> None:
    @dataclass(frozen=True)
    class RecordField:
        id: int
        name: str
        record: str
        key: str

    @dataclass(frozen=True)
    class RecordMap:
        format_id: str
        fields: tuple[RecordField, ...]

        def field(self, key: FieldKey) -> RecordField:
            for field in self.fields:
                if key == field.id or key == field.name:
                    return field
            raise StateTransformError(f"Unknown record field {key!r}")

    storage: ByteMap = RecordMap(
        "unregistered-vendor/variable-records-v7",
        (RecordField(41, "Cutoff", "filter-a", "frequency"),),
    )
    parameters = ParameterMap(
        byte_map=storage,
        parameters=(
            Parameter(2, "Cutoff", "Hz", (FieldSamples(41, (20.0, 20000.0)),), ParameterStatus.NATIVE_CHECKED),
        ),
        positions=(0.0, 1.0),
        source_sha256="a" * 64,
        evidence_sha256="b" * 64,
        scope="Structural-address fixture; only the two captured positions are mapped",
    )
    assert parameters.byte_map.field(41).name == "Cutoff"
    assert parameters.sampled_values(2, 1.0) == {41: 20000.0}
    with pytest.raises(StateTransformError, match="no measurement"):
        parameters.sampled_values(2, 0.5)


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


def test_original_source_control_map_retains_evidence_gaps_and_coupling() -> None:
    model = parameter_map()
    assert len(model.byte_map.fields) == 296
    assert len(model.parameters) == 182
    assert Counter(c.status for c in model.parameters) == {
        ParameterStatus.NATIVE_CHECKED: 167,
        ParameterStatus.ROUNDING_UNRESOLVED: 14,
        ParameterStatus.NO_SAVED_CHANGE: 1,
    }
    assert model.sampled_values(1, 0.5) == {31: -5.0, 32: -5.0}
    with pytest.raises(StateTransformError, match="no measurement"):
        model.sampled_values(1, 0.51)
    with pytest.raises(StateTransformError, match="No saved-field"):
        model.sampled_values(0, 1.0)
    assert not any("trash" in name.lower() for name in registered_custom_states())


def test_native_source_fixture_and_model_constructors_preserve_format_guards() -> None:
    raw = (FIXTURES / "trash1-default.state").read_bytes()
    model = Bank.parse(raw)
    assert model.encode() == raw
    for field in byte_map().fields:
        model.programs[0].settings.value(field.id)
    with pytest.raises(StateTransformError, match="sixteen"):
        Bank(model.programs[:-1])
    with pytest.raises(StateTransformError, match="Truncated"):
        Program(b"short", b"Name")


def test_historical_mixed_bank_retains_opaque_records_during_typed_edits() -> None:
    raw = (FIXTURES / "trash1-mixed-revisions.state").read_bytes()
    model = Bank.parse(raw)
    assert model.encode() == raw
    old_index = next(i for i, p in enumerate(model.programs) if p.revision == 21)
    opaque_index = next(i for i, p in enumerate(model.programs) if p.revision == 22)
    with pytest.raises(StateTransformError, match="unmapped"):
        _ = model.program(opaque_index).settings
    changed = model.with_values(old_index, {"PreFilter: Node 1 Freq": 823.25})
    assert changed.program(opaque_index) == model.program(opaque_index)
    assert Bank.parse(changed.encode()) == changed
    for index in (-1, 16):
        with pytest.raises(ValueError, match="index"):
            model.program(index)


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
    plugin = PluginData.from_element(info)
    bank = plugin.decode_state(Bank.parse)
    assert plugin.program_number == 3
    frequency = "PreFilter: Node 1 Freq"
    edited = bank.with_values(plugin.program_number, {frequency: 823.25})
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
    missing = PluginData.from_element(ET.fromstring("<VstPluginInfo/>"))
    assert missing.program_number is None
    with pytest.raises(ValueError):
        missing.decode_state(Bank.parse)
    with pytest.raises(ValueError):
        missing.set_state_model(state)
    info = ET.fromstring("<VstPluginInfo><Preset><VstPreset><Buffer/></VstPreset></Preset></VstPluginInfo>")
    buffer = info.find("Preset/VstPreset/Buffer")
    assert buffer is not None
    buffer.text = "original text"
    plugin = PluginData.from_element(info)

    class InvalidState:
        def encode(self) -> bytes:
            raise ValueError("invalid state")

    with pytest.raises(ValueError, match="invalid state"):
        plugin.set_state_model(InvalidState())
    assert buffer.text == "original text"


def test_canonical_device_bridge_preserves_vst3_controller_and_reads_program_fresh() -> None:
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
    compat = PluginData.from_element(info)
    assert compat.decode_state(BytesState).encode() == b"\x03"
    compat.set_state_model(BytesState(b"\x04"))
    assert device.saved_state.state == b"\x04"
    assert device.saved_state.controller == b"\x02"
    vst2 = ET.fromstring(
        '<VstPluginInfo><Preset><VstPreset><ProgramNumber Value="1"/><Buffer/></VstPreset></Preset></VstPluginInfo>'
    )
    plugin = PluginData.from_element(vst2)
    assert plugin.program_number == 1
    PluginDevice(vst2).set_saved_state_record(program_number=3)
    assert plugin.program_number == 3


@pytest.mark.parametrize("trial", range(3))
def test_library_target_curves_replay_independent_native_reference_chunks(trial: int) -> None:
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    name = f"trash2-reference{trial}.state"
    record = next(row for row in manifest["files"] if row["file"] == name)
    expected = (FIXTURES / name).read_bytes()
    assert hashlib.sha256(expected).hexdigest() == record["sha256"]
    controls = {index: value for index, value in record["controls"]}
    baseline = (FIXTURES / "trash2-default.state").read_bytes()
    model = Trash2State.parse(baseline).with_parameters(controls)
    assert model.encode() == expected
    assert Trash2State.parse(expected).encode() == expected


def test_partial_target_map_refuses_another_state_shape_even_at_the_same_length() -> None:
    raw = (FIXTURES / "trash2-default.state").read_bytes()
    changed = bytearray(raw)
    changed[100] ^= 1
    with pytest.raises(StateTransformError, match="opaque template"):
        Trash2State.parse(bytes(changed))
    model = Trash2State.parse(raw)
    assert model.with_parameters({4: 0.5}).settings.value(4) == -67.0
    for index, value in ((5, 0.5), (4, float("nan")), (4, -0.1), (4, 1.1)):
        with pytest.raises(StateTransformError):
            model.with_parameters({index: value})
    assert model.encode() == raw
