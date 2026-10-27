"""Native Instruments' item container: the tree Kontakt, Battery, Maschine and Massive save.

Every NI plugin state measured so far is one of these, from the first byte.
Measured 2026-10-23 on Battery 4, Kontakt 5 and 7, and Maschine 2 states out of
the user's sets: 25 Battery, 67 Maschine and 58 Kontakt buffers parse and
serialize back byte for byte, and so does every compressed subtree that decodes.

An **item** is a little-endian u64 size covering the whole item, then 32 bytes
it carries as they are (a u32, ``hsin``, a u64, a 16 byte id), then one data
**frame**, then its children: a u32 version, a u32 count, and per child a u32,
a four character domain, a u32 type and the child item.

A **frame** is a u64 size covering the frame, a four character domain
(``DSIN`` for NI's own types, ``4KIN`` for Kontakt's), a u32 type and a u32
version, then the frame of the type it derives from, then its own data. The
chain ends at ``DSIN`` type 1, the base item.

A ``DSIN`` type 0x73 frame is a **subtree**: its data is a u32 version, a flag
byte, and either a whole item (flag 0) or that item compressed (flag 1): a u32
decompressed size, a u32 compressed size and a FastLZ stream. Most Kontakt
libraries encrypt that stream -- 53 of the 58 Kontakt bodies measured -- and an
encrypted body is opaque here and stays exactly as it was.

Every size is derived from what it covers, so :func:`serialize` recomputes them
all: an edit to one frame's data changes every enclosing size and nothing else.
A compressed subtree is compressed afresh only when its tree has changed.

Patches
-------
Battery and Kontakt keep their patch in a ``DSIN`` type 0x6d frame, whose own
data has lengths of its own: a 20 byte head whose u64 at byte 12 counts the
record stream after it, the stream -- records of a u16 id, a u32 length and
that many bytes -- and an eight byte tail. Two records in every one measured:
``0x0003`` the program, ``0x004b`` the list of files it uses, where the paths
are. The file list counts entries, never bytes. Measured 2026-10-23: Battery 4
in DawDreamer refuses a patch whose path changed length while those two
lengths did not -- it plays nothing and hands the bytes back untouched -- and
plays a patch whose path changed the same way when they did.

Paths
-----
Battery and Kontakt name a file as a list of **segments**: a u32 count, then
per segment a type byte and a u32-counted UTF-16LE string -- 1 the drive
letter, 2 a folder, 4 the file. ``C:\\Users\\Public\\...\\Kick Wave 1.wav`` is
nine segments. Folders are stored the same way without a file segment, and are
not files to relink.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re
import struct
from collections.abc import Iterator

from abletoolz.formats import FormatError, fastlz
from abletoolz.plugins.state.errors import StateTransformError

ITEM_MAGIC = b"hsin"
NI_DOMAIN = b"DSIN"
BASE_TYPE = 1
SUBTREE_TYPE = 0x73

_ITEM_HEAD_BYTES = 32
_FRAME_HEAD = struct.Struct("<Q4sII")
_CHILD_HEAD = struct.Struct("<I4sI")
# Far past any type hierarchy NI writes, and short of the recursion that parsing and serializing a chain take.
_MAX_FRAME_DEPTH = 64


@dataclasses.dataclass(slots=True)
class Frame:
    """One data frame: its type, the frame it derives from, and its own data."""

    domain: bytes
    type: int
    version: int
    base: Frame | None
    data: bytes
    subtree: Subtree | None = None

    def chain(self) -> Iterator[Frame]:
        """This frame and every frame it derives from, most derived first."""
        frame: Frame | None = self
        while frame is not None:
            yield frame
            frame = frame.base


@dataclasses.dataclass(slots=True)
class Subtree:
    """A whole item inside a frame's data, stored as it is or compressed."""

    version: int
    compressed: bool
    rest: bytes
    stored: bytes = b""
    _tree: Item | None = None
    _plain: bytes | None = None
    _opened: bool = False

    def tree(self) -> Item | None:
        """The item inside, or None when the body does not decode, which is what an encrypted one does."""
        if not self.compressed or self._opened:
            return self._tree
        self._opened = True
        size, length = struct.unpack_from("<II", self.stored, 0)
        try:
            self._plain = fastlz.decompress(self.stored[8 : 8 + length], size)
            self._tree = parse(self._plain)
        except (FormatError, StateTransformError):
            self._tree, self._plain = None, None
        return self._tree


