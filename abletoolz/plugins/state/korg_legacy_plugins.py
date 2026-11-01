"""KORG Legacy MDE-X, Polysix and WAVESTATION standalone VST2 maps, for recognition only.

Each map names the plugin and keeps its saved chunk verbatim; nothing here reads
inside one. A device saves the whole bank as its chunk: MDE-X and Polysix a
global block (``glbl``, ``bolg``) and then every program. The WAVESTATION's
three host parameters do not describe the performance and wave-sequence data in
its chunk.
"""

from __future__ import annotations

from abletoolz.plugins.identity.ids import Vst2Id
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.framings import VERBATIM
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import PluginFormat, PluginMap

MDEX_SHA256 = "92f4228c9cacb7691dcc9f625db2552efdc77c52af09500b68f586f20fb3a403"
WAVESTATION_SHA256 = "070092f07185c496166553bab98996f8101ec0a333d83456bcd9d0b787957baf"


def _structural() -> MeasuredState:
    return MeasuredState(StateRung.VERBATIM, (StateEvidence.STRUCTURAL,))


@register
def korg_mde_x() -> PluginMap:
    return PluginMap(
        id="korg.mde-x",
        name="MDE-X",
        version="1",
        scope="Standalone VST2; 296-byte programs and 64 normalized parameter slots",
        vendor="KORG",
        vst2=PluginFormat(Vst2Id(0x4B4C4656), "MDE-X", VERBATIM, _structural(), plan_name="mde_x_vst2"),
        binary_sha256=(MDEX_SHA256,),
    )


@register
def korg_polysix() -> PluginMap:
    return PluginMap(
        id="korg.polysix",
        name="Polysix",
        version="1",
        scope="Standalone VST2; 2,048-byte prog records, internal fields remain opaque",
        vendor="KORG",
        vst2=PluginFormat(Vst2Id(0x4B4C5056), "Polysix", VERBATIM, _structural(), plan_name="polysix_vst2"),
    )


@register
def korg_wavestation() -> PluginMap:
    return PluginMap(
        id="korg.wavestation",
        name="WAVESTATION",
        version="1",
        scope="Standalone VST2 identity and opaque native state; three host controls",
        vendor="KORG",
        vst2=PluginFormat(Vst2Id(0x4B4C5756), "WAVESTATION", VERBATIM, _structural(), plan_name="wavestation_vst2"),
        binary_sha256=(WAVESTATION_SHA256,),
    )
