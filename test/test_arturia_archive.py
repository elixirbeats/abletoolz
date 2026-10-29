"""The map of named numbers in an Arturia archive: read, rewritten in place, and refused when it is not one map."""

from __future__ import annotations

import pathlib
import struct

import pytest

from abletoolz.formats.arturia_archive import NumberMap, TwoArchives
from abletoolz.formats.errors import FormatError

# Prophet-5 V's init patch with its macros removed (see ``state/prophet_5_v.py``).
BASELINE_STATE = pathlib.Path(__file__).parents[1] / "abletoolz/plugins/state/data/prophet-5-v-baseline.state"


def string(value: str) -> bytes:
    encoded = value.encode()
    return str(len(encoded)).encode() + b" " + encoded


def archive(
    entries: dict[str, str], before: bytes = b"22 serialization::archive 10 0 7 0 7 ", after: bytes = b" 0 1"
) -> bytes:
    body = b" ".join(string(name) + b" " + value.encode() for name, value in sorted(entries.items()))
    return before + str(len(entries)).encode() + b" 0 0 0 " + body + after


def two(first: bytes, second: bytes) -> bytes:
    return struct.pack("<QQ", len(first), len(second)) + first + second


def test_a_map_is_read_by_name() -> None:
    assert NumberMap.parse(archive({"Cutoff": "0.25", "Attack": "1e-3"})).values == {"Attack": 0.001, "Cutoff": 0.25}


def test_a_rewrite_changes_only_the_number() -> None:
    data = archive({"Cutoff": "0.25", "Attack": "0.5"})
    written = NumberMap.parse(data).with_values({"Cutoff": 0.713})
    assert written == data.replace(b"0.25", b"0.713")


def test_two_archives_name_their_numbers_by_section_and_keep_their_lengths_true() -> None:
    state = two(archive({"A": "0.1"}), archive({"A": "0.2", "B": "0.3"}))
    parsed = TwoArchives.parse(state)
    assert parsed.values == {"section[0]/A": 0.1, "section[1]/A": 0.2, "section[1]/B": 0.3}
    written = parsed.with_values({"section[0]/A": 0.123456, "section[1]/B": 0.5})
    assert TwoArchives.parse(written).values == {"section[0]/A": 0.123456, "section[1]/A": 0.2, "section[1]/B": 0.5}


def test_the_prophet_5_v_baseline_holds_both_archives_maps() -> None:
    values = TwoArchives.parse(BASELINE_STATE.read_bytes()).values
    assert len(values) == 2110
    assert values["section[0]/Prophet5VCO_PW1"] == values["section[1]/Prophet5VCO_PW1"]


@pytest.mark.parametrize(
    ("data", "message"),
    [
        # A map with its names out of order, or a value that is not a number, is not a map at all.
        (archive({"B": "1", "A": "2"}).replace(b"1 A 2 1 B 1", b"1 B 1 1 A 2"), "found 0"),
        (archive({"A": "nan"}), "found 0"),
        (b"no map here", "found 0"),
        (archive({"A": "1"}) + b" " + archive({"B": "2"}, before=b""), "found 2"),
    ],
    ids=["unsorted", "not a number", "no map", "two maps"],
)
def test_an_archive_that_is_not_one_map_is_refused(data: bytes, message: str) -> None:
    with pytest.raises(FormatError, match=message):
        NumberMap.parse(data)


def test_a_name_the_map_does_not_hold_is_refused() -> None:
    with pytest.raises(FormatError, match="no"):
        NumberMap.parse(archive({"A": "1"})).with_values({"B": 2.0})


def test_lengths_that_do_not_account_for_the_state_are_refused() -> None:
    state = two(archive({"A": "1"}), archive({"A": "2"}))
    with pytest.raises(FormatError, match="do not account"):
        TwoArchives.parse(state + b" ")
