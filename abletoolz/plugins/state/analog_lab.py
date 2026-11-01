"""Arturia Analog Lab 3 and 4, and Analog Lab V: a preset wrapped around a synth's patch, in Boost text archives.

All three save the same thing: a Boost text archive that opens with the preset's
record -- name, bank, the id of the instrument that saved it, author, type,
tags, comment, time, version and a map of named strings -- followed by the
record of the synth patch inside it and then every control as a named number.
The instrument id says which product wrote the preset: 1 for Analog Lab 3 and 4,
60 for Analog Lab V.

The VST2 state is two such archives behind their two lengths. Analog Lab 4.1
writes the preset's own record empty in the first and fills it in the second;
4.2 writes the same archive twice. Analog Lab 4's VST3 keeps the archive as
both its processor and its controller half, so the archive twice is the patch
it holds. Analog Lab V does the same in both formats: its own default state,
hosted, is one archive twice in its VST2 and that archive as both VST3 halves.

Analog Lab 3 stores the same VST2 id as Analog Lab 4, so one map reads both.
"""

from __future__ import annotations

import datetime

from abletoolz.formats.arturia_archive import TwoArchives
from abletoolz.formats.errors import FormatError
from abletoolz.plugins.identity.ids import Vst2Id, Vst3Id
from abletoolz.plugins.state.arturia_v import FirstArchive, OneArchive
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.framings import VERBATIM
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import PluginFormat, PluginMap

ANALOG_LAB_4_VST2 = Vst2Id(1097621810)
ANALOG_LAB_4_VST3 = Vst3Id((1098019957, 1096173907, 1097621810, 1349676899))
ANALOG_LAB_V_VST2 = Vst2Id(1097621878)
ANALOG_LAB_V_VST3 = Vst3Id((1098019957, 1096173907, 1097621878, 1349676899))


def archives(patch: bytes, product: str) -> tuple[bytes, bytes]:
    """The two archives of a VST2 state, refused when the lengths do not cut it."""
    try:
        return TwoArchives.split(patch)
    except FormatError as error:
        raise StateTransformError(f"not an {product} patch: {error}") from None


@register
def analog_lab_4() -> PluginMap:
    measured = MeasuredState(StateRung.VERBATIM, (StateEvidence.HOSTED,), datetime.date(2026, 10, 31))
    return PluginMap(
        id="arturia.analog-lab-4",
        name="Analog Lab 4",
        version="4",
        scope="also Analog Lab 3, which stores the same VST2 id; a preset archive twice; the VST3 is read, not written",
        vendor="Arturia",
        vst2=PluginFormat(ANALOG_LAB_4_VST2, "Analog Lab 4", VERBATIM, measured, plan_name="analog_lab_4_vst2"),
        vst3=PluginFormat(
            ANALOG_LAB_4_VST3, "Analog Lab 4", OneArchive("Analog Lab 4"), measured, plan_name="analog_lab_4_vst3"
        ),
    )


@register
def analog_lab_v() -> PluginMap:
    measured = MeasuredState(StateRung.VERBATIM, (StateEvidence.HOSTED,), datetime.date(2026, 10, 31))
    return PluginMap(
        id="arturia.analog-lab-v",
        name="Analog Lab V",
        version="5",
        scope="5.11.2.6235; a preset archive twice; the VST3 is neither read nor written",
        vendor="Arturia",
        vst2=PluginFormat(ANALOG_LAB_V_VST2, "Analog Lab V", VERBATIM, measured, plan_name="analog_lab_v_vst2"),
        # No controller half: the VST3 converts an older preset differently from the VST2 (a two-part Analog Lab 4
        # preset loses its macros and changes again on the next load), so only the VST2 is written.
        vst3=PluginFormat(
            ANALOG_LAB_V_VST3, "Analog Lab V", FirstArchive("Analog Lab V"), measured, plan_name="analog_lab_v_vst3"
        ),
    )
