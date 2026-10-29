"""KORG Legacy Cell: its bank, which synth sits in each slot, and the MS-20 program inside.

Legacy Cell is a 32-bit VST2 only (a jBridge wrapper saves it as ``LegacyCell.64``,
same UniqueId, same state). It hosts KORG's MS-20 and Polysix engines: a cell
holds up to two synths, A and B, each with two insert effects, then two master
effects and the cell's mixer. Live stores its whole bank: 128 cells and then the
working cell, the one the device plays.

    eight u32: 32, 0x201, 129 (the cells), 6, the selected cell, 0, two FFFFFFFF
    129 cells, each:
        u32 header length, then the header: the cell's name in 28 bytes and its
            knob, slider and key-zone settings, carried as they are
        12 records of a u32 16, a u32 version, a u32 length and a four-letter
        tag, then that many bytes; a length of 0 is an empty place:
            synth A, its two effects, synth B, its two effects (``VMLK`` an
            MS-20 program, ``VPLK`` a Polysix program, ``VFLK`` an effect
            whose first 24 bytes are its name)
            the two master effects
            ``1MLK``, ``2MLK``, ``3MLK`` (the mixer) and ``tlFM``

Every length, tag and place is checked, and the whole bank must be accounted
for. Read over every Legacy Cell bank in real sets, the layout covers each
byte. An MS-20 inside a cell is the standalone MS-20's own 2,048-byte program
(:class:`~abletoolz.plugins.state.korg_ms20.Ms20Program`): the standalone
plugin restores every working cell's MS-20 program and reads back what that
reader reads. The cell's effects and mixer are named, not decoded.

Legacy Cell itself was not hosted: it needs KORG's effect engine beside it, which
the machine holding the plugins lacks. Its layout is structural evidence.
"""

from __future__ import annotations

import dataclasses
import datetime
import struct
from collections.abc import Iterator

from abletoolz.plugins.identity.ids import Vst2Id
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.framings import VERBATIM
from abletoolz.plugins.state.korg_ms20 import PROGRAM_BYTES, Ms20Program
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import Layout, PluginFormat, PluginMap

LEGACY_CELL_VST2 = Vst2Id(0x4B4C4356)
LEGACY_CELL_SHA256 = "f430a31d9b5b4a467eceeb14e414f8f8357e5d0ecbdad2bc4c9302a098fc92dd"
CELLS = 128
_HEADER = struct.Struct("<8I")
_RECORD = struct.Struct("<III4s")
_NAME_BYTES = 28
SYNTHS = {b"VMLK": "MS-20", b"VPLK": "Polysix"}
_EFFECT = b"VFLK"
_MIXER = (b"1MLK", b"2MLK", b"3MLK", b"tlFM")


@dataclasses.dataclass(frozen=True, slots=True)
class Record:
    """One tagged record: where its data sits in the bank, its tag, or None for an empty place."""

    at: int
    tag: bytes | None
    data: bytes


