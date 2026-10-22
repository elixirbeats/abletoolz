from __future__ import annotations

import json
import struct
from dataclasses import dataclass, replace
from pathlib import Path
from typing import assert_type
from xml.etree import ElementTree as ET

import pytest

from abletoolz.formats import hex_text
from abletoolz.plugins.container.device import PluginDevice
from abletoolz.plugins.state import StateTransformError, registered_custom_states
from abletoolz.plugins.state.controls import (
    FieldSamples,
    Float32Affine,
    Parameter,
    ParameterBinding,
    ParameterCurves,
    ParameterMap,
    ParameterStatus,
)
from abletoolz.plugins.state.izotope_trash import STATE_FORMAT as TRASH1_FORMAT
from abletoolz.plugins.state.izotope_trash import Bank
from abletoolz.plugins.state.izotope_trash import plugin_map as trash1_map
from abletoolz.plugins.state.izotope_trash2 import STATE_FORMAT as TRASH2_FORMAT
from abletoolz.plugins.state.izotope_trash2 import Trash2State
from abletoolz.plugins.state.izotope_trash2 import plugin_map as trash2_map
from abletoolz.plugins.state.model import FieldEncoding, FieldKey, FixedByteMap, MappedState, StateField
from abletoolz.plugins.state.plugin_map import PluginMap, StateFormat

FIXTURES = Path(__file__).parent / "state_fixtures" / "trash"


def layout() -> FixedByteMap:
    return FixedByteMap("test/float", 4, (StateField(12, "Amount", FieldEncoding.FLOAT32_LE, 0),))


def mapped_plugin() -> PluginMap[MappedState]:
    storage = layout()
    return PluginMap(
        "test.effect",
        "Test effect",
        None,
        "Unit fixture",
        (StateFormat("test/raw-float", storage.decode, "One scalar", (storage,)),),
    )


def test_explicit_formats_keep_vendor_models_and_refuse_missing_codecs() -> None:
    knowledge = mapped_plugin()
    raw = struct.pack("<f", 0.5)
    result = knowledge.decode("test/raw-float", raw)
    assert_type(result, MappedState)
    assert result.value("Amount") == 0.5
    assert result.encode() == raw
    with pytest.raises(StateTransformError, match="Unknown state format"):
        knowledge.decode("guessed-from-bytes", raw)
    with pytest.raises(StateTransformError, match="requires 4 bytes"):
        knowledge.decode("test/raw-float", b"short")
    with pytest.raises(StateTransformError, match="immutable bytes"):
        knowledge.decode("test/raw-float", bytearray(raw))  # type: ignore[arg-type]  # Exercise an untyped caller.
    description = replace(knowledge.formats[0], decoder=None)
    with pytest.raises(StateTransformError, match="No codec"):
        description.decode(raw)
    assert description.byte_map("test/float") is knowledge.formats[0].byte_maps[0]
    with pytest.raises(StateTransformError, match="Unknown byte map"):
        description.byte_map("other-layout")


@pytest.mark.parametrize(
    "change", ["duplicate_format", "empty_formats", "bad_digest", "duplicate_digest", "blank_scope"]
)
def test_plugin_map_refuses_ambiguous_identity_and_scope(change: str) -> None:
    knowledge = mapped_plugin()
    with pytest.raises(StateTransformError):
        match change:
            case "duplicate_format":
                replace(knowledge, formats=knowledge.formats * 2)
            case "empty_formats":
                replace(knowledge, formats=())
            case "bad_digest":
                replace(knowledge, binary_sha256=("not-a-sha256",))
            case "duplicate_digest":
                replace(knowledge, binary_sha256=("a" * 64, "a" * 64))
            case "blank_scope":
                replace(knowledge, scope=" ")


def test_format_owns_the_actual_layout_referenced_by_parameter_knowledge() -> None:
    owned, foreign = layout(), layout()
    samples = ParameterMap(
        foreign,
        (Parameter(1, "Amount", "", (FieldSamples(12, (0.0, 1.0)),), ParameterStatus.NATIVE_CHECKED),),
        (0.0, 1.0),
        "a" * 64,
        "b" * 64,
        "Unit fixture",
    )
    curves = ParameterCurves(foreign, (ParameterBinding(1, 12, "Amount", "", Float32Affine(1, 0)),), "Unit fixture")
    for parameters, executable in (((samples,), ()), ((), (curves,))):
        with pytest.raises(StateTransformError, match="owned by this state format"):
            StateFormat("test/root", owned.decode, "Unit fixture", (owned,), parameters, executable)
    with pytest.raises(StateTransformError, match="Duplicate byte-map ID"):
        StateFormat("test/root", owned.decode, "Unit fixture", (owned, foreign))


def test_parameter_curves_support_new_laws_and_reject_conflicting_edits() -> None:
    class Quadratic:
        def __call__(self, normalized: float) -> float:
            return normalized * normalized

    curves = ParameterCurves(
        layout(),
        (ParameterBinding(7, 12, "Amount", "", Quadratic()),),
        "Measured fixture inputs only",
    )
    assert curves.values({7: 0.3}) == {12: 0.09}
    for index, value in ((True, 0.5), (8, 0.5), (7, float("nan")), (7, 1.1)):
        with pytest.raises(StateTransformError):
            curves.values({index: value})
    shared = replace(curves, bindings=curves.bindings + (ParameterBinding(8, 12, "Other", "", Quadratic()),))
    with pytest.raises(StateTransformError, match="same saved field"):
        shared.values({7: 0.3, 8: 0.7})
    with pytest.raises(StateTransformError, match="Duplicate executable"):
        replace(curves, bindings=curves.bindings * 2)
    with pytest.raises(StateTransformError, match="Unknown field"):
        replace(curves, bindings=(ParameterBinding(7, 999, "Missing", "", Quadratic()),))


