"""Read and edit the measured Trash 1 revision-21 bank without loading a DLL.

Named coordinates come from the original engine's typed descriptor table.
They describe serialized settings, including controls absent from the public
VST inventory. A newer-plugin transform must establish its own correspondence.
"""

from __future__ import annotations

import json
import struct
from collections.abc import Mapping
from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path

from pydantic import TypeAdapter

from abletoolz.plugins.state import StateTransformError
from abletoolz.plugins.state.controls import Parameter, ParameterMap
from abletoolz.plugins.state.model import (
    EnumValue,
    FieldEncoding,
    FieldKey,
    FixedByteMap,
    MappedState,
    StateField,
    StateValue,
)
from abletoolz.plugins.state.plugin_map import PluginMap, StateFormat

MAGIC = 0x003A2B11
REVISION = 21
LENGTH = 1117
PROGRAMS = 16
MAX_NAME = 4096
PROGRAM_LENGTHS = {21: 1117, 22: 1121}
STATE_FORMAT = "izotope.trash1.bank/revisions21-22"


# Retained for callers of the first research codec.
Field = StateField


@cache
def algorithms() -> tuple[EnumValue, ...]:
    """Names and IDs from the fingerprinted original engine's descriptor table."""
    path = Path(__file__).with_name("data") / "trash1-algorithms.json"
    return TypeAdapter(tuple[EnumValue, ...]).validate_json(path.read_bytes(), strict=True)


@cache
def fields() -> tuple[Field, ...]:
    """Return the named, typed coordinates for this measured format."""
    path = Path(__file__).with_name("data") / "trash1-v21.json"
    data: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("fields"), list):
        raise ValueError("Missing Trash 1 field definitions")
    result = []
    for row in data["fields"]:
        if not isinstance(row, dict):
            raise ValueError("Invalid field definition")
        ident, name, encoding = row.get("id"), row.get("name"), row.get("encoding")
        offset, element, parameter = row.get("byte_offset"), row.get("element_id"), row.get("parameter_id")
        if (
            type(ident) is not int
            or not isinstance(name, str)
            or encoding not in ("float32_le", "uint32_le", "bool8")
            or type(offset) is not int
            or type(element) is not int
            or type(parameter) is not int
        ):
            raise ValueError("Invalid field definition types")
        if not isinstance(encoding, str):
            raise ValueError("Invalid field encoding")
        if not 16 <= offset <= LENGTH + 4 - (1 if encoding == "bool8" else 4):
            raise ValueError("Field exceeds program state")
        if encoding == "float32_le":
            kind = FieldEncoding.FLOAT32_LE
        elif encoding == "uint32_le":
            kind = FieldEncoding.UINT32_LE
        else:
            kind = FieldEncoding.BOOL8
        choices = algorithms() if name.startswith("Trash:") and name.endswith(" Algorithm") else ()
        result.append(Field(ident, name, kind, offset, element, parameter, choices))
    if [field.id for field in result] != list(range(296)):
        raise ValueError("Incomplete or ambiguous Trash 1 field inventory")
    return tuple(result)


@cache
def byte_map() -> FixedByteMap:
    """The complete revision-21 storage map; it makes no version-conversion claim."""
    return FixedByteMap("izotope.trash1.program/revision21", LENGTH + 4, fields())


@dataclass(frozen=True, slots=True)
class _ControlData:
    source_sha256: str
    evidence_sha256: str
    scope: str
    positions: tuple[float, ...]
    controls: tuple[Parameter, ...]


@cache
def parameter_map() -> ParameterMap:
    """Public-to-saved bindings with sampled values and bounded native-check status."""
    path = Path(__file__).with_name("data") / "trash1-controls.json"
    data = TypeAdapter(_ControlData).validate_json(path.read_bytes(), strict=True)
    return ParameterMap(byte_map(), data.controls, data.positions, data.source_sha256, data.evidence_sha256, data.scope)


# Compatibility for the original research API.
state_map = byte_map
control_map = parameter_map


@dataclass(frozen=True, slots=True)
class Program:
    state: bytes
    name: bytes

    def __post_init__(self) -> None:
        _revision(self.state)
        if type(self.state) is not bytes or type(self.name) is not bytes or len(self.name) > MAX_NAME:
            raise StateTransformError("Trash program requires immutable state and bounded name bytes")

    @property
    def revision(self) -> int:
        return _revision(self.state)

    @property
    def settings(self) -> MappedState:
        if self.revision != REVISION:
            raise StateTransformError("Revision 22 fields are unmapped; its complete state is preserved")
        return byte_map().decode(self.state)

    def with_values(self, changes: Mapping[FieldKey, StateValue]) -> Program:
        return replace(self, state=self.settings.with_values(changes).encode())


