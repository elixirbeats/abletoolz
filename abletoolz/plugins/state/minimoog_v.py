"""Arturia minimoog V Original: its VST2 chunk, read by the plugin's own control names.

Its chunk is 1,797 little-endian
float32, 7,188 bytes, read off the plugin's parameter registrations: each
control registers its internal name (the same names Mini V3's archives store),
its display label and its byte offset in the chunk. Most hold a stored 0 to 1
like a host parameter; a choice among items is stored as that position too, but
the modulation routings, play mode, polyphony and the motion recorder's
destinations hold the item's index itself.

The chunk has room for 20 modulation slots, each its output, input and amount
from byte 192; the plugin shows eight. It also holds a preset path from byte
592, the motion recorder's four curves from byte 2,356, 1,580 bytes apart, and
a few words the editor keeps for its preset browser; none of those is read.
"""

from __future__ import annotations

import struct

from abletoolz.plugins.identity.ids import Vst2Id
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.framings import VERBATIM
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import PluginFormat, PluginMap

MINIMOOG_V_ORIGINAL_VST2 = Vst2Id(5065038)
PRODUCT = "minimoog V Original"
CHUNK_BYTES = 7188
MODULATION_SLOTS = 20
SHOWN_SLOTS = 8

# Runs of controls 4 bytes apart, from the byte offset each run starts at.
_RUNS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        0,
        (
            "RANGE_OSC1",
            "RANGE_OSC2",
            "RANGE_OSC3",
            "WAVE_OSC1",
            "WAVE_OSC2",
            "WAVE_OSC3",
            "WIDTH_OSC1",
            "WIDTH_OSC2",
            "WIDTH_OSC3",
            "ACCORD_OSC2",
            "ACCORD_OSC3",
            "KEYB_OSC3",
            "MODUL_OSC",
            "SYNCHRO_OSC1",
            "ON_OSC1",
            "ON_OSC2",
            "ON_OSC3",
            "ON_NOISE",
            "ON_EXT",
            "ON_ROSE",
            "MODUL_FILTRE",
            "KEYB_FOLLOW1_ON",
            "KEYB_FOLLOW2_ON",
            "MODE_ZONE",
            "CANAL_MIDI",
            "MODE_LEGATO",
            "POLY_ON",
            "UNISSON_ON",
            "NBR_VOIES",
            "SOFT_DIST",
        ),
    ),
    (
        124,
        (
            "MIXE_MODULATION",
            "FINE_OSC2",
            "FINE_OSC3",
            "LEVEL_OSC1",
            "LEVEL_OSC2",
            "LEVEL_OSC3",
            "LEVEL_NOISE",
            "LEVEL_EXT",
            "CUT_OFF",
            "EMPHASIS",
            "AMOUNT",
            "TEMPS_ATTAQUE_VCF",
            "TEMPS_DECAY_VCF",
            "LEVEL_SUSTAIN_VCF",
            "TEMPS_ATTAQUE_VCA",
            "TEMPS_DECAY_VCA",
            "LEVEL_SUSTAIN_VCA",
        ),
    ),
    (
        432,
        (
            "LFO_RATE",
            "LFO_WAVE",
            "LFO_SYNC",
            "VOLUME",
            "PORTAMENTO",
            "TUNE",
            "DETUNE_VOIES",
            "ON_GLIDE",
            "ON_RELEASE",
            "PEDAL_GLIDE",
            "PEDAL_RELEASE",
            "SPEED_CHORUS",
            "DEPTH_CHORUS",
            "DRY_WET_CHORUS",
            "ON_CHORUS",
            "TYPE_CHORUS",
            "SPEED_DELAI_LEFT",
            "DEPTH_DELAI_LEFT",
            "SPEED_DELAI_RIGHT",
            "DEPTH_DELAI_RIGHT",
            "DRY_WET_DELAI",
            "ON_DELAI",
            "SYNC_DELAI",
            "PITCH_WHEEL_CONTROL",
            "MODULATION_WHEEL_CONTROL",
            "FOOT_WHEEL_CONTROL",
            "ARP_MODE",
            "ARP_SPEED",
            "ARP_SYNC",
            "ARP_HOLD",
            "ARP_OCTAVE",
            "ARP_PLAY",
            "ARP_REPEAT",
            "TRAPPE_MODUL",
            "PITCH_BEND_COARSE",
            "PITCH_BEND_ACTIF",
        ),
    ),
    (588, ("ON_A440",)),
    (740, ("KEYB_OSC2",)),
    (
        748,
        (
            "FORMANT_DRY_WET",
            "FORMANT_Q",
            "FORMANT_LISTENER_X",
            "FORMANT_LISTENER_Y",
            "FORMANT_A_X",
            "FORMANT_A_Y",
            "FORMANT_E_X",
            "FORMANT_E_Y",
            "FORMANT_I_X",
            "FORMANT_I_Y",
            "FORMANT_O_X",
            "FORMANT_O_Y",
            "FORMANT_U_X",
            "FORMANT_U_Y",
            "ON_FORMANT_FILTER",
            "FORMANT_LFO_RATE",
            "FORMANT_LFO_ON",
            "ON_MOTION_RECORDER",
        ),
    ),
    (7184, ("LFO_WAVE_EXT",)),
)


def _offsets() -> dict[str, int]:
    offsets = {name: start + 4 * n for start, names in _RUNS for n, name in enumerate(names)}
    for slot in range(1, MODULATION_SLOTS + 1):
        for n, field in enumerate(("OUTPUT", "INPUT", "AMOUNT")):
            offsets[f"MODULATION{slot:02d}_{field}"] = 192 + 12 * (slot - 1) + 4 * n
    for curve in range(1, 5):
        offsets[f"COURBE{curve}_DEST"] = 2356 + 1580 * (curve - 1)
    return offsets


OFFSETS = _offsets()


def values(chunk: bytes) -> dict[str, float]:
    """Every control the chunk holds, by the plugin's internal name, as stored."""
    if len(chunk) != CHUNK_BYTES:
        raise StateTransformError(f"a {PRODUCT} chunk is {CHUNK_BYTES} bytes, not {len(chunk)}")
    return {name: struct.unpack_from("<f", chunk, offset)[0] for name, offset in OFFSETS.items()}


@register
def minimoog_v_original() -> PluginMap:
    measured = MeasuredState(StateRung.VERBATIM, (StateEvidence.STRUCTURAL,))
    return PluginMap(
        id="arturia.minimoog-v-original",
        name=PRODUCT,
        version="1",
        scope="32-bit VST2; a chunk of float32 at the offsets the plugin registers its controls at",
        vendor="Arturia",
        vst2=PluginFormat(MINIMOOG_V_ORIGINAL_VST2, PRODUCT, VERBATIM, measured, plan_name="minimoog_v_original_vst2"),
    )
