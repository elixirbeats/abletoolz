"""minimoog V Original patches into Mini V3, its descendant, control by control under the names both share.

Mini V3 kept the original's internal control names, so the join is by name and
the work is in how each side stores a value. A converted patch is the installed
Mini V3's default state with every control the original sets written over it.
Every control goes one of these ways:

* **copied** -- the same stored 0 to 1 on both: every knob, and the choices
  whose items both plugins list in the same order (ranges, waveforms, LFO wave,
  arpeggiator mode, octaves and repeats; the original's item labels sit in its
  binary in that order) or that Arturia's own Mini V3 ports of the original's
  factory patches carry unchanged (chorus type). The saved mod wheel is
  ``VST3_CtrlModWheel`` in Mini V3. A level above full (the original stores up
  to 1.1) is written at full, which Mini V3 plays the same;
* **switched** -- a switch stored anywhere from 0 to 1, read as on from 0.5 the
  way Mini V3 reads it, written as 0 or 1. Legato is a switch in the original
  and Off, On or Glide in Mini V3; the ports write the original's on as Glide;
* **indexed** -- the original stores an item's index, Mini V3 that index over
  its top index: the modulation outputs (36 in Mini V3) and inputs (16), play
  mode (3) and polyphony (1 to 8 voices, then 16 and 32; the original counts 1
  to 16, then 32). The ports keep every index. An unused slot (input Off) can
  hold an output past Mini V3's list in the original; it is written Off. Mini V3
  keeps an LFO-to-cutoff route in every patch and adds one to its last unused
  slot when a patch has none, so that slot is written that way at no depth;
* **refused or said** -- what Mini V3 has no place for: modulation slots past
  the eighth, the motion recorder's curves, a MIDI channel, a polyphony Mini V3
  does not offer, and the external input or arpeggiator hold set anywhere but
  off (Mini V3 splits each into three settings where the original has two). The
  saved pitch bend and expression pedal are not part of a Mini V3 state.

The original's ``LFO_WAVE`` is not carried: Mini V3 plays ``LFO_WAVE_EXT`` and
leaves the other at 0.5 in every one of its factory patches. Nothing here has
been heard in Live, and the original cannot be hosted on a 64-bit machine.
"""

from __future__ import annotations

import datetime
import math

from abletoolz.plugins.converters import limitation
from abletoolz.plugins.state import mini_v3, minimoog_v
from abletoolz.plugins.state.catalog import Converted, converter
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung

COPIED = (
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
    "LFO_RATE",
    "LFO_WAVE_EXT",
    "VOLUME",
    "PORTAMENTO",
    "TUNE",
    "DETUNE_VOIES",
    "SPEED_CHORUS",
    "DEPTH_CHORUS",
    "DRY_WET_CHORUS",
    "TYPE_CHORUS",
    "SPEED_DELAI_LEFT",
    "DEPTH_DELAI_LEFT",
    "SPEED_DELAI_RIGHT",
    "DEPTH_DELAI_RIGHT",
    "DRY_WET_DELAI",
    "ARP_MODE",
    "ARP_SPEED",
    "ARP_OCTAVE",
    "ARP_REPEAT",
    "PITCH_BEND_COARSE",
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
    "FORMANT_LFO_RATE",
    *(f"MODULATION{slot:02d}_AMOUNT" for slot in range(1, minimoog_v.SHOWN_SLOTS + 1)),
)
RENAMED = {"MODULATION_WHEEL_CONTROL": "VST3_CtrlModWheel"}
SWITCHES = (
    "KEYB_OSC2",
    "KEYB_OSC3",
    "MODUL_OSC",
    "SYNCHRO_OSC1",
    "ON_OSC1",
    "ON_OSC2",
    "ON_OSC3",
    "ON_NOISE",
    "ON_ROSE",
    "MODUL_FILTRE",
    "KEYB_FOLLOW1_ON",
    "KEYB_FOLLOW2_ON",
    "POLY_ON",
    "UNISSON_ON",
    "SOFT_DIST",
    "LFO_SYNC",
    "ON_GLIDE",
    "ON_RELEASE",
    "PEDAL_GLIDE",
    "PEDAL_RELEASE",
    "ON_CHORUS",
    "ON_DELAI",
    "SYNC_DELAI",
    "ARP_SYNC",
    "ARP_PLAY",
    "TRAPPE_MODUL",
    "PITCH_BEND_ACTIF",
    "ON_A440",
    "ON_FORMANT_FILTER",
    "FORMANT_LFO_ON",
)
# The original's two settings against Mini V3's three; off reads the same below a third.
THREE_WAY = {"ON_EXT": "the external input", "ARP_HOLD": "the arpeggiator hold"}
OUTPUTS, INPUTS, OUTPUT_OFF, INPUT_OFF = 36, 16, 32, 12
LFO, CUTOFF = 8, 11
ORIGINAL_VOICES = (*range(1, 17), 32)
MINI_V3_VOICES = (*range(1, 9), 16, 32)

NOTE = (
    "Copied control by control under the names minimoog V Original and Mini V3 share, the way Arturia's own Mini V3 "
    "ports of the original's factory patches carry them; read back by the installed Mini V3. Not heard in Live: "
    "audition this device."
)


