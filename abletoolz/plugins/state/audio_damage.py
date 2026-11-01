"""Audio Damage's Ratshack Reverb 2, DubStation 1.5 and Discord 3, read from what their devices saved.

Each saves its whole bank as the chunk (``FBCh``): per program a 24-byte name,
one little-endian 0..1 float per control, then its MIDI controller assignments.

* Ratshack: 5 programs of 6 floats and 8 bytes.
* DubStation: 20 programs of 11 floats and 12 bytes.
* Discord 3: 32 programs of 41 floats and 44 bytes. It does not start without
  its registration.
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

RATSHACK_BANK = ProgramBank(programs=5, record=56, name=24, controls=6)
DUBSTATION_BANK = ProgramBank(programs=20, record=80, name=24, controls=11)
DISCORD_BANK = ProgramBank(programs=32, record=232, name=24, controls=41)


@register
def ratshack_reverb_2() -> PluginMap:
    return PluginMap(
        id="audio-damage.ratshack-reverb-2",
        name="Ratshack Reverb 2",
        version="2",
        scope="VST2 chunk: the whole bank of 5 programs",
        vendor="Audio Damage",
        vst2=PluginFormat(
            Vst2Id(1095004790), "RatshackReverb2 x64", VERBATIM, _STRUCTURAL, plan_name="ratshack_reverb_2_vst2"
        ),
        layouts=(RATSHACK_BANK.layout(),),
        binary_sha256=("0639a6196f88941ab491b22da6f7fc9ab4e3e5879e7cd6c6b9f70e0704ce37cb",),
    )


@register
def dubstation_15() -> PluginMap:
    return PluginMap(
        id="audio-damage.dubstation-1.5",
        name="DubStation 1.5",
        version="1.5",
        scope="VST2 chunk: the whole bank of 20 programs",
        vendor="Audio Damage",
        vst2=PluginFormat(Vst2Id(1095001141), "DubStation_15 x64", VERBATIM, _STRUCTURAL, plan_name="dubstation_vst2"),
        layouts=(DUBSTATION_BANK.layout(),),
        binary_sha256=("ff930678c7a5c1a096f4a78c52f02ec8a73263548a1c3a7c053e32549732de7a",),
    )


@register
def discord_3() -> PluginMap:
    return PluginMap(
        id="audio-damage.discord-3",
        name="Discord 3",
        version="3",
        scope="VST2 chunk: the whole bank of 32 programs",
        vendor="Audio Damage",
        vst2=PluginFormat(Vst2Id(0x41446433), "Discord 3", VERBATIM, _STRUCTURAL, plan_name="discord_3_vst2"),
        layouts=(DISCORD_BANK.layout(),),
        binary_sha256=("8cf2138f3a7c97da5bcf8000b5a1f2699058dd88662c8293585f21d29848dba9",),
    )
