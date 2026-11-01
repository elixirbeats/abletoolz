"""CS-80V and CS-80 V2 patches into CS-80 V3, control by control under the names CS-80 V3 stores.

CS-80 V3 is the same engine as CS-80 V2 with a new host: its processor keeps
V2's controls, its scalar curves (30 helpers agree bit for bit as float32 at
1,031 inputs each) and its routing (the same graph nodes for every modulation
input and output), so the join is by the byte each control sits at in the old
chunk (``state/cs_80_v.py``) and the work is in how each side stores a value.
A converted patch is the installed CS-80 V3's factory Default with every
mapped control written over it. Every control goes one of these ways:

* **copied** -- the value in CS-80 V3's stored 0 to 1: a knob as it is, an
  item index or a note over CS-80 V3's range for it;
* **switched** -- a two-way switch, which the old plugins store at 0.25 or
  0.75 and read as on from 0.5 (measured by hosting CS-80 V2), written 0 or 1
  as Arturia's own CS-80 V3 ports of the old factory patches write it;
* **as the ports write it** -- CS-80 V3 keeps the sine and ring modulator
  switches hidden and always on, and plays neither switch (hosted). A sine
  switched off (CS-80 V2 then ignores its level) is written on at level 0. A
  ring modulator switched off (CS-80 V2 then ignores its knobs, and plays as it
  does with the modulation knob at full) is written on with that knob at full.
  A disconnected modulation input stored as 14 is CS-80 V3's 13, a modulation
  amount above 1 is written at 1, the multi panel's third setting is written
  as its second (CS-80 V3 plays each voice's own patch either way), and zone
  ends left outside the keyboard in an unused zone are clamped;
* **said** -- written at the end of CS-80 V3's range: any other value outside
  it; reported: the eight voices' fine tuning, which CS-80 V3 stores but does
  not play (CS-80 V2 tunes all its voices about 6 cents up or down across the
  range; the first generation leaves them at 0, which the ports centre), the
  saved pitch bend and the expression pedal, which a CS-80 V3 state does not
  hold. The saved mod wheel is written to both of CS-80 V3's names for it.

The evidence for the mapping is Arturia's own: of the old patches in the
corpus, those named after a factory preset have a CS-80 V3 port of that
preset in its factory library, and every control of the patch that plays and
of the global panel matches the converted one, for both generations, except
where the ports were re-voiced by hand (levels, effects, a few whole patches)
or reset (the performance switches). The ports also add mod wheel and pedal
routes in free modulation slots; those are not added here. Rendered side by
side, a converted CS-80 V2 patch on CS-80 V3 and Arturia's port given the same
level and effects differ by 0.4 dB. CS-80 V3 itself plays about 5 cents flat of
CS-80 V2 and its envelopes and filters do not sound identical; nothing here
changes that.
"""

from __future__ import annotations

import datetime
import functools

from abletoolz.plugins.converters import limitation
from abletoolz.plugins.state import cs_80_v, cs_80_v3
from abletoolz.plugins.state.catalog import Converted, converter
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung

PATCHES = 8
SLOTS = 10
INPUT_DISCONNECTED_ALIAS, INPUT_OFF = 14, 13
_PATCH_SWITCHES = (
    "OSC1_SQR",
    "OSC2_SQR",
    "OSC1_SYNCHRO",
    "OSC2_DERIVE",
    "OSC1_KEYB",
    "OSC2_KEYB",
    "ON_HIGHT1",
    "ON_HIGHT2",
    "ON_LOW1",
    "ON_LOW2",
    "FILTRE1_TYPE",
    "FILTRE2_TYPE",
    "LFO1_SYNC",
    "LFO2_SYNC",
)
_GLOBAL_SWITCHES = (
    "ARP_HOLD",
    "ARP_PLAY",
    "ARP_SYNC",
    "GLISSENDO",
    "MODUL_TO_VIB",
    "ON_CHORUS",
    "ON_DELAI",
    "PEDALE_EXPRESSION",
    "PEDALE_PORTAMENTO_ON",
    "PEDALE_SUSTAIN_ON",
    "PEDALE_WHA",
    "PITCH_BEND_ACTIF",
    "SUSTAIN_MODE",
    "SYNC_DELAI",
    "TRAPPE_MODUL",
    "TYPE_CHORUS",
    *(f"{name}{voice}" for name in ("EFFETS_VOIE", "PORTAMENTO_VOIE", "RING_MOD_VOIE") for voice in range(1, 9)),
    *(f"ZONE{zone}_ARP" for zone in range(1, 5)),
)
SWITCHES = frozenset(
    (*_GLOBAL_SWITCHES, *(f"Patch{n}_{name}" for n in range(1, PATCHES + 1) for name in _PATCH_SWITCHES))
)
DETUNES = tuple(f"CS_80_DETUNE{voice}" for voice in range(1, 9))
WHEEL, WHEEL_ALIAS = "MODULATION_WHEEL_CONTROL", "VST3_CtrlModWheel"