def _index(name: str, stored: float, count: float = math.inf) -> int:
    """The item index the original stores for ``name``; anything else is not a patch it wrote."""
    if not (math.isfinite(stored) and stored == int(stored) and 0 <= stored < count):
        raise StateTransformError(f"{name} holds {stored}, not an item index below {count}")
    return int(stored)


def plan(chunk: bytes, notices: list[str] | None = None) -> tuple[dict[str, float], list[str]]:
    """Mini V3's stored values for a minimoog V Original chunk, by name, and what is reported as not carried.

    A setting Mini V3 has no place for is refused without ``notices``, or left out and said there.
    """
    old = minimoog_v.values(chunk)
    out: dict[str, float] = {}
    for name in COPIED:
        out[name] = min(max(old[name], 0.0), 1.0)
        if out[name] != old[name]:
            limitation(notices, f"{name} stores {old[name]:.4g}, outside 0 to 1; written as {out[name]:g}")
    for name, target in RENAMED.items():
        out[target] = old[name]
    for name in SWITCHES:
        out[name] = 1.0 if old[name] >= 0.5 else 0.0
    out["MODE_LEGATO"] = 1.0 if old["MODE_LEGATO"] >= 0.5 else 0.0
    for name, what in THREE_WAY.items():
        out[name] = 0.0
        if old[name] >= 1 / 3:
            limitation(notices, f"{what} set at {old[name]:.3g} has no measured Mini V3 setting; written off")
    out["MODE_ZONE"] = _index("MODE_ZONE", old["MODE_ZONE"], 3) / 2
    voices = ORIGINAL_VOICES[_index("NBR_VOIES", old["NBR_VOIES"], len(ORIGINAL_VOICES))]
    if voices not in MINI_V3_VOICES:
        limitation(notices, f"polyphony of {voices} voices is not a Mini V3 setting; written as 8")
        voices = 8
    out["NBR_VOIES"] = MINI_V3_VOICES.index(voices) / (len(MINI_V3_VOICES) - 1)
    routes: dict[int, tuple[int, int]] = {}
    for slot in range(1, minimoog_v.MODULATION_SLOTS + 1):
        prefix = f"MODULATION{slot:02d}"
        source = _index(f"{prefix}_INPUT", old[f"{prefix}_INPUT"], INPUTS)
        destination = _index(f"{prefix}_OUTPUT", old[f"{prefix}_OUTPUT"])
        if slot > minimoog_v.SHOWN_SLOTS:
            if source != INPUT_OFF:
                limitation(notices, f"modulation slot {slot} is in use; Mini V3 has {minimoog_v.SHOWN_SLOTS}")
            continue
        if destination >= OUTPUTS:
            if source != INPUT_OFF:
                limitation(notices, f"modulation slot {slot} routes to output {destination}, past Mini V3's; left off")
            source, destination = INPUT_OFF, OUTPUT_OFF
        routes[slot] = (source, destination)
    # Mini V3 keeps an LFO-to-cutoff route in every patch: given one without it, it routes its last unused slot
    # that way at the amount the slot holds. That slot is written so here, at no depth, which is the state it saves.
    unused = [slot for slot, (source, _) in routes.items() if source == INPUT_OFF]
    if (LFO, CUTOFF) not in routes.values() and unused:
        routes[unused[-1]] = (LFO, CUTOFF)
        out[f"MODULATION{unused[-1]:02d}_AMOUNT"] = 0.5
    for slot, (source, destination) in routes.items():
        out[f"MODULATION{slot:02d}_INPUT"] = source / (INPUTS - 1)
        out[f"MODULATION{slot:02d}_OUTPUT"] = destination / (OUTPUTS - 1)
    if old["ON_MOTION_RECORDER"] >= 0.5 or any(old[f"COURBE{n}_DEST"] != OUTPUT_OFF for n in range(1, 5)):
        limitation(notices, "the motion recorder's curves are not carried; Mini V3's recorder is left off")
    if old["CANAL_MIDI"] != 0:
        limitation(notices, f"MIDI channel setting {old['CANAL_MIDI']:g} is not carried; Mini V3 listens on all")
    reported = []
    if old["PITCH_WHEEL_CONTROL"] != 0.5:
        reported.append(f"the saved pitch bend ({old['PITCH_WHEEL_CONTROL']:.3f}, centre is 0.500)")
    if old["FOOT_WHEEL_CONTROL"] != 0:
        reported.append(f"the saved expression pedal ({old['FOOT_WHEEL_CONTROL']:.3f})")
    return out, reported


@converter(
    "arturia.minimoog-v-original",
    "arturia.mini-v3",
    MeasuredState(StateRung.RE_ENCODE, (StateEvidence.HOSTED,), datetime.date(2026, 10, 30)),
)
def convert(patch: bytes, program: int | None = None) -> Converted:
    """A minimoog V Original chunk as a Mini V3 state. ``program`` is not used: the chunk holds one patch."""
    notices = [NOTE]
    values, reported = plan(patch, notices)
    if reported:
        notices.append("Not carried: " + "; ".join(reported) + ".")
    return Converted(mini_v3.with_values(mini_v3.baseline(), values), tuple(notices))
