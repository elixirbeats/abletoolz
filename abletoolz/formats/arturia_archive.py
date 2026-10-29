"""The map of named numbers inside an Arturia plugin's saved state, edited in place.

A V Collection plugin saves a Boost text archive: space-separated tokens, each
string written as its byte length, a space and the bytes. Somewhere in it is
one sorted map of control names to numbers, which is where the controls live:

    N 0 0 0    then N times:    length name    number

Measured on Prophet-5 V 1.3.3.6106 (a VST2 state holds two such
archives behind two little-endian u64 lengths, :class:`TwoArchives`): the
plugin restored every one of its 96 core controls from a state whose numbers
were rewritten here, and read them back to within 2e-6. Everything around the
map stays as the plugin wrote it, and a write that would move it is refused.

The map is found by its header, so only an archive holding exactly one
complete map is read; anything else is refused rather than guessed at.
"""

from __future__ import annotations

import dataclasses
import math
import re
import struct
from collections.abc import Mapping

from abletoolz.formats.errors import FormatError

MAX_BYTES = 16 * 1024 * 1024
_MAX_ENTRIES = 16384
_MAX_NAME_BYTES = 4096
_MAX_TOKEN_BYTES = 96
_MAX_HEADERS = 64
_HEADER = re.compile(rb"(?<!\S)([0-9]{1,7}) 0 0 0 (?=[0-9]+ )")
_NUMBER = re.compile(rb"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_SPACE = b" \t\r\n"
_LENGTHS = struct.Struct("<QQ")


@dataclasses.dataclass(frozen=True, slots=True)
class _Field:
    name: str
    value_start: int
    value_end: int
    text: str


@dataclasses.dataclass(frozen=True, slots=True)
class NumberMap:
    """One archive and the map of named numbers in it."""

    data: bytes
    start: int
    end: int
    fields: tuple[_Field, ...]

    @property
    def values(self) -> dict[str, float]:
        return {field.name: float(field.text) for field in self.fields}

    def with_values(self, edits: Mapping[str, float]) -> bytes:
        """The archive with each named number replaced, every other byte as it was."""
        unknown = set(edits) - {field.name for field in self.fields}
        if unknown:
            raise FormatError(f"this archive's map has no {sorted(unknown)}")
        out = bytearray()
        cursor = 0
        for field in self.fields:
            if field.name in edits:
                value = edits[field.name]
                if not math.isfinite(value):
                    raise FormatError(f"{field.name} cannot be {value}")
                out += self.data[cursor : field.value_start] + repr(float(value)).encode("ascii")
                cursor = field.value_end
        out += self.data[cursor:]
        encoded = bytes(out)
        reread = _parse(encoded, self.start)
        if [field.name for field in reread.fields] != [field.name for field in self.fields]:
            raise FormatError("an edit changed the map's structure")
        if encoded[: self.start] != self.data[: self.start] or encoded[reread.end :] != self.data[self.end :]:
            raise FormatError("an edit changed bytes outside the map")
        return encoded

    @classmethod
    def parse(cls, data: bytes) -> NumberMap:
        """The one complete map in ``data``; refused when there is none or more than one."""
        if len(data) > MAX_BYTES:
            raise FormatError(f"a {len(data)} byte archive is over the {MAX_BYTES} byte limit")
        candidates = []
        for number, match in enumerate(_HEADER.finditer(data), start=1):
            if number > _MAX_HEADERS:
                raise FormatError("too many map headers in this archive")
            try:
                candidates.append(_parse(data, match.start()))
            except FormatError:
                continue
        # A header can appear inside a real map's names; one that starts inside another map is part of it.
        found = [
            candidate
            for candidate in candidates
            if not any(other.start < candidate.start < other.end for other in candidates)
        ]
        if len(found) != 1:
            raise FormatError(f"expected one map of named numbers in this archive, found {len(found)}")
        return found[0]


def _token(data: bytes, at: int) -> tuple[bytes, int, int]:
    while at < len(data) and data[at] in _SPACE:
        at += 1
    start = at
    while at < len(data) and data[at] not in _SPACE:
        at += 1
        if at - start > _MAX_TOKEN_BYTES:
            raise FormatError(f"a token at byte {start} is longer than {_MAX_TOKEN_BYTES} bytes")
    return data[start:at], start, at


def _parse(data: bytes, offset: int) -> NumberMap:
    match = _HEADER.match(data, offset)
    if match is None:
        raise FormatError(f"no map header at byte {offset}")
    count = int(match[1])
    if not 1 <= count <= _MAX_ENTRIES:
        raise FormatError(f"a map of {count} entries is outside the measured range")
    at = match.end()
    fields: list[_Field] = []
    previous: bytes | None = None
    for _ in range(count):
        length, _, length_end = _token(data, at)
        if not length.isdigit() or data[length_end : length_end + 1] != b" ":
            raise FormatError(f"expected a name length at byte {length_end}")
        size = int(length)
        start, end = length_end + 1, length_end + 1 + size
        if not 1 <= size <= _MAX_NAME_BYTES or end >= len(data) or data[end] not in _SPACE:
            raise FormatError(f"a map name at byte {start} runs past its length")
        raw_name = data[start:end]
        if previous is not None and raw_name <= previous:
            raise FormatError("map names are not sorted and unique")
        previous = raw_name
        try:
            name = raw_name.decode("utf-8")
        except UnicodeDecodeError:
            raise FormatError(f"a map name at byte {start} is not UTF-8") from None
        if any(ord(character) < 32 for character in name):
            raise FormatError(f"a map name at byte {start} holds a control character")
        value, value_start, at = _token(data, end)
        if _NUMBER.fullmatch(value) is None or not math.isfinite(float(value)):
            raise FormatError(f"{name} holds {value!r}, not a finite number")
        fields.append(_Field(name, value_start, at, value.decode("ascii")))
    return NumberMap(data, offset, at, tuple(fields))


@dataclasses.dataclass(frozen=True, slots=True)
class TwoArchives:
    """A VST2 state of two archives behind their two lengths, each with its map; numbers are named per archive."""

    archives: tuple[NumberMap, NumberMap]

    @staticmethod
    def split(state: bytes) -> tuple[bytes, bytes]:
        """The two archives, as their lengths cut them."""
        if len(state) < _LENGTHS.size:
            raise FormatError("this state ends before its two archive lengths")
        first, second = _LENGTHS.unpack_from(state)
        if first + second != len(state) - _LENGTHS.size:
            raise FormatError("this state's two archive lengths do not account for it")
        body = state[_LENGTHS.size :]
        return body[:first], body[first:]

    @classmethod
    def parse(cls, state: bytes) -> TwoArchives:
        first, second = cls.split(state)
        return cls((NumberMap.parse(first), NumberMap.parse(second)))

    @property
    def values(self) -> dict[str, float]:
        """Every number, named ``section[N]/name`` after the archive it is in."""
        return {
            f"section[{n}]/{name}": value
            for n, archive in enumerate(self.archives)
            for name, value in archive.values.items()
        }

    def with_values(self, edits: Mapping[str, float]) -> bytes:
        """The state with the named numbers replaced; the lengths follow the archives."""
        per_archive: list[dict[str, float]] = [{}, {}]
        for key, value in edits.items():
            section, _, name = key.partition("/")
            if section not in ("section[0]", "section[1]") or not name:
                raise FormatError(f"{key} does not name an archive and a number in it")
            per_archive[int(section[8])][name] = value
        parts = [archive.with_values(changes) for archive, changes in zip(self.archives, per_archive, strict=True)]
        return _LENGTHS.pack(len(parts[0]), len(parts[1])) + parts[0] + parts[1]
