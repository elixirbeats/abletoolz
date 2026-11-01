"""Analog Lab 3 and 4 presets into Analog Lab V, which converts their synth patches itself.

Analog Lab V loads an Analog Lab 3 or 4 preset and converts the synth patch
inside it to its current engines on its own; nothing here touches a control.
Two things in the preset's record decide whether that conversion holds:

* **the instrument id** -- left at Analog Lab 4's 1, Analog Lab V converts the
  patch again every time it loads one it saved, so a set saved and reopened
  drifts. Written as Analog Lab V's 60, the state it saves loads back to
  itself;
* **the part entries** -- Analog Lab 4.2's VST3 names the synth part in the
  preset's map of named strings (``InstrumentPart1`` and its variation) where
  its VST2 does not. Given them, Analog Lab V keeps the preset's macros as they
  are and converts them on the next load instead, so a set reopened plays other
  macro settings. Those two entries are left out, the way the VST2 writes the
  record; every other entry stays, because the synth patch's own entries decide
  how its controls convert (without them a CS-80 V3 patch is converted as an
  older CS-80's).

Everything else stays byte for byte as the source saved it. Measured by loading
each converted preset into the installed Analog Lab V (VST2 and VST3), saving,
and loading what it saved: the second save is the first, but for a synth that
moves on its own (a free-running LFO, a random register) and Modular V3, whose
every save adds a few default controls for a filter input. Analog Lab V's
first save also matches Arturia's current factory file of the same preset
control for control where the source had not been edited. Not heard in Live.
"""

from __future__ import annotations

import dataclasses
import datetime
import re

from abletoolz.formats.arturia_archive import TwoArchives
from abletoolz.plugins.state import analog_lab
from abletoolz.plugins.state.catalog import Converted, converter
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung

HEADER = b"22 serialization::archive 10 0 7 0 7 "
SOURCE_ID, TARGET_ID = 1, 60
SOURCE_MAJORS = ("3", "4")
PART_ENTRIES = (b"InstrumentPart1", b"InstrumentPart1VariationIdx")
_INTEGER = re.compile(rb"-?[0-9]{1,19}")
_SPACE = b" "

NOTE = (
    "Analog Lab V converts this Analog Lab 3/4 preset's synth itself when it loads it; abletoolz only marks the preset "
    "as Analog Lab V's. Loaded and saved back by the installed Analog Lab V. Not heard in Live: audition this device."
)


@dataclasses.dataclass(slots=True)
class _Reader:
    """Boost text archive tokens: integers, and strings written as their byte length, a space and the bytes."""

    data: bytes
    at: int

    def integer(self) -> tuple[int, int, int]:
        """The next integer and where its token starts and ends."""
        while self.at < len(self.data) and self.data[self.at] in b" \t\r\n":
            self.at += 1
        start = self.at
        while self.at < len(self.data) and self.data[self.at] not in b" \t\r\n":
            self.at += 1
        if not _INTEGER.fullmatch(self.data[start : self.at]):
            raise StateTransformError(f"expected a number at byte {start} of the preset's record")
        return int(self.data[start : self.at]), start, self.at

    def string(self) -> bytes:
        size, _, end = self.integer()
        if size < 0 or self.data[end : end + 1] != _SPACE or end + 1 + size > len(self.data):
            raise StateTransformError(f"a string at byte {end} of the preset's record runs past its archive")
        self.at = end + 1 + size
        return self.data[end + 1 : self.at]

    def skip(self, count: int) -> None:
        for _ in range(count):
            self.integer()


def _record(reader: _Reader, *, outer: bool) -> tuple[int, int, int, bytes]:
    """Read one preset record up to its map: its instrument id, that id's token span, and its version."""
    reader.string()  # name
    reader.string()  # bank
    instrument, id_start, id_end = reader.integer()
    reader.string()  # author
    reader.string()  # type
    if outer:
        reader.skip(2)  # the tag vector's class record, written once per archive
    count, _, _ = reader.integer()
    reader.integer()
    for _ in range(count):
        reader.string()
    reader.skip(2)  # two flags
    reader.string()  # comment
    reader.integer()  # time
    version = reader.string()
    reader.skip(7)
    reader.string()
    if outer:
        reader.skip(2)
    return instrument, id_start, id_end, version


def _map(reader: _Reader, *, class_written: bool) -> tuple[int, int, list[tuple[bytes, bytes]], bool]:
    """Read a map of named strings: where it starts and ends, its entries, and whether it wrote its class record."""
    count, start, _ = reader.integer()
    reader.integer()
    wrote_class = bool(count) and not class_written
    if wrote_class:
        reader.skip(2)
    entries = [(reader.string(), reader.string()) for _ in range(count)]
    return start, reader.at, entries, wrote_class


def _spell(entries: list[tuple[bytes, bytes]], *, with_class: bool) -> bytes:
    """A map of named strings as the archive writes it, with the pair's class record when it is the first."""
    if not entries:
        return b"0 0"
    head = [str(len(entries)).encode(), b"0"] + ([b"0", b"0"] if with_class else [])
    return b" ".join(head + [b"%d %s" % (len(text), text) for entry in entries for text in entry])


def as_analog_lab_v(archive: bytes) -> bytes:
    """One Analog Lab 3/4 archive marked as Analog Lab V's, without the part entries Analog Lab 4.2's VST3 adds."""
    if not archive.startswith(HEADER):
        raise StateTransformError("not an Analog Lab 3 or 4 archive: it does not open with its Boost header")
    reader = _Reader(archive, len(HEADER))
    instrument, id_start, id_end, version = _record(reader, outer=True)
    if instrument != SOURCE_ID or version.split(b".")[0] not in (major.encode() for major in SOURCE_MAJORS):
        raise StateTransformError(
            f"this preset was saved by instrument {instrument}, version {version.decode('ascii', 'replace')!r}; "
            "only Analog Lab 3 and 4 presets (instrument 1, version 3 or 4) convert"
        )
    root_start, root_end, root, root_wrote_class = _map(reader, class_written=False)
    reader.skip(4)
    _record(reader, outer=False)
    child_start, child_end, child, _ = _map(reader, class_written=root_wrote_class)
    kept = [entry for entry in root if entry[0] not in PART_ENTRIES]
    edits = [(id_start, id_end, str(TARGET_ID).encode())]
    if kept != root:
        edits.append((root_start, root_end, _spell(kept, with_class=bool(kept))))
        if child and not kept:  # the pair's class record moves to the first map that still has an entry
            edits.append((child_start, child_end, _spell(child, with_class=True)))
    out = archive
    for start, end, text in sorted(edits, reverse=True):
        out = out[:start] + text + out[end:]
    return out


@converter(
    "arturia.analog-lab-4",
    "arturia.analog-lab-v",
    MeasuredState(StateRung.VENDOR_COMPAT, (StateEvidence.HOSTED,), datetime.date(2026, 10, 31)),
)
def convert(patch: bytes, program: int | None = None) -> Converted:
    """An Analog Lab 3/4 VST2 state as Analog Lab V's. ``program`` is not used: the state holds one preset."""
    first, second = (as_analog_lab_v(archive) for archive in analog_lab.archives(patch, "Analog Lab 3 or 4"))
    return Converted(TwoArchives.join(first, second), (NOTE,))
