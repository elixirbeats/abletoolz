"""Arturia Mini V3: its patch, edited by the names its archives store, in the stored 0 to 1.

Its VST2 state is two Boost text archives behind their lengths
(:class:`~abletoolz.formats.arturia_archive.TwoArchives`), the same archive
twice, and every control is a named number in each one's map. The names are the
plugin's own internal ones (``CUT_OFF``, ``MODULATION01_OUTPUT``), and a value is
the stored 0 to 1: a choice reads as the floor of the value times its item
count, so an item is written as its index over the top index. The installed
3.12.0.3422 VST3 keeps a value above 1 (``LEVEL_OSC1`` 1.1) but plays it as 1.

The plugin stores every number as a single-precision float to eight digits and
rewrites any other spelling on load, so this module writes them that way: a
state built here comes back from the plugin byte for byte. The preset record
at the head of an archive is not restored from a state at all; the plugin saves
its own, so the baseline keeps the one it wrote.

``data/mini-v3-baseline.state`` is the installed VST3's own default state, saved
twice so that the plugin's own rounding has settled, in the VST2 form. The VST3
keeps the same archive as its processor and its controller state.
"""

from __future__ import annotations

import datetime

from abletoolz.plugins.state.arturia_v import ArchiveInstrument
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.plugin_map import PluginMap

MINI_V3 = ArchiveInstrument(
    "arturia.mini-v3", "Mini V3", 1296649779, "3.12.0.3422", "mini-v3-baseline.state", datetime.date(2026, 10, 30)
)
baseline = MINI_V3.baseline
with_values = MINI_V3.with_values


@register
def mini_v3() -> PluginMap:
    return MINI_V3.plugin_map()