@dataclasses.dataclass(slots=True)
class Child:
    """One child of an item, with the record that introduces it."""

    ref: int
    domain: bytes
    type: int
    item: Item


@dataclasses.dataclass(slots=True)
class Item:
    """One item: its head carried as it is, its data frame, and its children."""

    head: bytes
    frame: Frame
    children_version: int
    children: list[Child]
    tail: bytes

    def items(self) -> Iterator[Item]:
        """This item and every item below it, through subtrees that decode."""
        yield self
        for frame in self.frame.chain():
            if frame.subtree is not None and (inner := frame.subtree.tree()) is not None:
                yield from inner.items()
        for child in self.children:
            yield from child.item.items()

    def opaque(self) -> int:
        """How many compressed subtrees at or below this item do not decode."""
        count = 0
        for item in self.items():
            for frame in item.frame.chain():
                if frame.subtree is not None and frame.subtree.compressed and frame.subtree.tree() is None:
                    count += 1
        return count


def is_container(state: bytes) -> bool:
    """Whether ``state`` opens like an NI item: its size, then the magic twelve bytes in."""
    return len(state) >= 40 and state[12:16] == ITEM_MAGIC and int.from_bytes(state[:8], "little") == len(state)


def parse(state: bytes) -> Item:
    """The item tree ``state`` holds; anything that is not one exactly is refused."""
    item, end = _item(state, 0)
    if end != len(state):
        raise StateTransformError(f"NI item ends at byte {end} of {len(state)}")
    return item


def serialize(item: Item) -> bytes:
    """The bytes of ``item``, with every size recomputed from what it covers."""
    body = bytearray(_frame_bytes(item.frame))
    body += struct.pack("<II", item.children_version, len(item.children))
    for child in item.children:
        body += _CHILD_HEAD.pack(child.ref, child.domain, child.type) + serialize(child.item)
    body += item.tail
    return struct.pack("<Q", 8 + len(item.head) + len(body)) + item.head + bytes(body)


def _need(state: bytes, end: int, what: str) -> None:
    if end > len(state):
        raise StateTransformError(f"NI {what} runs past the end of the state")


def _item(state: bytes, at: int) -> tuple[Item, int]:
    _need(state, at + 8 + _ITEM_HEAD_BYTES, "item head")
    size = int.from_bytes(state[at : at + 8], "little")
    if state[at + 12 : at + 16] != ITEM_MAGIC:
        raise StateTransformError(f"No NI item at byte {at}")
    end = at + size
    _need(state, end, "item")
    frame, cursor = _frame(state, at + 8 + _ITEM_HEAD_BYTES, end)
    _need(state, cursor + 8, "child list")
    children_version, count = struct.unpack_from("<II", state, cursor)
    cursor += 8
    children = []
    for _ in range(count):
        _need(state, cursor + _CHILD_HEAD.size, "child record")
        ref, domain, kind = _CHILD_HEAD.unpack_from(state, cursor)
        child, cursor = _item(state, cursor + _CHILD_HEAD.size)
        children.append(Child(ref, domain, kind, child))
    if cursor > end:
        raise StateTransformError(f"NI item children overrun the item at byte {at}")
    return Item(state[at + 8 : at + 8 + _ITEM_HEAD_BYTES], frame, children_version, children, state[cursor:end]), end


