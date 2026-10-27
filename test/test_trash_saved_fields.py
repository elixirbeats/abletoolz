"""Trash 2's saved fields, each checked against a save the plugin made itself.

Every fixture is a native save, named with its sha256 in a manifest: a write here is right when it is
that file byte for byte. The algorithm tables come from each version's own descriptor table.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.izotope_trash import Bank
from abletoolz.plugins.state.izotope_trash import algorithms as source_algorithms
from abletoolz.plugins.state.izotope_trash2 import Trash2State
from abletoolz.plugins.state.izotope_trash2 import algorithms as target_algorithms
from abletoolz.plugins.state.model import FixedByteMap

FIXTURES = Path(__file__).parent / "state_fixtures/trash"


def native(name: str, sha256: str | None = None) -> bytes:
    """A native save, checked against the digest its manifest recorded when there is one."""
    raw = (FIXTURES / name).read_bytes()
    if sha256 is not None:
        assert hashlib.sha256(raw).hexdigest() == sha256, name
    return raw


def default() -> Trash2State:
    return Trash2State.parse(native("trash2-default.state"))


def test_algorithm_domains_are_version_local() -> None:
    source, target = source_algorithms(), target_algorithms()
    assert len(source) == 48 and len(target) == 52
    assert source[11].name == target[18].name == "Clip Control"
    assert source[36].name == "Bit Aliasing"
    assert "Bit Aliasing" not in {item.name for item in target}
    bank = Bank.parse(native("trash1-default.state"))
    assert bank.program(0).settings.map.field("Trash: First Algorithm").choices is source
    changed = bank.with_values(3, {"Trash: First Algorithm": 47})
    assert changed.programs[:3] == bank.programs[:3]
    assert changed.programs[4:] == bank.programs[4:]
    with pytest.raises(StateTransformError, match="no mapped meaning"):
        bank.with_values(0, {"Trash: First Algorithm": 48})
    assert default().settings.map.field("TRASH: Single Stg1 Algorithm").choices is target
    with pytest.raises(StateTransformError, match="no mapped meaning"):
        default().with_values({"TRASH: Single Stg1 Algorithm": 52})


# -- saved fields ------------------------------------------------------------------


def test_saved_fields_predict_complete_independent_native_public_saves() -> None:
    manifest = json.loads((FIXTURES / "trash2-fields.json").read_bytes())
    baseline = default()
    assert len(manifest["fields"]) == 56
    for row in manifest["fields"]:
        saved = native(row["file"], row["sha256"])
        predicted = baseline.with_values({row["field"]: row["physical_value"]})
        assert predicted.encode() == saved, row["field"]
        assert Trash2State.parse(saved).encode() == saved
    with pytest.raises(StateTransformError, match="Unknown field"):
        baseline.with_values({"TRASH: Stg1 Tension Scale": 0.5})


def test_algorithm_selectors_replay_native_reload_receipts_in_each_saved_slot() -> None:
    manifest = json.loads((FIXTURES / "trash2-selectors.json").read_bytes())
    baseline = default()
    assert len(manifest["selectors"]) == 30
    for row in manifest["selectors"]:
        saved = native(row["file"], row["sha256"])
        predicted = baseline.with_values({row["field_id"]: row["value"]})
        assert predicted.encode() == saved
        assert Trash2State.parse(saved).encode() == saved


@pytest.mark.parametrize(("first", "second"), [(False, False), (True, False), (False, True), (True, True)])
def test_joint_polarity_edits_match_native_saves(first: bool, second: bool) -> None:
    baseline = default().with_values(
        {
            "TRASH: Stg1 DC Offset": False,
            "TRASH: Stg2 DC Offset": False,
            "Limiter Enable": False,
            "TRASH: Stg2 Enable": True,
        }
    )
    predicted = baseline.with_stage_polarity(1, inverted=first).with_stage_polarity(2, inverted=second)
    saved = native(f"trash2-polarity-{int(first)}{int(second)}.state")
    assert predicted.encode() == saved
    assert Trash2State.parse(saved).encode() == saved
    assert predicted.with_stage_polarity(2, inverted=False).with_stage_polarity(2, inverted=second) == predicted


# -- what the partial map refuses ---------------------------------------------------


def test_partial_curve_writes_and_unmeasured_curve_framing_are_refused() -> None:
    raw = native("trash2-default.state")
    state = Trash2State.parse(raw)
    with pytest.raises(StateTransformError, match="linear waveshaper"):
        state.with_values({"TRASH: Stg1 Shape Start Y": 1.0})
    with pytest.raises(StateTransformError):
        state.with_stage_polarity(True, inverted=True)
    with pytest.raises(StateTransformError):
        state.with_stage_polarity(3, inverted=False)
    # Another state shape at the same length: one template byte off is a stranger.
    for offset in (100, 734, 1378, 1398):
        wrong = bytearray(raw)
        wrong[offset] ^= 1
        with pytest.raises(StateTransformError, match="opaque template"):
            Trash2State.parse(bytes(wrong))
    assert state.encode() == raw


def test_a_native_preset_label_and_any_byte_map_but_the_measured_one_are_refused() -> None:
    """A loaded preset's label is template context the map does not claim."""
    raw = native("trash2-loaded-name.state")
    assert b"<Default>" in raw
    with pytest.raises(StateTransformError, match="opaque template"):
        Trash2State.parse(raw)
    baseline = default()
    wrong = FixedByteMap("different-layout", len(baseline.encode()), baseline.settings.map.fields)
    with pytest.raises(StateTransformError, match="measured core map"):
        Trash2State(wrong.decode(baseline.encode()))
