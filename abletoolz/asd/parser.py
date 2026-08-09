"""abletoolz.asd.parser  —  schema-driven parser/serializer for Ableton's binary .asd format.

Layout (reverse-engineered, byte-exact against Live 12 and legacy fixtures — see FORMAT.md):

  [06 49][u64 N][u32 x N leading table][17 pre-doc bytes][document...]

Each document:

  [AB 1E 56 78][u8 version][u32 doc id][ascii root class][i32 class count][class defs][root data]

Strings: ascii = 00 + u8 len + bytes; utf16 = u32 char count + UTF-16-LE bytes.
Class def: name + i32 field count (-1 list container, -3 array container) + per-field
(utf16 name + type = ascii class ref or 1-byte primitive id).

Warp markers live in SampleData.WarpMarkers, a list whose elements serialize as
``00 0A "WarpMarker"`` + u32 element index + f64 SecTime + f64 BeatTime; the schema table
carries a ``WarpMarker`` class def (SecTime/BeatTime doubles) when the list is non-empty.

No third-party dependencies.
"""

from __future__ import annotations

import shutil
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import override

FILE_MAGIC = b"\x06\x49"
DOC_MAGIC = b"\xab\x1e\x56\x78"

PRIM_BOOL = 0x10
PRIM_INT = 0x11
PRIM_FLOAT = 0x12
PRIM_STRING = 0x14
PRIM_DOUBLE = 0x17
PRIM_ARRAY_ELEM_SIZE: dict[int, int] = {0x31: 1, 0x32: 2, 0x35: 4, 0x40: 4}

UNSET_INT = -0x80000000  # INT_MIN sentinel Live uses for "analysis not set"
UNSET_DOUBLE = 1.7976931348623157e308  # DBL_MAX sentinel

PRE_DOC_BYTES = bytes.fromhex("00000000" "64000000" "04000000" "00000000" "00")

FieldType = str | int  # ascii class name, or primitive type id


@dataclass
class ClassDef:
    """One schema-table entry: a named class and its ordered fields."""

    name: str
    field_count: int  # >= 0 plain struct; -1 list container; -3 array container
    fields: list[tuple[str, FieldType]]


@dataclass
class Obj:
    """A serialized instance of a schema class (field name -> value)."""

    cls: str
    values: dict[str, Value]


@dataclass
class ListValue:
    """List container payload (field count -1): indexed, per-element-typed elements."""

    elems: list[Obj]


@dataclass
class ArrayValue:
    """Array container payload (field count -3): element class written once up front."""

    elem_cls: str
    elems: list[Obj]


@dataclass
class PrimArray:
    """Primitive array (type ids 0x31/0x32/0x35/0x40) kept as raw little-endian bytes."""

    type_id: int
    raw: bytes

    @property
    def count(self) -> int:
        """Element count implied by the raw byte length."""
        return len(self.raw) // PRIM_ARRAY_ELEM_SIZE[self.type_id]


Value = int | float | str | Obj | ListValue | ArrayValue | PrimArray


@dataclass
class Document:
    """One AB1E5678-delimited document: schema table + serialized root object."""

    version: int
    doc_id: int
    schema: list[ClassDef]
    root: Obj

    def class_def(self, name: str) -> ClassDef:
        """Return the schema entry for ``name``."""
        for cd in self.schema:
            if cd.name == name:
                return cd
        raise KeyError(name)


@dataclass
class WarpMarker:
    """A warp marker: audio position (seconds) pinned to a grid position (beats)."""

    marker_id: int
    sec_time: float  # seconds into the audio file
    beat_time: float  # beat position on the grid

    @override
    def __repr__(self) -> str:
        """Compact debug form."""
        return f"WarpMarker(id={self.marker_id}, sec={self.sec_time:.6f}, beat={self.beat_time:.6f})"

    def pack(self) -> bytes:
        """Serialize as one 32-byte list-element record (tag + index + SecTime + BeatTime)."""
        return (
            b"\x00\x0aWarpMarker"
            + struct.pack("<I", self.marker_id)
            + struct.pack("<d", self.sec_time)
            + struct.pack("<d", self.beat_time)
        )


