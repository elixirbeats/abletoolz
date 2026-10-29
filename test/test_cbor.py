"""Finding a text value in CBOR by its key, and replacing it without touching anything else."""

from __future__ import annotations

import pytest

from abletoolz.formats.cbor import TextItem, replace_text, text_values
from abletoolz.formats.errors import FormatError


def text(value: str) -> bytes:
    encoded = value.encode()
    assert len(encoded) < 24
    return bytes([0x60 | len(encoded)]) + encoded


KEY = text("samplePath")
FLOAT32_HALF = b"\xfa\x3f\x00\x00\x00"
# {"osc": [{"samplePath": "a.wav", "level": 0.5f32}, 7], "samplePath": 1}
DOCUMENT = (
    b"\xa2" + text("osc") + b"\x82\xa2" + KEY + text("a.wav") + text("level") + FLOAT32_HALF + b"\x07" + KEY + b"\x01"
)


def test_a_text_value_is_found_by_its_key_at_any_depth_and_others_are_skipped() -> None:
    (item,) = text_values(DOCUMENT, "samplePath")
    assert item.value == "a.wav"
    assert DOCUMENT[item.start : item.end] == text("a.wav")


def test_indefinite_lengths_and_tags_are_walked() -> None:
    # A tagged value is not a plain text string, so it is walked past rather than reported.
    document = b"\xbf" + KEY + b"\xc0" + text("b.wav") + text("list") + b"\x9f\x01\x02\xff" + b"\xff"
    assert [item.value for item in text_values(document, "samplePath")] == []
    tagged_free = b"\xbf" + KEY + text("b.wav") + text("list") + b"\x9f\x01\x02\xff" + b"\xff"
    assert [item.value for item in text_values(tagged_free, "samplePath")] == ["b.wav"]


def test_a_replacement_leaves_every_other_byte_as_written() -> None:
    """The float32 stays a float32, which a decode and re-encode would turn into a float64."""
    (item,) = text_values(DOCUMENT, "samplePath")
    replaced = replace_text(DOCUMENT, item, "b.wav")
    assert replaced == DOCUMENT.replace(text("a.wav"), text("b.wav"))
    assert FLOAT32_HALF in replaced


def test_a_long_replacement_gets_the_longer_head() -> None:
    (item,) = text_values(DOCUMENT, "samplePath")
    long_path = "D:/Library/" + "x" * 40 + ".wav"
    replaced = replace_text(DOCUMENT, item, long_path)
    assert bytes([0x78, len(long_path)]) + long_path.encode() in replaced
    assert [found.value for found in text_values(replaced, "samplePath")] == [long_path]


def test_an_item_that_is_not_where_it_says_is_refused() -> None:
    with pytest.raises(FormatError, match="does not hold"):
        replace_text(DOCUMENT, TextItem("a.wav", 0, 6), "b.wav")


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (DOCUMENT[:-1], "ends at byte"),
        (DOCUMENT + b"\x00", "ends at byte"),
        (b"\xa1" + KEY + b"\x1c", "additional information 28"),
        (b"\xa1" + KEY + b"\x65abc", "runs past the end"),
        (b"\x82" + KEY + b"\x1a\x00", "ends at byte"),
        (b"\x81" * 5000 + KEY, "nests deeper"),
        (b"\xc0" * 5000 + KEY, "nests deeper"),
        (b"\x7f" * 5000 + KEY, "nests deeper"),
    ],
    ids=[
        "truncated",
        "trailing bytes",
        "reserved",
        "short string",
        "number cut short",
        "deep arrays",
        "deep tags",
        "deep strings",
    ],  # fmt: skip
)
def test_bytes_that_are_not_one_item_are_refused(data: bytes, message: str) -> None:
    with pytest.raises(FormatError, match=message):
        text_values(data, "samplePath")


def test_a_document_that_never_spells_the_key_holds_none_and_is_not_walked() -> None:
    assert text_values(b"\x1c not CBOR at all", "samplePath") == ()
