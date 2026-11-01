"""Camel Audio CamelPhat 3, read from what its devices saved.

One unique id covers the 32-bit build, its jBridge wrapper and a later build.
The chunk is the whole bank, 64 programs of a float32 3.0, a NUL-terminated
name, a u32 53 and 53 little-endian 0..1 floats. The later build ends every
program with two more NUL-terminated strings, ``<compatibility>`` and
``useOldBitCrush 0`` or ``1``.
"""

from __future__ import annotations

import dataclasses
import datetime
import struct

from abletoolz.plugins.identity.ids import Vst2Id
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.framings import VERBATIM
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import Layout, PluginFormat, PluginMap

# The format is only read here, by the shape of its bytes.
_STRUCTURAL = MeasuredState(StateRung.VERBATIM, (StateEvidence.STRUCTURAL,), datetime.date(2026, 10, 31))

CAMELPHAT_PROGRAMS = 64
CAMELPHAT_CONTROLS = 53
CAMELPHAT_BIT_CRUSHER = 8  # DistBitCrusher, the control the later build's flag is about
_CAMELPHAT_RECORD = struct.Struct("<f")
_CAMELPHAT_COUNT = struct.Struct("<I")
_COMPATIBILITY = b"<compatibility>\0"
_BIT_CRUSH_FLAGS = {b"useOldBitCrush 1": True, b"useOldBitCrush 0": False}


@dataclasses.dataclass(frozen=True, slots=True)
class CamelPhatProgram:
    """One program of CamelPhat 3's bank; ``old_bit_crush`` is None in a bank that predates the flag."""

    name: bytes
    values: tuple[float, ...]
    old_bit_crush: bool | None


def camelphat_programs(bank: bytes) -> tuple[CamelPhatProgram, ...]:
    """CamelPhat 3's chunk as its 64 programs; refused unless ``bank`` is exactly that, by either build."""
    programs: list[CamelPhatProgram] = []
    at = 0
    while at < len(bank) and len(programs) < CAMELPHAT_PROGRAMS:
        name_end = bank.find(b"\0", at + _CAMELPHAT_RECORD.size)
        values_at = name_end + 1 + _CAMELPHAT_COUNT.size
        if name_end < 0 or values_at + 4 * CAMELPHAT_CONTROLS > len(bank):
            break
        (version,) = _CAMELPHAT_RECORD.unpack_from(bank, at)
        (count,) = _CAMELPHAT_COUNT.unpack_from(bank, name_end + 1)
        if version != 3.0 or count != CAMELPHAT_CONTROLS:
            break
        name = bank[at + _CAMELPHAT_RECORD.size : name_end]
        values = struct.unpack_from(f"<{count}f", bank, values_at)
        at = values_at + 4 * count
        flag = None
        if bank.startswith(_COMPATIBILITY, at):
            flag_end = bank.find(b"\0", at + len(_COMPATIBILITY))
            flag = _BIT_CRUSH_FLAGS.get(bank[at + len(_COMPATIBILITY) : flag_end]) if flag_end >= 0 else None
            if flag is None:
                break
            at = flag_end + 1
        programs.append(CamelPhatProgram(name, values, flag))
    if len(programs) != CAMELPHAT_PROGRAMS or at != len(bank):
        raise StateTransformError(f"Not a CamelPhat 3 bank of {CAMELPHAT_PROGRAMS} programs of 53 controls")
    return tuple(programs)


def _is_camelphat_bank(patch: bytes) -> bool:
    try:
        camelphat_programs(patch)
    except StateTransformError:
        return False
    return True


@register
def camelphat_3() -> PluginMap:
    return PluginMap(
        id="camel-audio.camelphat-3",
        name="CamelPhat 3",
        version="3",
        scope="VST2 chunk: the whole bank of 64 programs",
        vendor="Camel Audio",
        vst2=PluginFormat(Vst2Id(0x43615033), "CamelPhat3", VERBATIM, _STRUCTURAL, plan_name="camelphat_3_vst2"),
        layouts=(Layout("bank of 64 programs of 53 controls", _is_camelphat_bank),),
        binary_sha256=("8c9bebf9b0e8abf4e50b57a8d84e668a6e77acf735f0c7e32e4348959c46e5d6",),
    )