def test_plugin_map_accepts_structural_addresses_and_an_unlisted_codec() -> None:
    @dataclass(frozen=True)
    class PathField:
        id: int
        name: str
        path: tuple[str, ...]

    @dataclass(frozen=True)
    class PathMap:
        format_id: str
        fields: tuple[PathField, ...]

        def field(self, key: FieldKey) -> PathField:
            for field in self.fields:
                if key == field.id or key == field.name:
                    return field
            raise StateTransformError(f"Unknown field {key!r}")

    @dataclass(frozen=True)
    class Document:
        raw: bytes

        def encode(self) -> bytes:
            return self.raw

    def decode(raw: bytes) -> Document:
        prefix, payload = raw.split(b"\x00", 1)
        if prefix != b"vendor-7":
            raise StateTransformError("Wrong vendor envelope")
        json.loads(payload)
        return Document(raw)

    paths = PathMap("vendor-7/nested-document", (PathField(87, "Cutoff", ("engine", "cutoff")),))
    knowledge = PluginMap(
        "vendor.synth",
        "Unknown synth",
        None,
        "Format fixture",
        (
            StateFormat("vendor-7/envelope", decode, "Vendor-prefixed document", (paths,)),
            StateFormat("vendor-8/unmapped", None, "Description only"),
        ),
    )
    raw = b'vendor-7\x00{"engine":{"cutoff":0.7},"unknown":[1,"retained"]}'
    model = knowledge.decode("vendor-7/envelope", raw)
    assert_type(model, Document)
    assert model.encode() == raw
    assert knowledge.formats[0].byte_map(paths.format_id).field(87).name == "Cutoff"
    with pytest.raises(StateTransformError, match="No codec"):
        knowledge.decode("vendor-8/unmapped", raw)


def test_trash_bank_map_preserves_mixed_revisions_without_promoting_a_converter() -> None:
    before = registered_custom_states()
    knowledge = trash1_map()
    raw = (FIXTURES / "trash1-mixed-revisions.state").read_bytes()
    bank = knowledge.decode(TRASH1_FORMAT, raw)
    assert_type(bank, Bank)
    assert bank.encode() == raw
    assert len(knowledge.formats[0].byte_maps[0].fields) == 296
    assert len(knowledge.formats[0].parameter_maps[0].parameters) == 182
    old = next(i for i, program in enumerate(bank.programs) if program.revision == 21)
    opaque = next(i for i, program in enumerate(bank.programs) if program.revision == 22)
    changed = bank.with_values(old, {"PreFilter: Node 1 Freq": 823.25})
    assert changed.program(opaque) == bank.program(opaque)
    with pytest.raises(StateTransformError, match="unmapped"):
        changed.program(opaque).settings.value(0)
    trash2_map()
    assert registered_custom_states() == before
    assert not any("trash" in name for name in before)


@pytest.mark.parametrize("trial", range(3))
def test_trash_target_map_replays_native_vectors_through_the_host_boundary(trial: int) -> None:
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    name = f"trash2-reference{trial}.state"
    reference = next(row for row in manifest["files"] if row["file"] == name)
    changes = {index: value for index, value in reference["controls"]}
    raw = (FIXTURES / "trash2-default.state").read_bytes()
    info = ET.fromstring(
        '<VstPluginInfo><Preset><VstPreset><Buffer/><ProgramNumber Value="0"/></VstPreset></Preset></VstPluginInfo>'
    )
    processor = info.find("Preset/VstPreset/Buffer")
    assert processor is not None
    processor.text = hex_text.encode(raw, 5)
    device = PluginDevice(info)
    codec = trash2_map().format(TRASH2_FORMAT)
    state = device.decode_state(codec.decode)
    assert_type(state, Trash2State)
    device.set_state_model(state.with_parameters(changes))
    assert device.saved_state.state == (FIXTURES / name).read_bytes()
    assert device.saved_state.program_number == 0
    original = ET.tostring(info)
    with pytest.raises(StateTransformError):
        device.set_state_model(state.with_parameters({4: float("nan")}))
    assert ET.tostring(info) == original


@pytest.mark.parametrize("normalized,fixture", [(0.6, "trash2-pre-center.state"), (0.6000001, "trash2-pre-next.state")])
def test_trash_pre_gain_matches_conditioned_native_zero_crossing(normalized: float, fixture: str) -> None:
    knowledge = trash2_map()
    baseline = knowledge.decode(TRASH2_FORMAT, (FIXTURES / "trash2-default.state").read_bytes())
    changed = baseline.with_parameters({122: normalized})
    assert changed.encode() == (FIXTURES / fixture).read_bytes()
    assert changed.settings.value(122) != baseline.settings.value(122)


def test_native_preset_label_is_preserved_as_an_unsupported_template_context() -> None:
    knowledge = trash2_map()
    raw = (FIXTURES / "trash2-loaded-name.state").read_bytes()
    assert b"<Default>" in raw
    with pytest.raises(StateTransformError, match="opaque template"):
        knowledge.decode(TRASH2_FORMAT, raw)
    baseline = knowledge.decode(TRASH2_FORMAT, (FIXTURES / "trash2-default.state").read_bytes())
    wrong = FixedByteMap("different-layout", len(baseline.encode()), baseline.settings.map.fields)
    with pytest.raises(StateTransformError, match="measured core map"):
        Trash2State(wrong.decode(baseline.encode()))
