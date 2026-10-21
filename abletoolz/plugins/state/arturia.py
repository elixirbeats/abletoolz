"""Read measured Arturia preset metadata and prepare legacy Analog Lab state.

The outer preset identity can be corrected without changing child versions or
opaque data. The intended native plugin must still convert and validate it.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass

HEADER = b"22 serialization::archive 10 0 7 0 7 "
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+")
MAX_BODY = 16 * 1024 * 1024
MAX_STRING = 65536
MAX_VECTOR = 1024
SPACE = b" \t\r\n"


class MetadataError(ValueError):
    """An observed wrapper schema or identity precondition does not match."""


@dataclass(frozen=True, slots=True)
class FieldSpan:
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class RootMetadata:
    data: bytes
    name: str
    bank: str
    instrument_id: int
    instrument_id_span: FieldSpan
    author: str
    preset_type: str
    tags: tuple[str, ...]
    flags: tuple[bool, bool]
    comment: str
    timestamp: int
    version: str
    version_span: FieldSpan

    def encode_identity(self, target_id: int, target_version: str | None) -> bytes:
        if self.instrument_id != 1:
            raise MetadataError("Expected legacy wrapper instrument ID 1")
        if not 1 < target_id < (1 << 31):
            raise MetadataError("Target wrapper ID must be a different positive int32")
        edits = [(self.instrument_id_span, str(target_id).encode("ascii"))]
        if target_version is not None:
            if len(target_version) > 64 or VERSION.fullmatch(target_version) is None:
                raise MetadataError("Target version must have four numeric components")
            encoded = target_version.encode("ascii")
            edits.append((self.version_span, str(len(encoded)).encode("ascii") + b" " + encoded))
        result = self.data
        for span, replacement in sorted(edits, key=lambda edit: edit[0].start, reverse=True):
            result = result[: span.start] + replacement + result[span.end :]
        actual = parse_root_metadata(result)
        expected_version = self.version if target_version is None else target_version
        if actual.instrument_id != target_id or actual.version != expected_version:
            raise MetadataError("Identity edit did not preserve the observed metadata schema")
        return result


@dataclass(slots=True)
class _Reader:
    data: bytes
    position: int

    def integer(self) -> tuple[int, FieldSpan]:
        while self.position < len(self.data) and self.data[self.position] in SPACE:
            self.position += 1
        start = self.position
        while self.position < len(self.data) and self.data[self.position] not in SPACE:
            self.position += 1
            if self.position - start > 20:
                raise MetadataError("Integer token exceeds its byte limit")
        token = self.data[start : self.position]
        if re.fullmatch(rb"-?[0-9]+", token) is None:
            raise MetadataError("Expected integer metadata token")
        number = int(token)
        if not -(1 << 63) <= number < (1 << 63):
            raise MetadataError("Metadata integer is outside the int64 range")
        return number, FieldSpan(start, self.position)

    def string(self) -> tuple[str, FieldSpan]:
        size, span = self.integer()
        if not 0 <= size <= MAX_STRING or self.data[self.position : self.position + 1] != b" ":
            raise MetadataError("Invalid string length or delimiter")
        self.position += 1
        end = self.position + size
        if end > len(self.data):
            raise MetadataError("Metadata string is truncated")
        if size and end < len(self.data) and self.data[end] not in SPACE:
            raise MetadataError("Metadata string lacks a following delimiter")
        try:
            text = self.data[self.position : end].decode("utf-8")
        except UnicodeDecodeError as error:
            raise MetadataError("Metadata string is not UTF-8") from error
        self.position = end
        return text, FieldSpan(span.start, end)

    def vector(self) -> tuple[str, ...]:
        count, _ = self.integer()
        item_version, _ = self.integer()
        if not 0 <= count <= MAX_VECTOR or item_version != 0:
            raise MetadataError("Unsupported string-vector count or item version")
        return tuple(self.string()[0] for _ in range(count))

    def boolean(self) -> bool:
        value, _ = self.integer()
        if value not in (0, 1):
            raise MetadataError("Expected serialized boolean 0 or 1")
        return bool(value)


def parse_root_metadata(data: bytes) -> RootMetadata:
    if len(data) > MAX_BODY or not data.startswith(HEADER):
        raise MetadataError("Expected bounded observed schema-7 archive header")
    reader = _Reader(data, len(HEADER))
    name, _ = reader.string()
    bank, _ = reader.string()
    instrument_id, id_span = reader.integer()
    author, _ = reader.string()
    preset_type, _ = reader.string()
    tracking, _ = reader.integer()
    vector_version, _ = reader.integer()
    if tracking != 0 or vector_version != 0:
        raise MetadataError("Unsupported first vector class information")
    tags = reader.vector()
    flags = reader.boolean(), reader.boolean()
    comment, _ = reader.string()
    timestamp, _ = reader.integer()
    version, version_span = reader.string()
    if version and (len(version) > 64 or VERSION.fullmatch(version) is None):
        raise MetadataError("Unsupported root instrument version string")
    return RootMetadata(
        data,
        name,
        bank,
        instrument_id,
        id_span,
        author,
        preset_type,
        tags,
        flags,
        comment,
        timestamp,
        version,
        version_span,
    )


def split_payloads(raw: bytes) -> tuple[bytes, bytes]:
    if len(raw) < 16 or len(raw) > 16 + 2 * MAX_BODY:
        raise MetadataError("Invalid two-payload envelope length")
    first, second = struct.unpack_from("<QQ", raw)
    if first > MAX_BODY or second > MAX_BODY or 16 + first + second != len(raw):
        raise MetadataError("Two-payload sizes do not consume the complete envelope")
    return raw[16 : 16 + first], raw[16 + first :]


def retarget(raw: bytes, target_id: int, target_version: str | None = None, *, standalone: bool = False) -> bytes:
    """Patch the legacy wrapper ID and optional version; retain child and opaque bytes."""
    if standalone:
        return parse_root_metadata(raw).encode_identity(target_id, target_version)
    first, second = split_payloads(raw)
    bodies = [parse_root_metadata(body).encode_identity(target_id, target_version) for body in (first, second)]
    return struct.pack("<QQ", len(bodies[0]), len(bodies[1])) + b"".join(bodies)
