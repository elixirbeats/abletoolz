"""Goodhertz's plugins.

Ghz Tupe 3's VST2 and VST3 save the same patch, and the VST3 writes no
controller half. Heard in Live 12.4.5 on 2026-08-10.
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
def tupe_3() -> PluginMap:
    measured = MeasuredState(StateRung.VERBATIM, (StateEvidence.EAR,), datetime.date(2026, 8, 10))
    return PluginMap(
        id="goodhertz.tupe-3",
        name="Ghz Tupe 3",
        version=None,
        scope="VST2 and VST3 save the same patch",
        vendor="Goodhertz",
        vst2=PluginFormat(Vst2Id(1414542168), "Ghz Tupe 3", VERBATIM, measured),
        vst3=PluginFormat(
            Vst3Id((1448301652, 1345542247, 1752834164, 1970300192)),
            "Ghz Tupe 3",
            VERBATIM,
            measured,
            NO_CONTROLLER_STATE,
        ),
    )
