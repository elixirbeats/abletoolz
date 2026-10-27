"""Trash 1's sixteen-program bank, read and edited through its plugin map.

The synthesized banks check the framing guards; the captured ones -- the plugin's default bank and a
historical bank mixing revisions 21 and 22 -- are the evidence the named fields are right.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import assert_type

import pytest

from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.izotope_trash import LENGTH, MAGIC, REVISION, Bank, Program, byte_map
from abletoolz.plugins.state.model import FieldEncoding, StateField

FIXTURES = Path(__file__).parent / "state_fixtures" / "trash"


def decode(raw: bytes) -> Bank:
    return Bank.parse(raw)


def fields() -> tuple[StateField, ...]:
    return byte_map().fields


def bank() -> bytes:
    state = struct.pack("<4I", LENGTH, MAGIC, REVISION, LENGTH) + bytes(LENGTH - 12)
    return b"".join(state + struct.pack("<I", len(name)) + name for name in (b"A", b"Longer name") * 8)


# -- the framing ---------------------------------------------------------------


def test_named_physical_field_edit_preserves_other_programs_and_unknown_bytes() -> None:
    raw = bank()
    original = decode(raw)
    binding = next(item for item in fields() if item.name == "PreFilter: Node 1 Freq")
    changed = original.with_value(0, binding.id, 823.25)
    assert changed.value(0, binding.id) == 823.25
    assert changed.programs[1:] == original.programs[1:]
    assert changed.programs[0].name == original.programs[0].name
    before, after = original.programs[0].state, changed.programs[0].state
    assert before[: binding.byte_offset] == after[: binding.byte_offset]
    assert before[binding.byte_offset + 4 :] == after[binding.byte_offset + 4 :]
    assert original.encode() == raw
    assert decode(changed.encode()) == changed


@pytest.mark.parametrize(("offset", "value"), [(0, LENGTH - 1), (4, 0), (8, REVISION - 1), (12, LENGTH + 1)])
def test_unknown_format_is_refused(offset: int, value: int) -> None:
    raw = bytearray(bank())
    struct.pack_into("<I", raw, offset, value)
    with pytest.raises(ValueError, match="Unsupported"):
        decode(bytes(raw))


def test_truncation_trailing_data_and_name_length_are_refused() -> None:
    raw = bank()
    for truncated in (raw[:12], raw[:LENGTH], raw[:-1]):
        with pytest.raises(ValueError):
            decode(truncated)
    with pytest.raises(ValueError, match="Trailing"):
        decode(raw + b"extra")
    modified = bytearray(raw)
    struct.pack_into("<I", modified, LENGTH + 4, 0xFFFFFFFF)
    with pytest.raises(ValueError, match="name"):
        decode(bytes(modified))


def test_field_types_and_indices_refuse_ambiguous_or_invalid_writes() -> None:
    original = decode(bank())
    boolean, integer, number = (
        next(item for item in fields() if item.encoding == encoding)
        for encoding in (FieldEncoding.BOOL8, FieldEncoding.UINT32_LE, FieldEncoding.FLOAT32_LE)
    )
    for ident, value in (
        (boolean.id, 1),
        (integer.id, True),
        (integer.id, -1),
        (number.id, float("nan")),
        (number.id, 1e100),
        (number.id, 1 << 4000),
    ):
        with pytest.raises(ValueError):
            original.with_value(0, ident, value)
    for program, ident in ((-1, 0), (16, 0), (0, -1), (0, 296)):
        with pytest.raises(ValueError):
            original.value(program, ident)


# -- the captured banks ----------------------------------------------------------


def test_the_default_bank_round_trips_and_every_field_reads() -> None:
    raw = (FIXTURES / "trash1-default.state").read_bytes()
    model = decode(raw)
    assert_type(model, Bank)
    assert model.encode() == raw
    for item in fields():
        model.programs[0].settings.value(item.id)
    with pytest.raises(StateTransformError, match="sixteen"):
        Bank(model.programs[:-1])
    with pytest.raises(StateTransformError, match="Truncated"):
        Program(b"short", b"Name")


def test_a_mixed_revision_bank_keeps_its_opaque_programs_through_a_typed_edit() -> None:
    """Revision 22 has no mapped fields: it is carried whole, never read with revision 21's coordinates."""
    raw = (FIXTURES / "trash1-mixed-revisions.state").read_bytes()
    model = decode(raw)
    assert model.encode() == raw
    old = next(i for i, program in enumerate(model.programs) if program.revision == 21)
    opaque = next(i for i, program in enumerate(model.programs) if program.revision == 22)
    changed = model.with_values(old, {"PreFilter: Node 1 Freq": 823.25})
    assert changed.program(opaque) == model.program(opaque)
    assert decode(changed.encode()) == changed
    with pytest.raises(StateTransformError, match="unmapped"):
        _ = changed.program(opaque).settings
    with pytest.raises(StateTransformError, match="unmapped"):
        model.with_values(opaque, {"PreFilter: Node 1 Freq": 823.25})
    for index in (-1, 16):
        with pytest.raises(ValueError, match="index"):
            model.program(index)
