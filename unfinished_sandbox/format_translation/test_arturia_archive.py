from __future__ import annotations

import pytest
from arturia_archive import ArchiveError, BudgetExceeded, find_float_maps, parse_float_map_at, single_float_map


def map_bytes(entries: list[tuple[str, str]]) -> bytes:
    return f"{len(entries)} 0 0 0 ".encode() + b" ".join(
        str(len(name.encode())).encode() + b" " + name.encode() + b" " + value.encode() for name, value in entries
    )


def test_spaces_utf8_offsets_and_length_changing_write() -> None:
    prefix, suffix = b"opaque prefix ", b" suffix\0\xff"
    raw = prefix + map_bytes([("MIDI Channel", "0"), ("éclair", "0.5")]) + suffix
    parsed = single_float_map(raw)
    assert parsed.header_start == len(prefix)
    assert [field.name for field in parsed.fields] == ["MIDI Channel", "éclair"]
    for field in parsed.fields:
        assert raw[field.key_start : field.key_end] == field.name.encode()
        assert raw[field.value_start : field.value_end] == field.value_text.encode()
    encoded = parsed.encode({"MIDI Channel": "0.17300001"})
    reread = parse_float_map_at(encoded, len(prefix))
    assert encoded[: reread.header_start] == prefix
    assert encoded[reread.end :] == suffix
    assert reread.fields[0].value_text == "0.17300001"
    assert reread.fields[1].value_text == "0.5"
    assert parsed.encode({}) == raw


@pytest.mark.parametrize(
    "raw",
    [
        b"2 0 0 0 1 A 0",
        b"1 0 0 0 9 A 0",
        b"1 0 0 0 1 A nan",
        b"1 0 0 0 1 A 1e999",
        map_bytes([("B", "0"), ("A", "1")]),
        map_bytes([("A", "0"), ("A", "1")]),
    ],
)
def test_malformed_maps_are_refused(raw: bytes) -> None:
    with pytest.raises(ArchiveError):
        parse_float_map_at(raw, 0)


def test_map_looking_text_inside_validated_key_is_excluded() -> None:
    nested = map_bytes([("A", "0"), ("B", "1")]).decode()
    raw = map_bytes([(nested, "0"), ("Z", "1")])
    assert len(find_float_maps(raw)) == 1
    assert single_float_map(raw).fields[0].name == nested


def test_two_independent_maps_are_ambiguous() -> None:
    raw = map_bytes([("A", "0")]) + b" separator " + map_bytes([("B", "1")])
    assert len(find_float_maps(raw)) == 2
    with pytest.raises(ArchiveError, match="found 2"):
        single_float_map(raw)


def test_edits_reject_unknown_keys_and_nonfinite_values() -> None:
    parsed = single_float_map(map_bytes([("A", "0")]))
    for edits in ({"missing": 1.0}, {"A": float("inf")}, {"A": "nan"}):
        with pytest.raises(ArchiveError):
            parsed.encode(edits)


def test_candidate_budget_is_bounded() -> None:
    raw = b" ".join([b"999 0 0 0 1 A broken"] * 65)
    with pytest.raises(BudgetExceeded, match="candidate"):
        find_float_maps(raw)
