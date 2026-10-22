"""Named saved fields and lossless state editing, independent of a host container.

A map describes storage coordinates, not knob curves or plugin equivalence.
Vendor codecs own framing and revision checks; unmodelled bytes stay intact.
"""

from __future__ import annotations

import enum
import hashlib
import math
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from abletoolz.plugins.state import StateTransformError

type StateValue = float | int | bool
type FieldKey = int | str


class EncodableState(Protocol):
    """A decoded state that can be returned to its original binary format."""

    def encode(self) -> bytes: ...


class FieldEncoding(enum.StrEnum):
    FLOAT32_LE = "float32_le"
    UINT32_LE = "uint32_le"
    BOOL8 = "bool8"

    @property
    def size(self) -> int:
        return 1 if self is FieldEncoding.BOOL8 else 4


@dataclass(frozen=True, slots=True)
class StateField:
    """A stable saved-field identity; ``id`` is independent of a VST knob index."""

    id: int
    name: str
    encoding: FieldEncoding
    byte_offset: int
    element_id: int | None = None
    parameter_id: int | None = None

    def __post_init__(self) -> None:
        if type(self.id) is not int or self.id < 0 or not self.name:
            raise StateTransformError("State fields need a nonnegative ID and a name")
        if not isinstance(self.encoding, FieldEncoding):
            raise StateTransformError("State field encoding must be explicit")
        if type(self.byte_offset) is not int or self.byte_offset < 0:
            raise StateTransformError("State field offset must be nonnegative")

    def read(self, raw: bytes) -> StateValue:
        if self.byte_offset + self.encoding.size > len(raw):
            raise StateTransformError(f"Truncated saved field {self.name!r}")
        if self.encoding is FieldEncoding.BOOL8:
            value = raw[self.byte_offset]
            if value not in (0, 1):
                raise StateTransformError(f"Noncanonical Boolean byte in {self.name!r}; raw bytes remain preserved")
            return bool(value)
        if self.encoding is FieldEncoding.UINT32_LE:
            integer: int = struct.unpack_from("<I", raw, self.byte_offset)[0]
            return integer
        number: float = struct.unpack_from("<f", raw, self.byte_offset)[0]
        return number

    def pack(self, value: StateValue) -> bytes:
        if self.encoding is FieldEncoding.BOOL8:
            if type(value) is not bool:
                raise StateTransformError(f"{self.name!r} requires True or False")
            return bytes([int(value)])
        if self.encoding is FieldEncoding.UINT32_LE:
            if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
                raise StateTransformError(f"{self.name!r} requires an unsigned 32-bit word")
            return struct.pack("<I", value)
        if type(value) not in (int, float):
            raise StateTransformError(f"{self.name!r} requires a finite number")
        try:
            number = float(value)
            if not math.isfinite(number):
                raise StateTransformError(f"{self.name!r} requires a finite number")
            return struct.pack("<f", number)
        except (OverflowError, struct.error) as error:
            raise StateTransformError(f"{self.name!r} cannot be represented as float32") from error


@dataclass(frozen=True, slots=True)
class StateMap:
    """One fixed serialized layout, optionally bounded to a measured template.

    ``opaque_sha256`` hashes the complete buffer after zeroing mapped fields.
    It prevents applying a partial map to a different layout of the same size.
    """

    format_id: str
    byte_count: int
    fields: tuple[StateField, ...]
    opaque_sha256: str | None = None

    def __post_init__(self) -> None:
        if not self.format_id or type(self.byte_count) is not int or self.byte_count <= 0:
            raise StateTransformError("State map needs a format identity and positive byte count")
        if type(self.fields) is not tuple or not self.fields:
            raise StateTransformError("State map fields must be a nonempty immutable tuple")
        if len({f.id for f in self.fields}) != len(self.fields):
            raise StateTransformError("Duplicate state field ID")
        if len({f.name for f in self.fields}) != len(self.fields):
            raise StateTransformError("Duplicate state field name")
        occupied: set[int] = set()
        for field in self.fields:
            end = field.byte_offset + field.encoding.size
            if end > self.byte_count:
                raise StateTransformError(f"State field {field.name!r} exceeds the buffer")
            positions = set(range(field.byte_offset, end))
            if occupied & positions:
                raise StateTransformError(f"Overlapping state field {field.name!r}")
            occupied.update(positions)
        if self.opaque_sha256 is not None:
            if len(self.opaque_sha256) != 64 or any(c not in "0123456789abcdef" for c in self.opaque_sha256):
                raise StateTransformError("Opaque template guard requires a lowercase SHA256")

    def field(self, key: FieldKey) -> StateField:
        for field in self.fields:
            if (type(key) is int and field.id == key) or (isinstance(key, str) and field.name == key):
                return field
        raise StateTransformError(f"Unknown field {key!r} in {self.format_id}")

    def opaque_digest(self, raw: bytes) -> str:
        if len(raw) != self.byte_count:
            raise StateTransformError(f"{self.format_id} requires {self.byte_count} bytes, received {len(raw)}")
        masked = bytearray(raw)
        for field in self.fields:
            masked[field.byte_offset : field.byte_offset + field.encoding.size] = bytes(field.encoding.size)
        return hashlib.sha256(masked).hexdigest()

    def decode(self, raw: bytes) -> MappedState:
        return MappedState(self, raw)


@dataclass(frozen=True, slots=True)
class MappedState:
    """A typed view of saved values backed by their complete original bytes."""

    map: StateMap
    data: bytes

    def __post_init__(self) -> None:
        if type(self.data) is not bytes:
            raise StateTransformError("Mapped state must own immutable bytes")
        if len(self.data) != self.map.byte_count:
            raise StateTransformError(f"{self.map.format_id} requires {self.map.byte_count} bytes")
        if self.map.opaque_sha256 is not None and self.map.opaque_digest(self.data) != self.map.opaque_sha256:
            raise StateTransformError(f"{self.map.format_id} does not match the measured opaque template")

    def value(self, field: FieldKey) -> StateValue:
        return self.map.field(field).read(self.data)

    def with_values(self, changes: Mapping[FieldKey, StateValue]) -> MappedState:
        """Apply typed field edits atomically; physical ranges and enums are vendor knowledge."""
        edits: dict[int, tuple[StateField, bytes]] = {}
        for key, value in changes.items():
            field = self.map.field(key)
            if field.id in edits:
                raise StateTransformError(f"Two changes address the same field {field.name!r}")
            edits[field.id] = field, field.pack(value)
        raw = bytearray(self.data)
        for field, encoded in edits.values():
            raw[field.byte_offset : field.byte_offset + len(encoded)] = encoded
        return MappedState(self.map, bytes(raw))

    def encode(self) -> bytes:
        return self.data
