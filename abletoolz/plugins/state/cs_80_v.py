"""Arturia CS-80V and CS-80 V2: their VST2 chunk, read by the names CS-80 V3 stores.

Four VST2s: CS-80V and CS-80V (10 voices), the first generation, and CS-80 V2
and CS-80 V2 10 voices. Each chunk is
little-endian float32 words with no header: eight patches of 568 bytes, then
the voices, keyboard zones, effects and performance controls, a 64-byte
preset path at byte 5192, and in the second generation one more word, the
editor's selection, which makes 5,396 bytes where the first generation has
5,392.

``data/cs-80-v.json`` places each control CS-80 V3 stores at its byte in the
chunk: 106 per patch (given for the first patch; patch ``n`` is ``568 n``
bytes further on) and 154 more. It was read off CS-80 V2 2.7.0.76's native
getter and setter: the 549 public parameters at the offsets the getter reads,
and the stored internal values joined to CS-80 V3's processor by the bytes
both read: 1,002 of the 1,004 controls CS-80 V3 archives, all but its MIDI
channel and its second name for the mod wheel. A value is stored as the plugin uses it:
a knob or switch from 0 to 1, an item index for choices (a modulation input 0
to 14, an output 0 to 38, a patch 0 to 7, a zone 0 to 4), a MIDI note for a
zone's ends; the range beside a name is the one CS-80 V3 stores it over.
Hosting the original V2 DLLs confirms the public half: every public parameter
reads back from a chunk as this map reads it, and a switch reads as on from
0.5.

The first generation's binaries are not here. Its chunks hold the same preset
paths at the same byte, and Arturia's own CS-80 V3 ports of its factory
patches carry the same values at the same places as they do for the second
generation's (see the converter); that is the evidence its layout rests on.
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
PITCH_BEND_OFFSET = 4832
EXPRESSION_OFFSET = 4840
PRESET_PATH = slice(5192, 5256)


@dataclasses.dataclass(frozen=True, slots=True)
class Generation:
    """One of the four plugins: its map id, name, UniqueId and chunk size."""

    map_id: str
    name: str
    unique_id: int
    chunk_bytes: int

    @property
    def first(self) -> bool:
        return self.chunk_bytes == 5392


CS_80V = Generation("arturia.cs-80v", "CS-80V", 4412246, 5392)
CS_80V_10 = Generation("arturia.cs-80v-10-voices", "CS-80V (10 voices)", 4412237, 5392)
CS_80_V2 = Generation("arturia.cs-80-v2", "CS-80 V2", 1129535026, 5396)
CS_80_V2_10 = Generation("arturia.cs-80-v2-10-voices", "CS-80 V2 10 voices", 1129532722, 5396)
GENERATIONS = (CS_80V, CS_80V_10, CS_80_V2, CS_80_V2_10)


@functools.cache
def fields() -> dict[str, tuple[int, float, float]]:
    """Every control by the name CS-80 V3 stores: its byte in the chunk and the range CS-80 V3 stores it over."""
    data = json.loads((DATA / "cs-80-v.json").read_text(encoding="utf-8"))
    out: dict[str, tuple[int, float, float]] = {}
    for patch in range(data["patches"]):
        for name, offset, *span in data["patch"]:
            low, high = span or (0, 1)
            out[f"Patch{patch + 1}_{name}"] = (offset + patch * data["patch_bytes"], low, high)
    for name, offset, *span in data["global"]:
        low, high = span or (0, 1)
        out[name] = (offset, low, high)
    return out


def _word(chunk: bytes, offset: int) -> float:
    value: float = struct.unpack_from("<f", chunk, offset)[0]
    if not math.isfinite(value):
        raise StateTransformError(f"the word at byte {offset} holds {value}, not a value the plugin writes")
    return value


def stored(chunk: bytes, generation: Generation) -> dict[str, float]:
    """Every mapped control as the chunk stores it: 0 to 1, an item index or a note."""
    if len(chunk) != generation.chunk_bytes:
        raise StateTransformError(f"a {generation.name} chunk is {generation.chunk_bytes} bytes, not {len(chunk)}")
    if b"\0" not in chunk[PRESET_PATH]:
        raise StateTransformError(f"this {generation.name} chunk's preset path runs past its 64 bytes")
    return {name: _word(chunk, offset) for name, (offset, _, _) in fields().items()}


def performance(chunk: bytes) -> tuple[float, float]:
    """The saved pitch bend (centre 0.5) and expression pedal (0 to 1)."""
    return _word(chunk, PITCH_BEND_OFFSET), _word(chunk, EXPRESSION_OFFSET)


def _map(generation: Generation) -> PluginMap:
    measured = MeasuredState(StateRung.VERBATIM, (StateEvidence.STRUCTURAL,))
    first = "first generation; layout from Arturia's ports" if generation.first else "2.7.0.76, native getter"
    plan_name = generation.map_id.removeprefix("arturia.").replace("-", "_") + "_vst2"
    return PluginMap(
        id=generation.map_id,
        name=generation.name,
        version="1" if generation.first else "2",
        scope=f"64-bit VST2; a {generation.chunk_bytes} byte chunk of float32 words; {first}",
        vendor="Arturia",
        vst2=PluginFormat(Vst2Id(generation.unique_id), generation.name, VERBATIM, measured, plan_name=plan_name),
        layouts=(Layout(f"{generation.chunk_bytes} byte chunk", lambda chunk: len(chunk) == generation.chunk_bytes),),
    )


@register
def cs_80v() -> PluginMap:
    return _map(CS_80V)


@register
def cs_80v_10_voices() -> PluginMap:
    return _map(CS_80V_10)


@register
def cs_80_v2() -> PluginMap:
    return _map(CS_80_V2)


@register
def cs_80_v2_10_voices() -> PluginMap:
    return _map(CS_80_V2_10)