def _frame(state: bytes, at: int, limit: int, depth: int = 0) -> tuple[Frame, int]:
    if depth > _MAX_FRAME_DEPTH:
        raise StateTransformError(f"NI frame at byte {at} derives through more than {_MAX_FRAME_DEPTH} frames")
    if at + _FRAME_HEAD.size > limit:
        raise StateTransformError(f"NI frame head past its item at byte {at}")
    size, domain, kind, version = _FRAME_HEAD.unpack_from(state, at)
    end = at + size
    if size < _FRAME_HEAD.size or end > limit:
        raise StateTransformError(f"NI frame at byte {at} does not fit its item")
    cursor = at + _FRAME_HEAD.size
    base = None
    if (domain, kind) != (NI_DOMAIN, BASE_TYPE):
        base, cursor = _frame(state, cursor, end, depth + 1)
    data = state[cursor:end]
    frame = Frame(domain, kind, version, base, data)
    if (domain, kind) == (NI_DOMAIN, SUBTREE_TYPE):
        frame.subtree = _subtree(data)
    return frame, end


def _subtree(data: bytes) -> Subtree:
    if len(data) < 5:
        raise StateTransformError("NI subtree frame too short")
    version, flag = struct.unpack_from("<IB", data, 0)
    if flag == 0:
        tree, end = _item(data, 5)
        return Subtree(version, False, data[end:], _tree=tree, _opened=True)
    if flag != 1 or len(data) < 13:
        raise StateTransformError(f"NI subtree flag {flag} is not one that has been measured")
    length = int.from_bytes(data[9:13], "little")
    if 13 + length > len(data):
        raise StateTransformError("NI compressed subtree runs past its frame")
    return Subtree(version, True, data[13 + length :], stored=data[5 : 13 + length])


def _frame_bytes(frame: Frame) -> bytes:
    inner = b"" if frame.base is None else _frame_bytes(frame.base)
    data = frame.data if frame.subtree is None else _subtree_bytes(frame.subtree)
    return (
        _FRAME_HEAD.pack(_FRAME_HEAD.size + len(inner) + len(data), frame.domain, frame.type, frame.version)
        + inner
        + data
    )


def _subtree_bytes(subtree: Subtree) -> bytes:
    if not subtree.compressed:
        assert subtree._tree is not None
        return struct.pack("<IB", subtree.version, 0) + serialize(subtree._tree) + subtree.rest
    stored = subtree.stored
    if subtree._tree is not None:
        plain = serialize(subtree._tree)
        if plain != subtree._plain:
            packed = fastlz.compress(plain)
            stored = struct.pack("<II", len(plain), len(packed)) + packed
    return struct.pack("<IB", subtree.version, 1) + stored + subtree.rest


# -- patches -----------------------------------------------------------------------

PATCH_TYPE = 0x6D
_PATCH_HEAD_BYTES = 20
_PATCH_LENGTH_AT = 12
_PATCH_TAIL_BYTES = 8
_RECORD_HEAD = struct.Struct("<HI")


def patch_records(data: bytes) -> list[tuple[int, bytes]] | None:
    """The records of a patch frame's data, or None when ``data`` is not laid out as one measured."""
    if len(data) < _PATCH_HEAD_BYTES + _PATCH_TAIL_BYTES:
        return None
    end = _PATCH_HEAD_BYTES + int.from_bytes(data[_PATCH_LENGTH_AT:_PATCH_HEAD_BYTES], "little")
    if end + _PATCH_TAIL_BYTES != len(data):
        return None
    records = []
    cursor = _PATCH_HEAD_BYTES
    while cursor < end:
        if cursor + _RECORD_HEAD.size > end:
            return None
        record_id, length = _RECORD_HEAD.unpack_from(data, cursor)
        cursor += _RECORD_HEAD.size
        if cursor + length > end:
            return None
        records.append((record_id, data[cursor : cursor + length]))
        cursor += length
    return records


