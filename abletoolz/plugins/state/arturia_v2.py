"""Arturia's V2 instruments that save their engine's record: their VST2 chunk, read by the names their V3s store.

Each chunk is the instrument's patch record as it sits in memory: little-endian
32-bit words, nearly all float32, then the preset's path and name. The V3 of
each keeps the same record in its processor, a few words longer, and stores
each control under a name in its archive. ``data/<plugin>.json`` places every
name at its word in the old chunk. A word is the stored 0 to 1 itself unless
the entry says ``steps`` ``n``, ``c``, a choice of ``n`` items held as a whole
number, item ``floor(n * stored)`` less ``c`` (a patch cord's source, -1 for
none), or ``int32``, a word held as an integer that the archive stores as that
number. A choice the V3 keeps in its record as the step it lands on, the old
record keeps as the knob's 0 to 1: Arturia's own ports of Modular V2 presets
store the old word unchanged, on every such choice in them.

The join was measured, not read from the old plugins, which are not here: the
installed V3 was hosted, every name it stores was set in turn, and the word it
wrote in the record in memory was read back. Where the V3 record is longer, the
old chunk's layout is the V3's with those words taken out, placed by the
words a real old chunk holds on either side of them. A name the V3 writes into
several words, as Modular V3's oscillator drivers write into each oscillator
they drive, is read from the first; the old chunk holds the same value in each.
A word in the old chunk that no V3 name writes is not read.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import math
import pathlib
import struct

from abletoolz.plugins.identity.ids import Vst2Id
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.framings import VERBATIM
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import Layout, PluginFormat, PluginMap

DATA = pathlib.Path(__file__).with_name("data")


@dataclasses.dataclass(frozen=True, slots=True)
class Control:
    """Where one V3 name sits in the old chunk, and how its word becomes the V3's stored value."""

    offset: int
    integer: bool = False
    steps: tuple[int, int] | None = None

    def word(self, chunk: bytes) -> float:
        value: float = struct.unpack_from("<i" if self.integer else "<f", chunk, self.offset)[0]
        return value

    def stored(self, word: float) -> tuple[float, bool]:
        """The stored value for ``word``, and whether it is exact; outside the range, its end."""
        if self.integer:
            return word, True
        if self.steps:
            items, below = self.steps
            index = min(max(round(word) + below, 0), items - 1)
            return index / (items - 1), index == word + below
        return min(max(word, 0.0), 1.0), 0.0 <= word <= 1.0


@dataclasses.dataclass(frozen=True, slots=True)
class Instrument:
    """One of the three old plugins: its map id, name, UniqueId, chunk size, first word and product tag."""

    map_id: str
    name: str
    unique_id: int
    chunk_bytes: int
    version: float
    tag: bytes

    @property
    def target(self) -> str:
        return self.map_id.removesuffix("v2") + "v3"

    @property
    def target_name(self) -> str:
        return self.name.removesuffix("V2") + "V3"

    def check(self, chunk: bytes) -> None:
        if len(chunk) != self.chunk_bytes:
            raise StateTransformError(f"a {self.name} chunk is {self.chunk_bytes} bytes, not {len(chunk)}")
        if chunk[:4] != struct.pack("<f", self.version):
            raise StateTransformError(f"a {self.name} chunk opens with {self.version}, and this one does not")
        if self.tag and chunk[4 : 4 + len(self.tag) + 1] != self.tag + b"\0":
            raise StateTransformError(f"a {self.name} chunk names itself {self.tag.decode()}, and this one does not")

    def matches(self, chunk: bytes) -> bool:
        try:
            self.check(chunk)
        except StateTransformError:
            return False
        return True


ARP_2600_V2 = Instrument("arturia.arp-2600-v2", "ARP 2600 V2", 1095913522, 5276, 1.0, b"Arp2600v2 V2.0")
PROPHET_V2 = Instrument("arturia.prophet-v2", "Prophet V2", 1345672754, 1060, 1.0, b"Prophet-V2 V2.0")
MODULAR_V2 = Instrument("arturia.modular-v2", "Modular V2", 5059151, 3392, 2.0, b"")
INSTRUMENTS = (ARP_2600_V2, MODULAR_V2, PROPHET_V2)


@functools.cache
def controls(instrument: Instrument) -> dict[str, Control]:
    """Every V3 name the old chunk holds, by name."""
    data = json.loads((DATA / f"{instrument.map_id.removeprefix('arturia.')}.json").read_text(encoding="utf-8"))
    out = {}
    for name, (offset, *how) in data["controls"].items():
        if how == ["int32"]:
            out[name] = Control(offset, integer=True)
        elif how and how[0] == "steps":
            out[name] = Control(offset, steps=(int(how[1]), int(how[2])))
        else:
            out[name] = Control(offset)
    return out


def words(chunk: bytes, instrument: Instrument) -> dict[str, float]:
    """Every mapped word of an old chunk, by the V3 name that writes it."""
    instrument.check(chunk)
    out = {}
    for name, control in controls(instrument).items():
        value = control.word(chunk)
        if not math.isfinite(value):
            raise StateTransformError(f"{name} holds {value}, not a value the plugin writes")
        out[name] = value
    return out


def _map(instrument: Instrument) -> PluginMap:
    measured = MeasuredState(StateRung.VERBATIM, (StateEvidence.STRUCTURAL,))
    return PluginMap(
        id=instrument.map_id,
        name=instrument.name,
        version="2",
        scope=f"64-bit VST2; a {instrument.chunk_bytes} byte patch record of 32-bit words",
        vendor="Arturia",
        vst2=PluginFormat(
            Vst2Id(instrument.unique_id),
            instrument.name,
            VERBATIM,
            measured,
            plan_name=instrument.map_id.removeprefix("arturia.").replace("-", "_") + "_vst2",
        ),
        layouts=(Layout(f"{instrument.chunk_bytes} byte record", instrument.matches),),
    )


@register
def arp_2600_v2() -> PluginMap:
    return _map(ARP_2600_V2)


@register
def prophet_v2() -> PluginMap:
    return _map(PROPHET_V2)


@register
def modular_v2() -> PluginMap:
    return _map(MODULAR_V2)