NOTE = (
    "Copied control by control under the names CS-80 V3 stores, the way Arturia's own CS-80 V3 ports of the old "
    "factory patches carry them; read back by the installed CS-80 V3. Not heard in Live: audition this device."
)


def _on(value: float) -> bool:
    return value >= 0.5


def plan(
    chunk: bytes, generation: cs_80_v.Generation, notices: list[str] | None = None
) -> tuple[dict[str, float], list[str]]:
    """CS-80 V3's stored values for an old chunk, by name, and what is reported as not carried.

    A value outside CS-80 V3's range is refused without ``notices``, or written at the end of the range and said.
    """
    old = cs_80_v.stored(chunk, generation)
    raw = dict(old)
    for patch in range(1, PATCHES + 1):
        for slot in range(1, SLOTS + 1):
            name = f"Patch{patch}_MODULATION{slot:02d}_INPUT"
            if raw[name] == INPUT_DISCONNECTED_ALIAS:
                raw[name] = INPUT_OFF
            amount = f"Patch{patch}_MODULATION{slot:02d}_AMOUNT"
            raw[amount] = min(raw[amount], 1.0)
    raw["TRAPPE_MULTI"] = min(raw["TRAPPE_MULTI"], 1.0)
    out: dict[str, float] = {}
    for name, (_, low, high) in cs_80_v.fields().items():
        value = raw[name]
        if name in SWITCHES:
            out[name] = 1.0 if _on(value) else 0.0
            continue
        # A zone's ends keep whatever the old plugin left in them, fractions of a note included; the ports clamp them.
        zone = name.startswith("ZONE") and name.endswith(("_INF", "_SUP"))
        if (high != 1 or low != 0) and not zone and value != int(value):
            raise StateTransformError(f"{name} holds {value}, not an item index")
        stored = (value - low) / (high - low)
        out[name] = min(max(stored, 0.0), 1.0)
        if out[name] != stored and not zone:
            limitation(notices, f"{name} stores {value:g}, outside CS-80 V3's {low:g} to {high:g}; written at the end")
    for patch in range(1, PATCHES + 1):
        for osc in (1, 2):
            switch = f"Patch{patch}_OSC{osc}_ON_SIN"
            if not _on(raw[switch]):
                out[f"Patch{patch}_OSC{osc}_SINUS"] = 0.0
            out[switch] = 1.0
    if not _on(raw["ON_RINGMOD"]):
        out["RING_MODULATION"] = 1.0
    out["ON_RINGMOD"] = 1.0
    reported = []
    tunings = [raw[name] for name in DETUNES]
    if any(value != 0.5 for value in tunings) and not (generation.first and not any(tunings)):
        reported.append("the eight voices' fine tuning (" + ", ".join(f"{value:.3f}" for value in tunings) + ")")
    out.update(dict.fromkeys(DETUNES, 0.5))
    out[WHEEL_ALIAS] = out[WHEEL]
    bend, expression = cs_80_v.performance(chunk)
    if bend != 0.5:
        reported.append(f"the saved pitch bend ({bend:.3f}, centre is 0.500)")
    if expression != 0:
        reported.append(f"the saved expression pedal ({expression:.3f})")
    return out, reported


def _convert(generation: cs_80_v.Generation, chunk: bytes, program: int | None = None) -> Converted:
    """An old chunk as a CS-80 V3 state. ``program`` is not used: the chunk holds one patch."""
    notices = [NOTE]
    values, reported = plan(chunk, generation, notices)
    if reported:
        notices.append("Not carried: " + "; ".join(reported) + ".")
    return Converted(cs_80_v3.with_values(cs_80_v3.baseline(), values), tuple(notices))


MEASURED = MeasuredState(StateRung.RE_ENCODE, (StateEvidence.HOSTED,), datetime.date(2026, 10, 31))
CONVERSIONS = {
    generation.map_id: converter(generation.map_id, "arturia.cs-80-v3", MEASURED)(
        functools.partial(_convert, generation)
    )
    for generation in cs_80_v.GENERATIONS
}
