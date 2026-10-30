"""Just enough CBOR (RFC 8949) to find a text value by its key and replace it in place.

Serum 2 saves its patch as CBOR, and a general decoder cannot write one back:
Serum writes a float as float32 wherever it fits, a decoder hands back a Python
float, and an encoder writes that as float64, so a decode and re-encode rewrites
every number in the patch. No real Serum 2 patch survives cbor2's round trip,
with or without its canonical form. So nothing here decodes a whole document. :func:`text_values` walks the
items and notes where each matching text value sits, and :func:`replace_text`
swaps that one item's bytes, leaving every other byte as the plugin wrote it.
"""

from __future__ import annotations

import dataclasses

from abletoolz.formats.errors import FormatError

_TEXT = 3
_ARRAY = 4
_MAP = 5
_TAG = 6
_SIMPLE = 7
_INDEFINITE = 31
_BREAK = 0xFF
# Deeper than any Serum 2 patch nests; past it a document is refused, not recursed into.
_MAX_DEPTH = 64
# The bytes an additional-information value of 24 to 27 says the argument takes.
_ARGUMENT_BYTES = {24: 1, 25: 2, 26: 4, 27: 8}


@dataclasses.dataclass(frozen=True, slots=True)
class TextItem:
    """One definite-length text string: its value, and where the whole item sits, end exclusive."""

    value: str
    start: int
    end: int


def _head(data: bytes, at: int) -> tuple[int, int | None, int]:
    """The major type, the argument (None when indefinite) and where the item's content starts."""
    if at >= len(data):
        raise FormatError(f"CBOR ends at byte {at}, inside an item")
    major, info = data[at] >> 5, data[at] & 0x1F
    at += 1
    if info < 24:
        return major, info, at
    if info == _INDEFINITE and major in (2, _TEXT, _ARRAY, _MAP):
        return major, None, at
    size = _ARGUMENT_BYTES.get(info)
    if size is None:
        raise FormatError(f"CBOR item at byte {at - 1} has additional information {info}")
    if at + size > len(data):
        raise FormatError(f"CBOR ends at byte {len(data)}, inside an item's head")
    return major, int.from_bytes(data[at : at + size], "big"), at + size


def _text(data: bytes, at: int) -> TextItem | None:
    """The definite-length text string at ``at``, or None for any other item."""
    major, length, content = _head(data, at)
    if major != _TEXT or length is None:
        return None
    end = content + length
    if end > len(data):
        raise FormatError(f"a CBOR text string at byte {at} runs past the end")
    try:
        return TextItem(data[content:end].decode("utf-8"), at, end)
    except UnicodeDecodeError as error:
        raise FormatError(f"a CBOR text string at byte {at} is not UTF-8") from error


def _scalar_size(initial: int) -> int:
    """How many bytes an integer, float or simple value takes, read off its first byte; 0 for any other item."""
    major, info = initial >> 5, initial & 0x1F
    if major not in (0, 1, _SIMPLE):
        return 0
    if info < 24:
        return 1
    size = _ARGUMENT_BYTES.get(info)
    return 0 if size is None else 1 + size


# A patch is mostly numbers -- Serum 2's run to hundreds of thousands -- so the
# walk steps over one with a table lookup rather than a call.
_SCALAR_SIZE = bytes(_scalar_size(initial) for initial in range(256))


def _walk(data: bytes, at: int, key: bytes, found: list[TextItem], depth: int = 0) -> int:
    """Where the item at ``at`` ends, noting every text value ``key`` maps to inside it."""
    if depth > _MAX_DEPTH:
        raise FormatError(f"CBOR nests deeper than {_MAX_DEPTH} items at byte {at}")
    major, argument, at = _head(data, at)
    if major in (0, 1) or major == _SIMPLE:
        # An integer's or a float's argument is its whole content.
        return at
    if major in (2, _TEXT):
        if argument is not None:
            if at + argument > len(data):
                raise FormatError(f"a CBOR string at byte {at} runs past the end")
            return at + argument
        # An indefinite string is definite strings of its own kind, up to the break.
        while data[at : at + 1] != bytes([_BREAK]):
            chunk, length, content = _head(data, at)
            if chunk != major or length is None:
                raise FormatError(f"a CBOR string at byte {at} holds a chunk that is not a definite string of its kind")
            if content + length > len(data):
                raise FormatError(f"a CBOR string at byte {at} runs past the end")
            at = content + length
        return at + 1
    if major == _TAG:
        return _walk(data, at, key, found, depth + 1)
    per_entry = 2 if major == _MAP else 1
    count = 0
    end = len(data)
    while argument is None or count < argument * per_entry:
        if at >= end:
            raise FormatError(f"CBOR ends at byte {at}, inside an item")
        if argument is None and data[at] == _BREAK:
            return at + 1
        if major == _MAP and count % 2 == 0:
            named = _is_text(data, at, key)
            at = _walk(data, at, key, found, depth + 1)
            if named:
                value = _text(data, at)
                if value is not None:
                    found.append(value)
            at = _walk(data, at, key, found, depth + 1)
            count += 2
            continue
        size = _SCALAR_SIZE[data[at]]
        at = at + size if size else _walk(data, at, key, found, depth + 1)
        count += 1
    return at


def _is_text(data: bytes, at: int, value: bytes) -> bool:
    """Whether the item at ``at`` is a definite-length text string holding ``value``."""
    major, length, content = _head(data, at)
    return major == _TEXT and length == len(value) and data[content : content + length] == value


def text_values(data: bytes, key: str) -> tuple[TextItem, ...]:
    """Every text value a map anywhere in the document gives for ``key``, in document order.

    A document whose bytes never spell ``key`` holds none, and is answered
    without walking it. Any other is walked whole, so bytes that are not one
    well-formed item are refused.
    """
    encoded = key.encode("utf-8")
    if encoded not in data:
        return ()
    found: list[TextItem] = []
    end = _walk(data, 0, encoded, found)
    if end != len(data):
        raise FormatError(f"CBOR document ends at byte {end} of {len(data)}")
    return tuple(found)


def _text_head(length: int) -> bytes:
    """The shortest head for a text string of ``length`` bytes, which is the one RFC 8949 prefers."""
    major = _TEXT << 5
    if length < 24:
        return bytes([major | length])
    for info, size in _ARGUMENT_BYTES.items():
        if length < 1 << (8 * size):
            return bytes([major | info]) + length.to_bytes(size, "big")
    raise FormatError(f"a {length} byte text string is too long for CBOR")


def replace_text(data: bytes, item: TextItem, value: str) -> bytes:
    """``data`` with ``item`` holding ``value`` instead, every other byte unchanged."""
    if _text(data, item.start) != item:
        raise FormatError(f"byte {item.start} does not hold the text string {item.value!r}")
    encoded = value.encode("utf-8")
    return data[: item.start] + _text_head(len(encoded)) + encoded + data[item.end :]
