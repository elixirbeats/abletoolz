"""u-he Podolski: its VST2 patch is u-he's preset text, and its VST3 holds the same text after a byte count.

The VST3 saves that state as its controller half too, byte for byte. Measured 2026-11-02 with the VST3 build
installed here (``Podolski(x64).vst3``) in a headless host: it loads every Podolski VST2 patch found in the library
and saves it back stable, every sound setting kept. What changes is the same in every patch: keys this build no
longer writes (the old arpeggiator song position and swing, voice slots, ``OSC1.Wave``, ``LFO1.FMS1``), its own
channel layout, surround and microtuning settings, and the revision stamp.
"""

from __future__ import annotations

import datetime

from abletoolz.plugins.container.controller import SameAsProcessor
from abletoolz.plugins.identity.ids import Vst2Id, Vst3Id
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.framings import LENGTH_PREFIXED, VERBATIM
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import Layout, PluginFormat, PluginMap

_HOSTED = MeasuredState(StateRung.REFRAME, (StateEvidence.HOSTED,), datetime.date(2026, 11, 2))


@register
def podolski() -> PluginMap:
    return PluginMap(
        id="u-he.podolski",
        name="Podolski",
        version="1",
        scope="u-he preset text; VST2 patches read back by the VST3 build",
        vendor="u-he",
        vst2=PluginFormat(Vst2Id(1349477487), "Podolski", VERBATIM, _HOSTED, plan_name="podolski_vst2"),
        vst3=PluginFormat(
            Vst3Id.of("D39D5B69-D6AF42FA-12345678-506F646F"),
            "Podolski",
            LENGTH_PREFIXED,
            _HOSTED,
            SameAsProcessor(LENGTH_PREFIXED.lower),
            plan_name="podolski_vst3",
        ),
        layouts=(Layout("u-he preset text", lambda patch: b"\n#AM=Podolski\n" in patch[:256]),),
    )
