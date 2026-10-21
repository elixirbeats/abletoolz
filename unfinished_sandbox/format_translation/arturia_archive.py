"""Bounded parsing of Arturia's observed counted string/float map shape.

Discovery finds structural candidates. The caller must establish the archive
family and field context before treating a candidate as writable plugin state.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass

MAX_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 16384
MAX_NAME_BYTES = 4096
MAX_TOKEN_BYTES = 96
MAX_HEADERS = 64
MAX_SCAN_BYTES = 64 * 1024 * 1024
HEADER = re.compile(rb"(?<!\S)([0-9]{1,7}) 0 0 0 (?=[0-9]+ )")
NUMBER = re.compile(rb"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
SPACE = b" \t\r\n"


class ArchiveError(ValueError):
    """The requested map is malformed or its context is ambiguous."""


class BudgetExceeded(ArchiveError):
    """Candidate discovery exceeded its explicit work bounds."""


@dataclass(slots=True)
class _Budget:
    remaining: int = MAX_SCAN_BYTES

    def charge(self, size: int) -> None:
        self.remaining -= size
        if self.remaining < 0:
            raise BudgetExceeded("Map discovery exceeds its total scan budget")


@dataclass(frozen=True, slots=True)
class NumericField:
    name: str
    key_start: int
    key_end: int
    value_start: int
    value_end: int
    value_text: str

    @property
    def value(self) -> float:
        return float(self.value_text)


@dataclass(frozen=True, slots=True)
class FloatMap:
    data: bytes
    header_start: int
    entries_start: int
    end: int
    fields: tuple[NumericField, ...]

    def encode(self, edits: Mapping[str, float | int | str]) -> bytes:
        unknown = set(edits) - {field.name for field in self.fields}
        if unknown:
            raise ArchiveError(f"Unknown map keys: {sorted(unknown)}")
        result = bytearray()
        cursor = 0
        for field in self.fields:
            if field.name not in edits:
                continue
            replacement = str(edits[field.name]).encode("ascii")
            _number(replacement)
            result.extend(self.data[cursor : field.value_start])
            result.extend(replacement)
            cursor = field.value_end
        result.extend(self.data[cursor:])
        encoded = bytes(result)
        reparsed = parse_float_map_at(encoded, self.header_start)
        if tuple(field.name for field in reparsed.fields) != tuple(field.name for field in self.fields):
            raise ArchiveError("Numeric edit changed map structure")
        if (
            encoded[: self.header_start] != self.data[: self.header_start]
            or encoded[reparsed.end :] != self.data[self.end :]
        ):
            raise ArchiveError("Numeric edit changed bytes outside the map")
        return encoded


def _number(token: bytes) -> str:
    if len(token) > MAX_TOKEN_BYTES or NUMBER.fullmatch(token) is None:
        raise ArchiveError("Invalid finite numeric token")
    text = token.decode("ascii")
    if not math.isfinite(float(text)):
        raise ArchiveError("Numeric value is not finite")
    return text


def _token(data: bytes, position: int, budget: _Budget) -> tuple[bytes, int, int]:
    while position < len(data) and data[position] in SPACE:
        position += 1
        budget.charge(1)
    start = position
    while position < len(data) and data[position] not in SPACE:
        position += 1
        if position - start > MAX_TOKEN_BYTES:
            raise ArchiveError("Token exceeds its byte limit")
    budget.charge(position - start)
    return data[start:position], start, position


def _parse(data: bytes, offset: int, budget: _Budget) -> FloatMap:
    if len(data) > MAX_BYTES:
        raise BudgetExceeded("Archive exceeds the 16 MiB parsing limit")
    match = HEADER.match(data, offset)
    if match is None:
        raise ArchiveError("Expected observed N 0 0 0 map header")
    count = int(match[1])
    if not 1 <= count <= MAX_ENTRIES:
        raise ArchiveError("Map entry count is outside its limit")
    position = match.end()
    fields: list[NumericField] = []
    previous: bytes | None = None
    for _ in range(count):
        length_token, _, length_end = _token(data, position, budget)
        if not length_token.isdigit() or length_end >= len(data) or data[length_end] != 32:
            raise ArchiveError("Expected a string byte length and single space delimiter")
        length = int(length_token)
        if not 1 <= length <= MAX_NAME_BYTES:
            raise ArchiveError("Map key length is outside its limit")
        start = length_end + 1
        end = start + length
        if end >= len(data) or data[end] not in SPACE:
            raise ArchiveError("Map key exceeds archive bounds or lacks a following delimiter")
        raw_name = data[start:end]
        budget.charge(length)
        try:
            name = raw_name.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ArchiveError("Map key is not valid UTF-8") from error
        if any(ord(character) < 32 for character in name):
            raise ArchiveError("Map key contains control characters")
        if previous is not None and raw_name <= previous:
            raise ArchiveError("Map keys are duplicated or not sorted by encoded bytes")
        previous = raw_name
        token, value_start, position = _token(data, end, budget)
        fields.append(NumericField(name, start, end, value_start, position, _number(token)))
    return FloatMap(data, offset, match.end(), position, tuple(fields))


def parse_float_map_at(data: bytes, offset: int) -> FloatMap:
    if offset < 0 or offset >= len(data):
        raise ArchiveError("Map offset is outside the archive")
    return _parse(data, offset, _Budget())


def find_float_maps(data: bytes) -> tuple[FloatMap, ...]:
    """Find complete sorted candidates, excluding headers inside validated keys."""
    if len(data) > MAX_BYTES:
        raise BudgetExceeded("Archive exceeds the 16 MiB parsing limit")
    budget = _Budget()
    candidates: list[FloatMap] = []
    for number, match in enumerate(HEADER.finditer(data), start=1):
        if number > MAX_HEADERS:
            raise BudgetExceeded("Too many candidate map headers")
        try:
            candidates.append(_parse(data, match.start(), budget))
        except BudgetExceeded:
            raise
        except ArchiveError:
            continue
    return tuple(
        candidate
        for candidate in candidates
        if not any(
            other is not candidate
            and any(field.key_start <= candidate.header_start < field.key_end for field in other.fields)
            for other in candidates
        )
    )


def single_float_map(data: bytes) -> FloatMap:
    candidates = find_float_maps(data)
    if len(candidates) != 1:
        raise ArchiveError(f"Expected one map candidate; found {len(candidates)}")
    return candidates[0]