class _Reader:
    """Cursor over file bytes with primitive decode helpers."""

    def __init__(self, data: bytes, pos: int = 0) -> None:
        self.data = data
        self.pos = pos

    def u8(self) -> int:
        v = self.data[self.pos]
        self.pos += 1
        return v

    def u32(self) -> int:
        v = int(struct.unpack_from("<I", self.data, self.pos)[0])
        self.pos += 4
        return v

    def i32(self) -> int:
        v = int(struct.unpack_from("<i", self.data, self.pos)[0])
        self.pos += 4
        return v

    def f32(self) -> float:
        v = float(struct.unpack_from("<f", self.data, self.pos)[0])
        self.pos += 4
        return v

    def f64(self) -> float:
        v = float(struct.unpack_from("<d", self.data, self.pos)[0])
        self.pos += 8
        return v

    def raw(self, n: int) -> bytes:
        v = self.data[self.pos : self.pos + n]
        if len(v) != n:
            raise ValueError(f"truncated .asd: wanted {n} bytes at 0x{self.pos:x}")
        self.pos += n
        return v

    def ascii_str(self) -> str:
        if self.data[self.pos] != 0:
            raise ValueError(f"expected ascii string at 0x{self.pos:x}, got byte 0x{self.data[self.pos]:02x}")
        ln = self.data[self.pos + 1]
        s = self.data[self.pos + 2 : self.pos + 2 + ln].decode("ascii")
        self.pos += 2 + ln
        return s

    def utf16_str(self) -> str:
        n = self.u32()
        return self.raw(2 * n).decode("utf-16-le")