@dataclasses.dataclass(frozen=True, slots=True)
class Slot:
    """A cell's synth slot: the synth's name, its MS-20 program when it is one, and its two effects."""

    synth: str | None
    ms20: Ms20Program | None
    ms20_at: int | None
    effects: tuple[str, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class Cell:
    """One Legacy Cell program."""

    name: str
    slots: tuple[Slot, Slot]
    master_effects: tuple[str, ...]

    @property
    def synths(self) -> tuple[str, ...]:
        return tuple(slot.synth for slot in self.slots if slot.synth is not None)


def _records(raw: bytes, at: int) -> Iterator[Record]:
    while True:
        if at + _RECORD.size > len(raw):
            raise StateTransformError(f"this Legacy Cell bank ends inside a record at byte {at}")
        size, _version, length, tag = _RECORD.unpack_from(raw, at)
        if size != _RECORD.size:
            raise StateTransformError(f"a Legacy Cell record at byte {at} does not open with 16")
        start = at + _RECORD.size
        if start + length > len(raw):
            raise StateTransformError(f"a Legacy Cell record at byte {at} runs past the bank")
        yield Record(start, tag if length else None, raw[start : start + length])
        at = start + length


def _effect(record: Record) -> str:
    if record.tag is None:
        return ""
    if record.tag != _EFFECT:
        raise StateTransformError(f"a Legacy Cell effect place holds {record.tag!r}")
    return record.data[:24].split(b"\0", 1)[0].decode("cp1252")


def _slot(synth: Record, first: Record, second: Record) -> Slot:
    if synth.tag is None:
        if first.tag is not None or second.tag is not None:
            raise StateTransformError("an empty Legacy Cell slot holds effects")
        return Slot(None, None, None, ())
    name = SYNTHS.get(synth.tag)
    if name is None:
        raise StateTransformError(f"a Legacy Cell synth place holds {synth.tag!r}")
    effects = (_effect(first), _effect(second))
    if name != "MS-20":
        return Slot(name, None, None, effects)
    if len(synth.data) != PROGRAM_BYTES:
        raise StateTransformError("a Legacy Cell MS-20 record is not one 2,048-byte program")
    return Slot(name, Ms20Program.parse(synth.data), synth.at, effects)


def _cell(raw: bytes, at: int, *, read_programs: bool) -> tuple[Cell | None, int]:
    if at + 4 > len(raw):
        raise StateTransformError(f"this Legacy Cell bank ends before cell header at byte {at}")
    (length,) = struct.unpack_from("<I", raw, at)
    if not 4 + _NAME_BYTES <= length <= 0x1000:
        raise StateTransformError(f"a Legacy Cell header at byte {at} is {length} bytes long")
    name = raw[at + 4 : at + 4 + _NAME_BYTES].split(b"\0", 1)[0].decode("cp1252")
    records = _records(raw, at + length)
    places = [next(records) for _ in range(12)]
    if [place.tag for place in places[8:]] != list(_MIXER):
        raise StateTransformError(f"Legacy Cell {name!r} does not close with its mixer records")
    end = places[-1].at + len(places[-1].data)
    if not read_programs:
        for place in places[:8]:
            if place.tag is not None and place.tag not in (*SYNTHS, _EFFECT):
                raise StateTransformError(f"Legacy Cell {name!r} holds a {place.tag!r} record")
        return None, end
    slots = (_slot(*places[0:3]), _slot(*places[3:6]))
    return Cell(name, slots, (_effect(places[6]), _effect(places[7]))), end


@dataclasses.dataclass(frozen=True, slots=True)
class LegacyCellBank:
    """A Legacy Cell bank as Live stores it, every byte kept, with its working cell read."""

    raw: bytes
    selected: int
    working: Cell

    @classmethod
    def parse(cls, raw: bytes) -> LegacyCellBank:
        if len(raw) < _HEADER.size:
            raise StateTransformError("a Legacy Cell bank is shorter than its header")
        size, version, count, _six, selected, zero, *ends = _HEADER.unpack_from(raw)
        if (size, version, count, zero, ends) != (_HEADER.size, 0x201, CELLS + 1, 0, [0xFFFFFFFF] * 2):
            raise StateTransformError("this Legacy Cell bank's header is not the measured one")
        if selected >= CELLS:
            raise StateTransformError(f"this Legacy Cell bank selects cell {selected} of {CELLS}")
        at = _HEADER.size
        for _ in range(CELLS):
            _, at = _cell(raw, at, read_programs=False)
        working, at = _cell(raw, at, read_programs=True)
        if at != len(raw) or working is None:
            raise StateTransformError(f"this Legacy Cell bank holds {len(raw) - at} bytes after its cells")
        return cls(raw, selected, working)

    def with_ms20(self, slot: int, program: Ms20Program) -> LegacyCellBank:
        """This bank with the working cell's MS-20 in ``slot`` (0 for A, 1 for B) replaced by ``program``."""
        at = self.working.slots[slot].ms20_at
        if at is None:
            raise StateTransformError(f"the working cell holds no MS-20 in slot {'AB'[slot]}")
        return LegacyCellBank.parse(self.raw[:at] + program.raw + self.raw[at + PROGRAM_BYTES :])

    def encode(self) -> bytes:
        return self.raw


def _is_bank(patch: bytes) -> bool:
    try:
        LegacyCellBank.parse(patch)
    except StateTransformError:
        return False
    return True


@register
def korg_legacy_cell() -> PluginMap:
    measured = MeasuredState(StateRung.VERBATIM, (StateEvidence.STRUCTURAL,), datetime.date(2026, 10, 29))
    return PluginMap(
        id="korg.legacy-cell",
        name="LegacyCell",
        version="1",
        scope="VST2 only; the 129-cell bank, its working cell's synths read and its MS-20 programs decoded",
        vendor="KORG",
        vst2=PluginFormat(LEGACY_CELL_VST2, "LegacyCell", VERBATIM, measured, plan_name="legacy_cell_vst2"),
        layouts=(Layout("Legacy Cell bank", _is_bank),),
        binary_sha256=(LEGACY_CELL_SHA256,),
    )
