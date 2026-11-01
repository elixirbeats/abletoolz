"""Sonimus Satson Channel, read from what its devices saved.

It exposes no chunk, so Live stores its parameter bank (``FxBk``): 6 programs
of a 28-byte name and 10 little-endian 0..1 floats.
"""

from __future__ import annotations

import datetime

from abletoolz.plugins.identity.ids import Vst2Id
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.framings import VERBATIM
from abletoolz.plugins.state.fxbk import ProgramBank
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import PluginFormat, PluginMap

# The format is only read here, by the shape of its bytes.
_STRUCTURAL = MeasuredState(StateRung.VERBATIM, (StateEvidence.STRUCTURAL,), datetime.date(2026, 10, 31))

SATSON_BANK = ProgramBank(programs=6, record=68, name=28, controls=10)


@register
def satson_channel() -> PluginMap:
    # Every Satson device in the library, 32-bit, jBridged and x64, saves this bank; its ten controls are Gain,
    # HP, HP 12 dB/oct, LP, FAT, ON, Auto Out, S Gain, MonoL and VU14. The x64 build on hand (sha256 b5975c12...)
    # is a later version with its own ``tffp`` chunk, which does not read these banks.
    return PluginMap(
        id="sonimus.satson-channel",
        name="Satson Channel",
        version="1",
        scope="Live's FxBk parameter bank of 6 programs",
        vendor="Sonimus",
        vst2=PluginFormat(Vst2Id(1398883121), "SatsonChannel", VERBATIM, _STRUCTURAL, plan_name="satson_channel_vst2"),
        layouts=(SATSON_BANK.layout(),),
    )