@dataclass(frozen=True, slots=True)
class Bank:
    programs: tuple[Program, ...]

    def __post_init__(self) -> None:
        if type(self.programs) is not tuple or len(self.programs) != PROGRAMS:
            raise StateTransformError("Trash 1 banks require sixteen immutable program records")

    @classmethod
    def parse(cls, raw: bytes) -> Bank:
        return decode(raw)

    def encode(self) -> bytes:
        """Preserve every unedited state and name byte."""
        raw = b"".join(p.state + struct.pack("<I", len(p.name)) + p.name for p in self.programs)
        decode(raw)
        return raw

    def program(self, index: int) -> Program:
        """Select a zero-based bank program, refusing missing or invalid indices."""
        return self.programs[_program(index)]

    def value(self, program: int, field_id: int) -> StateValue:
        return self.program(program).settings.value(field_id)

    def with_value(self, program: int, field_id: int, value: StateValue) -> Bank:
        """Edit a stored physical value; normalized knob curves are separate."""
        return self.with_values(program, {field_id: value})

    def with_values(self, program: int, changes: Mapping[FieldKey, StateValue]) -> Bank:
        """Edit named physical settings in one explicitly selected bank program."""
        index = _program(program)
        programs = list(self.programs)
        programs[index] = programs[index].with_values(changes)
        return Bank(tuple(programs))


@cache
def plugin_map() -> PluginMap[Bank]:
    """Bank framing and revision-21 knowledge; revision-22 fields stay opaque."""
    parameters = parameter_map()
    return PluginMap(
        id="izotope.trash1",
        name="iZotope Trash",
        version=None,
        scope="Original VST2 build fingerprint; sixteen-program banks; product release unknown",
        binary_sha256=(parameters.source_sha256,),
        formats=(
            StateFormat(
                id=STATE_FORMAT,
                decoder=Bank.parse,
                scope=(
                    "Revision-21/22 bank framing is preserved. The byte map addresses each revision-21 program, "
                    "not absolute bank offsets. Revision-22 fields are opaque. The host retains program selection."
                ),
                byte_maps=(byte_map(),),
                parameter_maps=(parameters,),
            ),
        ),
    )


def _program(index: int) -> int:
    if type(index) is not int or not 0 <= index < PROGRAMS:
        raise ValueError("Program index must be 0..15")
    return index


def _revision(raw: bytes) -> int:
    if len(raw) < 16:
        raise StateTransformError("Truncated Trash 1 program state")
    length, magic, revision, duplicate = struct.unpack_from("<4I", raw)
    if magic != MAGIC or PROGRAM_LENGTHS.get(revision) != length or duplicate != length or len(raw) != length + 4:
        raise StateTransformError("Unsupported Trash 1 length, magic or revision")
    return int(revision)


def decode(raw: bytes) -> Bank:
    """Read sixteen revision-21/22 frames; only revision 21 has field semantics."""
    if len(raw) > PROGRAMS * (max(PROGRAM_LENGTHS.values()) + 8 + MAX_NAME):
        raise ValueError("Trash 1 bank exceeds bounded record sizes")
    programs = []
    cursor = 0
    for _ in range(PROGRAMS):
        if cursor + 16 > len(raw):
            raise ValueError("Truncated Trash 1 program state or name length")
        length, magic, revision, duplicate_length = struct.unpack_from("<4I", raw, cursor)
        if magic != MAGIC or PROGRAM_LENGTHS.get(revision) != length or duplicate_length != length:
            raise ValueError("Unsupported Trash 1 length, magic or revision")
        end = cursor + length + 4
        if end + 4 > len(raw):
            raise ValueError("Truncated Trash 1 program state or name length")
        name_length = struct.unpack_from("<I", raw, end)[0]
        next_record = end + 4 + name_length
        if name_length > MAX_NAME or next_record > len(raw):
            raise ValueError("Unbounded or truncated Trash 1 program name")
        programs.append(Program(raw[cursor:end], raw[end + 4 : next_record]))
        cursor = next_record
    if cursor != len(raw):
        raise ValueError("Trailing bytes after Trash 1 bank")
    return Bank(tuple(programs))
