"""Arturia CS-80 V3: its patch, edited by the names its archives store, in the stored 0 to 1.

Its VST2 state is two Boost text archives behind their lengths
(:class:`~abletoolz.formats.arturia_archive.TwoArchives`), the same archive
twice, and every control is a named number in each one's map: eight patches of
106 controls each (``Patch1_CUTOFF_LOW1``), then the voices, the keyboard
zones, the effects and the arpeggiator. A choice is stored as its item's index
over the top index. The VST3 keeps the same archive as its processor and its
controller state.

``data/cs-80-v3-baseline.state`` is the installed plugin's factory Default,
saved by the plugin, with no macro assignments: the state a converted patch is
built from. The preset record at the head of an archive is not restored from a
state; the plugin keeps the one it wrote. Numbers are written in the plugin's
own spelling, the single-precision float to eight digits.
"""

from __future__ import annotations

import datetime

from abletoolz.plugins.state.arturia_v import ArchiveInstrument
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.plugin_map import PluginMap

CS_80_V3 = ArchiveInstrument(
    "arturia.cs-80-v3", "CS-80 V3", 1129535027, "3.10", "cs-80-v3-baseline.state", datetime.date(2026, 10, 31)
)
baseline = CS_80_V3.baseline
with_values = CS_80_V3.with_values


@register
def cs_80_v3() -> PluginMap:
    return CS_80_V3.plugin_map()
