from __future__ import annotations

import struct

import pytest

from abletoolz.plugins.state.izotope_trash import LENGTH, MAGIC, REVISION, decode, fields


def bank() -> bytes:
    state = struct.pack("<4I", LENGTH, MAGIC, REVISION, LENGTH) + bytes(LENGTH - 12)
    return b"".join(state + struct.pack("<I", len(name)) + name for name in (b"A", b"Longer name") * 8)


def test_named_physical_field_edit_preserves_other_programs_and_unknown_bytes() -> None:
    raw = bank()
    original = decode(raw)
    binding = next(field for field in fields() if field.name == "PreFilter: Node 1 Freq")
    changed = original.with_value(0, binding.id, 823.25)
    assert changed.value(0, binding.id) == 823.25
    assert changed.programs[1:] == original.programs[1:]
    assert changed.programs[0].name == original.programs[0].name
    before, after = original.programs[0].state, changed.programs[0].state
    assert before[: binding.byte_offset] == after[: binding.byte_offset]
    assert before[binding.byte_offset + 4 :] == after[binding.byte_offset + 4 :]
    assert original.encode() == raw
    assert decode(changed.encode()) == changed


@pytest.mark.parametrize("offset,value", [(0, LENGTH - 1), (4, 0), (8, REVISION - 1), (12, LENGTH + 1)])
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
    boolean = next(field for field in fields() if field.encoding == "bool8")
    integer = next(field for field in fields() if field.encoding == "uint32_le")
    number = next(field for field in fields() if field.encoding == "float32_le")
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


def test_mixed_revisions_preserve_opaque_program_without_applying_old_coordinates() -> None:
    original = decode(bank())
    old = original.programs[0]
    new_state = struct.pack("<4I", 1121, MAGIC, 22, 1121) + bytes(1109)
    raw = old.state + struct.pack("<I", len(old.name)) + old.name
    raw += (new_state + struct.pack("<I", 1) + b"B") * 15
    mixed = decode(raw)
    assert mixed.encode() == raw
    assert mixed.programs[1].revision == 22
    with pytest.raises(ValueError, match="unmapped"):
        mixed.value(1, 31)
    with pytest.raises(ValueError, match="unmapped"):
        mixed.with_value(1, 31, 0.0)
    assert mixed.with_value(0, 31, -5.0).programs[1:] == mixed.programs[1:]