def replace_in_patch(data: bytes, old: bytes, new: bytes) -> tuple[bytes, int]:
    """A patch frame's data with ``old`` replaced by ``new`` in every record, its lengths recomputed; and how many."""
    records = patch_records(data)
    if records is None:
        raise StateTransformError("This patch frame is not laid out as the measured ones are")
    count = sum(payload.count(old) for _, payload in records)
    stream = b"".join(
        _RECORD_HEAD.pack(record_id, len(changed)) + changed
        for record_id, payload in records
        for changed in (payload.replace(old, new),)
    )
    head = data[:_PATCH_LENGTH_AT] + struct.pack("<Q", len(stream))
    return head + stream + data[len(data) - _PATCH_TAIL_BYTES :], count


# -- paths -------------------------------------------------------------------------

SEGMENT_DRIVE = 1
SEGMENT_FOLDER = 2
SEGMENT_FILE = 4
_MAX_SEGMENTS = 64
_MAX_SEGMENT_CHARS = 1024

# A segment path opens with its count, then the drive segment: type 1, one character, a letter.
_SEGMENT_PATH_START = re.compile(rb"[\x02-\x40]\x00\x00\x00\x01\x01\x00\x00\x00[A-Za-z]\x00", re.DOTALL)


@dataclasses.dataclass(frozen=True, slots=True)
class SegmentPath:
    """A file path stored as segments, where it sits in a frame's data, spelled as Windows spells it."""

    start: int
    end: int
    path: str


def segment_paths(data: bytes) -> Iterator[SegmentPath]:
    """Every file path ``data`` stores as segments: a drive, folders, then exactly one file, last."""
    for match in _SEGMENT_PATH_START.finditer(data):
        found = _segment_path(data, match.start())
        if found is not None:
            yield found


def encode_segment_path(path: str) -> bytes:
    """``path`` as segments; only an absolute Windows path with a drive letter has a measured spelling."""
    windows = pathlib.PureWindowsPath(path)
    drive = windows.drive
    if len(drive) != 2 or drive[1] != ":" or not drive[0].isalpha() or not windows.root:
        raise StateTransformError(f"{path} is not a Windows path with a drive letter, the one form measured")
    parts = windows.parts[1:]
    if not parts:
        raise StateTransformError(f"{path} names no file")
    segments = [(SEGMENT_DRIVE, drive[0]), *((SEGMENT_FOLDER, part) for part in parts[:-1]), (SEGMENT_FILE, parts[-1])]
    out = bytearray(struct.pack("<I", len(segments)))
    for kind, text in segments:
        out += struct.pack("<BI", kind, len(text)) + text.encode("utf-16-le")
    return bytes(out)


def _segment_path(data: bytes, at: int) -> SegmentPath | None:
    count = int.from_bytes(data[at : at + 4], "little")
    if not 2 <= count <= _MAX_SEGMENTS:
        return None
    cursor = at + 4
    segments: list[tuple[int, str]] = []
    for _ in range(count):
        if cursor + 5 > len(data):
            return None
        kind = data[cursor]
        chars = int.from_bytes(data[cursor + 1 : cursor + 5], "little")
        end = cursor + 5 + 2 * chars
        if not 1 <= chars <= _MAX_SEGMENT_CHARS or end > len(data):
            return None
        try:
            text = data[cursor + 5 : end].decode("utf-16-le")
        except UnicodeDecodeError:
            return None
        if any(char < " " for char in text):
            return None
        segments.append((kind, text))
        cursor = end
    kinds = [kind for kind, _ in segments]
    if kinds[0] != SEGMENT_DRIVE or kinds[-1] != SEGMENT_FILE or any(kind != SEGMENT_FOLDER for kind in kinds[1:-1]):
        return None
    drive, *rest = (text for _, text in segments)
    return SegmentPath(at, cursor, f"{drive}:\\" + "\\".join(rest))
