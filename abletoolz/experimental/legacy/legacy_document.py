"""Read the AB1E v3 document dialect observed in the copied Suite 8 sets.

Class revisions, field order, collection IDs, allocation counters and byte
ranges remain available to a later converter. This module never writes sets.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from pathlib import Path


class DecodeError(ValueError):
    """Unsupported or malformed bytes, with their location in the source."""


@dataclass(frozen=True, slots=True)
class ClassType:
    revision: int
    name: str


type TypeRef = ClassType | int
type Atom = bool | int | float | str | bytes | tuple[int, ...] | ClassType | None


@dataclass(frozen=True, slots=True)
class Definition:
    kind: ClassType
    count: int
    fields: tuple[tuple[str, TypeRef], ...]


@dataclass(frozen=True, slots=True)
class Entry:
    identifier: int | None
    node: Node


@dataclass(frozen=True, slots=True)
class Node:
    kind: TypeRef
    start: int
    end: int
    atom: Atom = None
    fields: tuple[tuple[str, Node], ...] = ()
    entries: tuple[Entry, ...] = ()
    allocation: int | None = None
    element_type: TypeRef | None = None

    def field(self, name: str) -> Node:
        for key, value in self.fields:
            if key == name:
                return value
        raise DecodeError(f"0x{self.start:x}: {self.class_name} has no field {name}")

    @property
    def class_name(self) -> str:
        return self.kind.name if isinstance(self.kind, ClassType) else f"primitive:{self.kind:02x}"


@dataclass(frozen=True, slots=True)
class LegacyDocument:
    source: bytes
    revision: int
    header_word: int
    definitions: tuple[Definition, ...]
    root: Node

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.source).hexdigest()

    @classmethod
    def read(cls, path: Path) -> LegacyDocument:
        return decode(path.read_bytes())


class Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        self.definitions: dict[ClassType, Definition] = {}
        self.trail: list[str] = []
        self.nodes = 0
        # Fieldless records cost no input bytes, so the node budget follows the input size.
        self.node_limit = min(2_000_000, 32 * len(data))

    def fail(self, message: str) -> DecodeError:
        return DecodeError(f"0x{self.pos:x}: {message} ({'/'.join(self.trail[-12:])})")

    def take(self, size: int) -> bytes:
        if size < 0 or size > len(self.data) - self.pos:
            raise self.fail(f"need {size} bytes, have {len(self.data) - self.pos}")
        result = self.data[self.pos : self.pos + size]
        self.pos += size
        return result

    def u8(self) -> int:
        return self.take(1)[0]

    def u32(self) -> int:
        return int.from_bytes(self.take(4), "little")

    def i32(self) -> int:
        return int.from_bytes(self.take(4), "little", signed=True)

    def text(self) -> str:
        return self.take(self.u32() * 2).decode("utf-16le", errors="surrogatepass")

    def kind(self) -> TypeRef:
        tag = self.u8()
        if tag:
            return tag
        revision = self.u8()
        length = self.u8()
        try:
            return ClassType(revision, self.take(length).decode("ascii"))
        except UnicodeDecodeError as error:
            raise self.fail("non-ASCII class name") from error

    def definition(self) -> Definition:
        kind = self.kind()
        if not isinstance(kind, ClassType) or not kind.name:
            raise self.fail("expected named class definition")
        count = self.i32()
        if count not in (-1, -3) and not 0 <= count <= 10000:
            raise self.fail(f"unsupported class field count {count}")
        fields = tuple((self.text(), self.kind()) for _ in range(max(0, count)))
        names = [name for name, _ in fields]
        if len(names) != len(set(names)):
            raise self.fail(f"duplicate fields in {kind}")
        return Definition(kind, count, fields)

    def value(self, kind: TypeRef, depth: int = 0) -> Node:
        if depth > 128:
            raise self.fail("nesting limit exceeded")
        self.nodes += 1
        if self.nodes > self.node_limit:
            raise self.fail("node limit exceeded")
        start = self.pos
        if isinstance(kind, int):
            return self.primitive(kind, start)
        definition = self.definitions.get(kind)
        if definition is None:
            raise self.fail(f"undefined class {kind}")
        if definition.count >= 0:
            fields: list[tuple[str, Node]] = []
            for name, field_type in definition.fields:
                self.trail.append(f"{kind.name}.{name}")
                fields.append((name, self.value(field_type, depth + 1)))
                self.trail.pop()
            return Node(kind, start, self.pos, fields=tuple(fields))
        count = self.u32()
        entries: list[Entry] = []
        if definition.count == -1:
            ids: set[int] = set()
            while True:
                item_type = self.kind()
                if item_type == ClassType(0, ""):
                    break
                identifier = self.u32()
                if identifier in ids:
                    raise self.fail(f"duplicate collection id {identifier}")
                ids.add(identifier)
                self.trail.append(f"{kind.name}[{identifier}]")
                entries.append(Entry(identifier, self.value(item_type, depth + 1)))
                self.trail.pop()
            return Node(kind, start, self.pos, entries=tuple(entries), allocation=count)
        item_type = self.kind()
        if count > 2_000_000:
            raise self.fail("array element limit exceeded")
        for index in range(count):
            self.trail.append(f"{kind.name}[{index}]")
            entries.append(Entry(None, self.value(item_type, depth + 1)))
            self.trail.pop()
        return Node(kind, start, self.pos, entries=tuple(entries), element_type=item_type)

    def primitive(self, kind: int, start: int) -> Node:
        atom: Atom
        if kind == 0x10:
            value = self.u8()
            if value not in (0, 1):
                raise self.fail(f"invalid boolean {value}")
            atom = bool(value)
        elif kind == 0x11:
            atom = self.i32()
        elif kind == 0x12:
            atom = float(struct.unpack("<f", self.take(4))[0])
        elif kind == 0x14:
            atom = self.text()
        elif kind == 0x16:
            atom = tuple(self.i32() for _ in range(4))
        elif kind == 0x17:
            atom = float(struct.unpack("<d", self.take(8))[0])
        elif kind == 0x18:
            atom = int.from_bytes(self.take(8), "little", signed=True)
        elif kind == 0x20:
            atom = self.kind()
        elif kind in (0x31, 0x32, 0x35, 0x40):
            width = {0x31: 1, 0x32: 2, 0x35: 4, 0x40: 4}[kind]
            atom = self.take(self.u32() * width)
        else:
            raise self.fail(f"unsupported primitive 0x{kind:x}")
        return Node(kind, start, self.pos, atom=atom)


def decode(data: bytes) -> LegacyDocument:
    reader = Reader(data)
    if reader.take(4) != bytes.fromhex("ab1e5678"):
        raise reader.fail("not an AB1E document")
    revision = reader.u8()
    if revision != 3:
        raise reader.fail(f"unsupported document dialect {revision}")
    header_word = reader.u32()
    root_type = reader.kind()
    if not isinstance(root_type, ClassType) or root_type.name != "LiveDocument":
        raise reader.fail(f"unsupported root {root_type}")
    count = reader.i32()
    if not 0 < count <= 10000:
        raise reader.fail(f"invalid definition count {count}")
    for _ in range(count):
        definition = reader.definition()
        if definition.kind in reader.definitions:
            raise reader.fail(f"duplicate class definition {definition.kind}")
        reader.definitions[definition.kind] = definition
    root = reader.value(root_type)
    if reader.pos != len(data):
        raise reader.fail(f"{len(data) - reader.pos} trailing bytes")
    return LegacyDocument(data, revision, header_word, tuple(reader.definitions.values()), root)
