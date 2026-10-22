from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import replace
from pathlib import Path

import pytest

from abletoolz.plugins.state import StateTransformError, registered_custom_states
from abletoolz.plugins.state.izotope_trash import Bank
from abletoolz.plugins.state.izotope_trash import algorithms as source_algorithms
from abletoolz.plugins.state.izotope_trash2 import Trash2State
from abletoolz.plugins.state.izotope_trash2 import algorithms as target_algorithms
from abletoolz.plugins.state.model import EnumValue, FieldEncoding, FixedByteMap, StateField

FIXTURES = Path(__file__).parent / "state_fixtures/trash"


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


def test_algorithm_domains_are_version_local_and_do_not_install_a_converter() -> None:
    registered = registered_custom_states()
    source, target = source_algorithms(), target_algorithms()
    assert len(source) == 48 and len(target) == 52
    assert source[11].name == target[18].name == "Clip Control"
    assert source[36].name == "Bit Aliasing"
    assert "Bit Aliasing" not in {item.name for item in target}
    bank = Bank.parse((FIXTURES / "trash1-default.state").read_bytes())
    assert bank.program(0).settings.map.field("Trash: First Algorithm").choices is source
    changed = bank.with_values(3, {"Trash: First Algorithm": 47})
    assert changed.programs[:3] == bank.programs[:3]
    assert changed.programs[4:] == bank.programs[4:]
    with pytest.raises(StateTransformError, match="no mapped meaning"):
        bank.with_values(0, {"Trash: First Algorithm": 48})
    target_state = Trash2State.parse((FIXTURES / "trash2-default.state").read_bytes())
    assert target_state.settings.map.field("TRASH: Single Stg1 Algorithm").choices is target
    with pytest.raises(StateTransformError, match="no mapped meaning"):
        target_state.with_values({"TRASH: Single Stg1 Algorithm": 52})
    assert registered_custom_states() == registered


def test_saved_fields_predict_complete_independent_native_public_saves() -> None:
    manifest = json.loads((FIXTURES / "trash2-fields.json").read_bytes())
    baseline = Trash2State.parse((FIXTURES / "trash2-default.state").read_bytes())
    assert len(manifest["fields"]) == 56
    for row in manifest["fields"]:
        native = (FIXTURES / row["file"]).read_bytes()
        assert hashlib.sha256(native).hexdigest() == row["sha256"]
        predicted = baseline.with_values({row["field"]: row["physical_value"]})
        assert predicted.encode() == native, row["field"]
        assert Trash2State.parse(native).encode() == native
    with pytest.raises(StateTransformError, match="Unmapped executable"):
        baseline.with_parameters({141: 0.5})
    with pytest.raises(StateTransformError, match="Unknown field"):
        baseline.with_values({"TRASH: Stg1 Tension Scale": 0.5})


def test_algorithm_selectors_replay_native_reload_receipts_in_each_saved_slot() -> None:
    manifest = json.loads((FIXTURES / "trash2-selectors.json").read_bytes())
    baseline = Trash2State.parse((FIXTURES / "trash2-default.state").read_bytes())
    assert len(manifest["selectors"]) == 30
    for row in manifest["selectors"]:
        native = (FIXTURES / row["file"]).read_bytes()
        assert hashlib.sha256(native).hexdigest() == row["sha256"]
        predicted = baseline.with_values({row["field_id"]: row["value"]})
        assert predicted.encode() == native
        assert Trash2State.parse(native).encode() == native


@pytest.mark.parametrize("first,second", [(False, False), (True, False), (False, True), (True, True)])
def test_joint_polarity_edits_match_native_saves(first: bool, second: bool) -> None:
    baseline = Trash2State.parse((FIXTURES / "trash2-default.state").read_bytes())
    baseline = baseline.with_values(
        {
            "TRASH: Stg1 DC Offset": False,
            "TRASH: Stg2 DC Offset": False,
            "Limiter Enable": False,
            "TRASH: Stg2 Enable": True,
        }
    )
    predicted = baseline.with_stage_polarity(1, inverted=first).with_stage_polarity(2, inverted=second)
    native = (FIXTURES / f"trash2-polarity-{int(first)}{int(second)}.state").read_bytes()
    assert predicted.encode() == native
    assert Trash2State.parse(native).encode() == native
    assert predicted.with_stage_polarity(2, inverted=False).with_stage_polarity(2, inverted=second) == predicted


def test_partial_curve_writes_and_unmeasured_curve_framing_are_refused() -> None:
    raw = (FIXTURES / "trash2-default.state").read_bytes()
    state = Trash2State.parse(raw)
    with pytest.raises(StateTransformError, match="linear waveshaper"):
        state.with_values({"TRASH: Stg1 Shape Start Y": 1.0})
    with pytest.raises(StateTransformError):
        state.with_stage_polarity(True, inverted=True)
    with pytest.raises(StateTransformError):
        state.with_stage_polarity(3, inverted=False)
    for offset in (734, 1378, 1398):
        wrong = bytearray(raw)
        wrong[offset] ^= 1
        with pytest.raises(StateTransformError, match="opaque template"):
            Trash2State.parse(bytes(wrong))
    assert state.encode() == raw
