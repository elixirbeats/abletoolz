"""FastLZ: what abletoolz writes reads back, and a level 2 stream as NI writes one decodes."""

from __future__ import annotations

import random

import pytest

from abletoolz.formats import FormatError, fastlz


@pytest.mark.parametrize(
    "data",
    [
        b"a",
        b"abc",
        b"a" * 1000,
        bytes(range(256)) * 40,
        b"xyz" * 3000,
        random.Random(7).randbytes(5000),
        b"head" + random.Random(8).randbytes(9000) + b"head" * 50,
    ],
    ids=["one", "three", "run", "ramp", "repeat", "noise", "far"],
)
def test_what_compress_writes_decompresses_to_the_same_bytes(data: bytes) -> None:
    packed = fastlz.compress(data)
    assert packed[0] >> 5 == 0  # level 1
    assert fastlz.decompress(packed, len(data)) == data


def test_a_level_2_stream_decodes() -> None:
    # Level 2 in the first byte, a three byte literal run, then a three byte match three back.
    assert fastlz.decompress(b"\x22abc\x20\x02", 6) == b"abcabc"


def test_a_stream_that_decodes_to_another_length_is_refused() -> None:
    packed = fastlz.compress(b"abcdef")
    with pytest.raises(FormatError, match="stated"):
        fastlz.decompress(packed, 5)
    with pytest.raises(FormatError, match="not the stated"):
        fastlz.decompress(packed, 7)


def test_nothing_is_not_a_stream() -> None:
    with pytest.raises(FormatError):
        fastlz.compress(b"")
    with pytest.raises(FormatError):
        fastlz.decompress(b"", 0)


def test_a_reference_before_the_start_is_refused() -> None:
    with pytest.raises(FormatError, match="before the start"):
        fastlz.decompress(b"\x00a\x20\x05", 4)
