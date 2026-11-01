"""Ohm Force Ohmicide, the 32-bit VST2: its saved chunk, read as the plugin's 100 controls.

Its chunk is five tagged records, each a 4-byte tag and a big-endian u32 length:

    PIID   4 bytes: ``PDT2``, the product
    Sett   524 bytes: the working settings, its 100 controls as big-endian
           float32 from byte 76 of its payload (byte 96 of the chunk)
    Bank   6,296 bytes: the plugin's own program bank
    Melo   30 bytes
    Misc   31 bytes

Each control is stored as the 0 to 1 the plugin's own setter and getter take,
so a value here is what the plugin reads back. That was measured by hosting the
x86 plugin headless, without an editor, and restoring two full vectors of
values written here: every getter and display read back as predicted. Only the
working settings are read; the bank and the two short records are not.

Live saves the ``Melo`` record with a byte the plugin's own fresh state holds
at 0 (byte 12 reads 2 in most saved chunks); rendering the same patch with
either byte gave the same audio, so it is not read.
"""

from __future__ import annotations

import datetime
import struct

from abletoolz.plugins.identity.ids import Vst2Id
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.framings import VERBATIM
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import Layout, PluginFormat, PluginMap

# ``Omic``; the jBridged copy of the plugin stores the same id.
OHMICIDE_VST2 = Vst2Id(1332570467)
OHMICIDE_SHA256 = "774e6f3c11bc77ac6c1f164fc273188799f5c685096815282e02828f5c75a80f"
PRODUCT = "Ohmicide"
RECORDS = ((b"PIID", 4), (b"Sett", 524), (b"Bank", 6296), (b"Melo", 30), (b"Misc", 31))
CHUNK_BYTES = sum(8 + size for _, size in RECORDS)
_CONTROLS_AT = 96
_HEADER = struct.Struct(">4sI")

_FEEDBACK = ("Fdbk Amnt", "Fdbk Freq", "Fdbk Spread")
_BAND = (
    "Shape",
    "Body",
    "Disto Gain",
    "Disto Type",
    "Disto Bias",
    "Disto Alteration",
    "Gate Threshold",
    "Gate Atk Time",
    "Gate Rls Time",
    "Gate Amount",
    "Pan",
    "Volume",
    "Dry/Wet mix",
    "Mid Side mode",
    "Solo",
    "Mute",
)
# The names the plugin gives its controls, in their order in the chunk.
CONTROLS: tuple[str, ...] = (
    "Tempo",
    "Bypass",
    "Dry/Wet mix",
    "Master Vol",
    "Input trim",
    "Stereo link",
    "Tone Freq",
    "Tone Shape",
    "Pre Disto",
    *("Reserved",) * 4,
    *(f"B{band} Upper Freq" for band in (1, 2, 3)),
    *(name for band in (1, 2, 3, 4) for name in (*(f"B{band} {c}" for c in _FEEDBACK), "Reserved")),
    *(name for band in (1, 2, 3, 4) for name in (*(f"B{band} {c}" for c in _BAND), "Reserved")),
)
CONTROL_COUNT = len(CONTROLS)


def is_chunk(patch: bytes) -> bool:
    """Whether ``patch`` has the five records an Ohmicide chunk holds, in their order and sizes."""
    if len(patch) != CHUNK_BYTES or patch[8:12] != b"PDT2":
        return False
    at = 0
    for tag, size in RECORDS:
        if _HEADER.unpack_from(patch, at) != (tag, size):
            return False
        at += 8 + size
    return True


def controls(patch: bytes) -> dict[str, float]:
    """Every named control's stored 0 to 1 in an Ohmicide chunk; the reserved slots are left out."""
    if not is_chunk(patch):
        raise StateTransformError(f"not an {PRODUCT} chunk: the records or their sizes differ")
    values = struct.unpack_from(f">{CONTROL_COUNT}f", patch, _CONTROLS_AT)
    out = {name: value for name, value in zip(CONTROLS, values, strict=True) if name != "Reserved"}
    if any(not 0 <= value <= 1 for value in out.values()):
        raise StateTransformError(f"this {PRODUCT} chunk holds a control outside 0 to 1")
    return out


@register
def ohmicide() -> PluginMap:
    measured = MeasuredState(StateRung.VERBATIM, (StateEvidence.HOSTED,), datetime.date(2026, 10, 31))
    return PluginMap(
        id="ohm-force.ohmicide",
        name=PRODUCT,
        version="1",
        scope="The x86 VST2 only; its five-record chunk, the working settings read",
        vendor="Ohm Force",
        vst2=PluginFormat(OHMICIDE_VST2, "Ohmicide_vstwin", VERBATIM, measured, plan_name="ohmicide_vst2"),
        layouts=(Layout("five tagged records", is_chunk),),
        binary_sha256=(OHMICIDE_SHA256,),
    )
