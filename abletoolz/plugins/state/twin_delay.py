"""Twin Delay, 32-bit Delphi freeware, read from what its devices saved.

It exposes no chunk, so Live stores its parameter bank (``FxBk``): 66 programs
of a 28-byte name (cut, not terminated, when full) and 20 little-endian 0..1
floats.
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

TWIN_DELAY_BANK = ProgramBank(programs=66, record=108, name=28, controls=20)


@register
def twin_delay() -> PluginMap:
    return PluginMap(
        id="twin-delay.twin-delay",
        name="Twin Delay",
        version=None,
        scope="Live's FxBk parameter bank of 66 programs",
        vendor="unknown",
        vst2=PluginFormat(Vst2Id(0x4F534432), "Twin Delay", VERBATIM, _STRUCTURAL, plan_name="twin_delay_vst2"),
        layouts=(TWIN_DELAY_BANK.layout(),),
        binary_sha256=("77ba17dd42c957e20c9f44cfcc9f5c8cd47a9a258c639000de9603b1dcc4f45d",),
    )
