"""Xfer Records' plugins.

Serum's VST2 and VST3 save the same compressed patch, and the VST3 writes no
controller half. Heard in Live 12.4.5 on 2026-08-10, and Serum's own
``moduleinfo.json`` declares that its VST3 accepts VST2 state.
"""

from __future__ import annotations

import datetime

from abletoolz.plugins.container.controller import NO_CONTROLLER_STATE
from abletoolz.plugins.identity.ids import Vst2Id, Vst3Id
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.framings import VERBATIM
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import PluginFormat, PluginMap


@register
def serum() -> PluginMap:
    measured = MeasuredState(
        StateRung.VERBATIM, (StateEvidence.EAR, StateEvidence.DECLARED), datetime.date(2026, 8, 10)
    )
    return PluginMap(
        id="xfer.serum",
        name="Serum",
        version="1",
        scope="VST2 and VST3 save the same patch",
        vendor="Xfer Records",
        vst2=PluginFormat(Vst2Id(1483109208), "Serum", VERBATIM, measured, plan_name="serum_vst2"),
        vst3=PluginFormat(
            Vst3Id((1448301656, 1718835315, 1701999981, 0)),
            "Serum",
            VERBATIM,
            measured,
            NO_CONTROLLER_STATE,
            plan_name="serum_vst3",
        ),
    )