class _Writer:
    """Byte accumulator with primitive encode helpers."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def u8(self, v: int) -> None:
        self.buf.append(v)

    def u32(self, v: int) -> None:
        self.buf += struct.pack("<I", v)

    def i32(self, v: int) -> None:
        self.buf += struct.pack("<i", v)

    def f32(self, v: float) -> None:
        self.buf += struct.pack("<f", v)

    def f64(self, v: float) -> None:
        self.buf += struct.pack("<d", v)

    def ascii_str(self, s: str) -> None:
        raw = s.encode("ascii")
        self.buf += b"\x00" + bytes([len(raw)]) + raw

    def utf16_str(self, s: str) -> None:
        self.u32(len(s))
        self.buf += s.encode("utf-16-le")


def _parse_class_def(r: _Reader) -> ClassDef:
    name = r.ascii_str()
    fc = r.i32()
    fields: list[tuple[str, FieldType]] = []
    for _ in range(max(fc, 0)):
        fname = r.utf16_str()
        ftype: FieldType = r.ascii_str() if r.data[r.pos] == 0 else r.u8()
        fields.append((fname, ftype))
    return ClassDef(name, fc, fields)


def _parse_prim(r: _Reader, ftype: int) -> Value:
    if ftype == PRIM_BOOL:
        return r.u8()
    if ftype == PRIM_INT:
        return r.i32()
    if ftype == PRIM_FLOAT:
        return r.f32()
    if ftype == PRIM_STRING:
        return r.utf16_str()
    if ftype == PRIM_DOUBLE:
        return r.f64()
    if ftype in PRIM_ARRAY_ELEM_SIZE:
        n = r.u32()
        return PrimArray(ftype, r.raw(n * PRIM_ARRAY_ELEM_SIZE[ftype]))
    raise ValueError(f"unknown primitive type id 0x{ftype:02x} at 0x{r.pos:x}")


def _parse_value(r: _Reader, ftype: FieldType, classes: dict[str, ClassDef]) -> Value:
    if isinstance(ftype, int):
        return _parse_prim(r, ftype)
    cd = classes[ftype]
    if cd.field_count >= 0:
        return _parse_obj(r, cd, classes)
    if cd.field_count == -1:  # list container
        n = r.u32()
        elems: list[Obj] = []
        for i in range(n):
            ecls = r.ascii_str()
            idx = r.u32()
            if idx != i:
                raise ValueError(f"list element index {idx} != position {i} at 0x{r.pos:x}")
            elems.append(_parse_obj(r, classes[ecls], classes))
        term = r.raw(2)
        if term != b"\x00\x00":
            raise ValueError(f"bad list terminator {term.hex()} at 0x{r.pos:x}")
        return ListValue(elems)
    if cd.field_count == -3:  # array container
        n = r.u32()
        ecls = r.ascii_str()
        return ArrayValue(ecls, [_parse_obj(r, classes[ecls], classes) for _ in range(n)])
    raise ValueError(f"unsupported container field count {cd.field_count} for class {cd.name}")


def _parse_obj(r: _Reader, cd: ClassDef, classes: dict[str, ClassDef]) -> Obj:
    values: dict[str, Value] = {}
    for fname, ftype in cd.fields:
        values[fname] = _parse_value(r, ftype, classes)
    return Obj(cd.name, values)


def _write_prim(w: _Writer, v: Value, ftype: int) -> None:
    if ftype == PRIM_BOOL:
        assert isinstance(v, int)
        w.u8(v)
    elif ftype == PRIM_INT:
        assert isinstance(v, int)
        w.i32(v)
    elif ftype == PRIM_FLOAT:
        assert isinstance(v, float)
        w.f32(v)
    elif ftype == PRIM_STRING:
        assert isinstance(v, str)
        w.utf16_str(v)
    elif ftype == PRIM_DOUBLE:
        assert isinstance(v, float)
        w.f64(v)
    elif ftype in PRIM_ARRAY_ELEM_SIZE:
        assert isinstance(v, PrimArray)
        w.u32(v.count)
        w.buf += v.raw
    else:
        raise ValueError(f"unknown primitive type id 0x{ftype:02x}")


def _write_value(w: _Writer, v: Value, ftype: FieldType, classes: dict[str, ClassDef]) -> None:
    if isinstance(ftype, int):
        _write_prim(w, v, ftype)
        return
    cd = classes[ftype]
    if cd.field_count >= 0:
        assert isinstance(v, Obj)
        _write_obj(w, v, cd, classes)
    elif cd.field_count == -1:
        assert isinstance(v, ListValue)
        w.u32(len(v.elems))
        for i, elem in enumerate(v.elems):
            w.ascii_str(elem.cls)
            w.u32(i)
            _write_obj(w, elem, classes[elem.cls], classes)
        w.buf += b"\x00\x00"
    elif cd.field_count == -3:
        assert isinstance(v, ArrayValue)
        w.u32(len(v.elems))
        w.ascii_str(v.elem_cls)
        for elem in v.elems:
            _write_obj(w, elem, classes[v.elem_cls], classes)
    else:
        raise ValueError(f"unsupported container field count {cd.field_count} for class {cd.name}")


def _write_obj(w: _Writer, obj: Obj, cd: ClassDef, classes: dict[str, ClassDef]) -> None:
    for fname, ftype in cd.fields:
        _write_value(w, obj.values[fname], ftype, classes)


def _parse_document(r: _Reader) -> Document:
    if r.raw(4) != DOC_MAGIC:
        raise ValueError(f"missing document magic at 0x{r.pos - 4:x}")
    version = r.u8()
    doc_id = r.u32()
    root_cls = r.ascii_str()
    n_classes = r.i32()
    schema = [_parse_class_def(r) for _ in range(n_classes)]
    classes = {cd.name: cd for cd in schema}
    root = _parse_obj(r, classes[root_cls], classes)
    return Document(version, doc_id, schema, root)


def _write_document(w: _Writer, doc: Document) -> None:
    w.buf += DOC_MAGIC
    w.u8(doc.version)
    w.u32(doc.doc_id)
    w.ascii_str(doc.root.cls)
    w.i32(len(doc.schema))
    classes = {cd.name: cd for cd in doc.schema}
    for cd in doc.schema:
        w.ascii_str(cd.name)
        w.i32(cd.field_count)
        for fname, ftype in cd.fields:
            w.utf16_str(fname)
            if isinstance(ftype, int):
                w.u8(ftype)
            else:
                w.ascii_str(ftype)
    _write_obj(w, doc.root, classes[doc.root.cls], classes)


WARP_MARKER_CLASS = ClassDef("WarpMarker", 2, [("SecTime", PRIM_DOUBLE), ("BeatTime", PRIM_DOUBLE)])


class AsdFile:
    """Parsed .asd file: leading table + documents, byte-exact round trip.

    Example::

        asd = AsdFile.load(Path("track.mp3.asd"))
        asd.set_grid(bpm=174.0, anchor_seconds=0.532)
        asd.save()          # backs up original, writes in-place
    """

    def __init__(
        self,
        path: Path,
        lead_table: list[int],
        pre_doc: bytes,
        documents: list[Document],
    ) -> None:
        """Build from parsed parts; use :meth:`load` for files on disk."""
        self.path = path
        self.lead_table = lead_table
        self.pre_doc = pre_doc
        self.documents = documents

    # ── Serialisation ──────────────────────────────────────────────────────

    def to_bytes(self) -> bytes:
        """Serialize the whole file back to bytes (byte-identical if unmodified)."""
        w = _Writer()
        w.buf += FILE_MAGIC
        w.buf += struct.pack("<Q", len(self.lead_table))
        w.buf += struct.pack(f"<{len(self.lead_table)}I", *self.lead_table)
        w.buf += self.pre_doc
        for doc in self.documents:
            _write_document(w, doc)
        return bytes(w.buf)

    def save(self, backup: bool = True) -> None:
        """Write in place, optionally keeping a one-time ``.bak`` copy of the original."""
        if backup and self.path.exists():
            bak = self.path.with_suffix(self.path.suffix + ".bak")
            if not bak.exists():
                shutil.copy2(self.path, bak)
        self.path.write_bytes(self.to_bytes())

    # ── Factory ────────────────────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path) -> AsdFile:
        """Parse ``path`` fully; raises ValueError on structural surprises."""
        data = path.read_bytes()
        if data[:2] != FILE_MAGIC:
            raise ValueError(f"{path.name}: not an .asd file (magic {data[:2].hex()})")
        n_lead = int(struct.unpack_from("<Q", data, 2)[0])
        lead_table = list(struct.unpack_from(f"<{n_lead}I", data, 10))
        r = _Reader(data, 10 + 4 * n_lead)
        pre_doc = r.raw(len(PRE_DOC_BYTES))
        documents: list[Document] = []
        while r.pos < len(data):
            documents.append(_parse_document(r))
        return cls(path=path, lead_table=lead_table, pre_doc=pre_doc, documents=documents)

    # ── SampleData access ──────────────────────────────────────────────────

    def sample_data_doc(self) -> Document:
        """Return the document whose root is SampleData."""
        for doc in self.documents:
            if doc.root.cls == "SampleData":
                return doc
        raise ValueError(f"{self.path.name}: no SampleData document")

    def sample_data(self) -> Obj:
        """Return the root SampleData object."""
        return self.sample_data_doc().root

    @staticmethod
    def _wrapped(obj: Obj, field: str) -> Obj:
        inner = obj.values[field]
        assert isinstance(inner, Obj)
        return inner

    def _sd_scalar(self, field: str) -> Value:
        return self._wrapped(self.sample_data(), field).values["Value"]

    def _sd_scalar_int(self, field: str) -> int:
        v = self._sd_scalar(field)
        assert isinstance(v, int)
        return v

    def _set_sd_scalar(self, field: str, value: Value) -> None:
        self._wrapped(self.sample_data(), field).values["Value"] = value

    @property
    def is_warped(self) -> bool:
        """SampleData.IsWarped (warp on/off)."""
        return bool(self._sd_scalar_int("IsWarped"))

    @is_warped.setter
    def is_warped(self, value: bool) -> None:
        self._set_sd_scalar("IsWarped", int(value))

    @property
    def loop_on(self) -> bool:
        """SampleData.LoopOn."""
        return bool(self._sd_scalar_int("LoopOn"))

    @property
    def warp_mode(self) -> int:
        """SampleData.WarpMode (0 Beats … 3 Re-Pitch, per Live's .als enum)."""
        return self._sd_scalar_int("WarpMode")

    @property
    def original_file_size(self) -> int:
        """SampleData.OriginalFileSize — source audio size in bytes (stale detection)."""
        return self._sd_scalar_int("OriginalFileSize")

    @property
    def extra_length(self) -> int:
        """SampleData.ExtraLength (0 for WAV sources; 526 seen for MP3)."""
        return self._sd_scalar_int("ExtraLength")

    # ── Warp markers ───────────────────────────────────────────────────────

    @property
    def markers(self) -> list[WarpMarker]:
        """WarpMarkers as a list (element index becomes ``marker_id``)."""
        lst = self.sample_data().values["WarpMarkers"]
        assert isinstance(lst, ListValue)
        out: list[WarpMarker] = []
        for i, elem in enumerate(lst.elems):
            sec = elem.values["SecTime"]
            beat = elem.values["BeatTime"]
            assert isinstance(sec, float) and isinstance(beat, float)
            out.append(WarpMarker(i, sec, beat))
        return out

    @markers.setter
    def markers(self, new_markers: list[WarpMarker]) -> None:
        if new_markers:
            self._ensure_warp_marker_class(self.sample_data_doc())
        elems = [
            Obj("WarpMarker", {"SecTime": m.sec_time, "BeatTime": m.beat_time})
            for m in sorted(new_markers, key=lambda m: m.sec_time)
        ]
        self.sample_data().values["WarpMarkers"] = ListValue(elems)

    @staticmethod
    def _ensure_warp_marker_class(doc: Document) -> None:
        if any(cd.name == "WarpMarker" for cd in doc.schema):
            return
        insert_at = len(doc.schema)
        for i, cd in enumerate(doc.schema):
            if cd.name == "RemoteableList":
                insert_at = i + 1
                break
        doc.schema.insert(insert_at, ClassDef("WarpMarker", 2, list(WARP_MARKER_CLASS.fields)))

    # ── Grid authoring ─────────────────────────────────────────────────────

    def set_grid(
        self,
        *,
        bpm: float,
        anchor_seconds: float,
        duration_seconds: float | None = None,
        warp_mode: int | None = None,
        clear_tempo_analysis: bool = True,
    ) -> None:
        """Author a constant-tempo warp grid: beat 0 at ``anchor_seconds``, slope ``bpm``.

        Writes two warp markers (anchor -> beat 0, and track end — or one bar past the
        anchor when the duration is unknown — pinning the tempo), sets IsWarped and
        MarkersGenerated, and by default resets AufTaktData to the unset sentinel so
        Live cannot prefer its own tempo analysis over the explicit markers.

        Args:
            bpm: exact constant tempo of the track.
            anchor_seconds: audio time of the downbeat that becomes beat 0 (the drop).
            duration_seconds: track length; pins the second marker at the track end.
            warp_mode: optional Live warp-mode enum override (e.g. 3 = Re-Pitch).
            clear_tempo_analysis: reset AufTaktData so auto-warp cannot override the grid.
        """
        if bpm <= 0.0:
            raise ValueError(f"bpm must be positive, got {bpm}")
        if anchor_seconds < 0.0:
            raise ValueError(f"anchor_seconds must be >= 0, got {anchor_seconds}")
        if duration_seconds is not None and duration_seconds > anchor_seconds:
            second_sec = duration_seconds
        else:
            second_sec = anchor_seconds + 240.0 / bpm  # one 4/4 bar past the anchor
        self.markers = [
            WarpMarker(0, anchor_seconds, 0.0),
            WarpMarker(1, second_sec, (second_sec - anchor_seconds) * bpm / 60.0),
        ]
        self.is_warped = True
        self._set_sd_scalar("MarkersGenerated", 1)
        if warp_mode is not None:
            self._set_sd_scalar("WarpMode", warp_mode)
        if clear_tempo_analysis:
            self._clear_auf_takt_data()

    def _clear_auf_takt_data(self) -> None:
        sd = self.sample_data()
        if "AufTaktData" in sd.values:
            sd.values["AufTaktData"] = unset_auf_takt_data()
        for doc in self.documents:
            if doc.root.cls == "AufTaktData":
                doc.root = unset_auf_takt_data()

    # ── Legacy convenience ─────────────────────────────────────────────────

    def clean(self, first_beat_seconds: float) -> None:
        """Replace all markers with a single anchor at ``first_beat_seconds`` -> beat 0.

        Note: a single marker cannot encode a tempo; prefer :meth:`set_grid`.
        """
        self.markers = [WarpMarker(0, first_beat_seconds, 0.0)]


def unset_auf_takt_data() -> Obj:
    """AufTaktData in Live's 'not analyzed' sentinel state."""
    return Obj(
        "AufTaktData",
        {
            "PreprocessedDataChunk": PrimArray(0x31, b""),
            "UnbiasedTempoEstimate": UNSET_DOUBLE,
            "IsSet": 0,
            "Version": UNSET_INT,
        },
    )
