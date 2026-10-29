"""Legacy KORG MS-20 patches, standalone or inside Legacy Cell, into KORG MS-20 V, control by control.

The legacy MS-20 and Legacy Cell are 32-bit plugins no current Live loads, and
MS-20 V is Arturia's model of the same synth with the same front panel and patch
panel. A patch is rebuilt into MS-20 V's factory ``Default`` from both plugins'
maps, in the units each one prints. Every legacy control goes one of these ways,
and each converted device lists which:

* **calibrated** -- both plugins rendered from the same MIDI, one control at a
  time (``data/ms-20-to-korg-ms-20-v.json``): VCO 2's tune (semitones on both),
  fine tune (20 cents a step) into the master tune with the measured offset
  between the two plugins' tuning, the volume (a straight gain on the legacy
  knob) into the master volume, the MG's rate (a line in hertz), portamento by
  its glide time, and the pitch modulation depths from EG1 and the MG;
* **matched knob by knob** -- both plugins kept loaded and rendered side by
  side, MS-20 V's control searched at each legacy knob position for the nearest
  sound: both filters' cutoffs and the HPF peak by their response against the
  open filter, the VCO levels by loudness, the pulse width by its duty cycle,
  EG2's attack, decay, sustain, release and hold by their envelopes, the LPF's
  EG2 amount by the response it holds at full sustain, and both filters' MG
  amounts by how far they swing the sound's brightness;
* **rendered at a few points** -- the LPF peak at its own knob position on MS-20 V's
  MK I filter, whose resonance peak rises within 1.5 dB of the legacy one at four
  positions (MK II's does not); the rest of the filter's response differs from
  the middle up whatever the setting, and no setting does better on average;
* **same print** -- both plugins print the same 0 to 10: the MG waveform, EG1's
  attack and release (their times are not measured);
* **same switch** -- waveforms and scales, sync to tempo, key sync, the momentary
  switch, the pitch bend range, the voice mode (Mono with single trigger is
  MS-20 V's legato, with multi trigger its retrigger, Poly its voice count);
* **matched by panel position** -- a ballpark, the same knob position: EG1's
  delay, the HPF's EG2 amount (renders could not tell its settings apart), the
  ESP and the saved wheel;
* **patch cables, by jack name** -- every legacy jack but two has an MS-20 V
  source or destination by the same name; renders hear the MG's triangle and
  square into the VCOs' CV the same way on both;
* **reported** -- the device converts without it and says so: the analog drift,
  the pan, the external modulation routes, unison, transpose that is not whole
  octaves, the unnamed controls when they leave their initial values, and the
  cables to the headphone output or the VCA's initial gain;
* **carried at a cost** -- converted as near as it can be and warned about:
  settings past MS-20 V's range (clamped), more than six voices, and, for a Legacy
  Cell, every synth but the first MS-20. A direct call without ``notices``
  refuses these instead.

A Legacy Cell's working cell converts when it holds an MS-20; a cell of Polysix
alone is refused. The cell's effects and mixer are named and not carried.
Nothing here has been heard in Live.
"""

from __future__ import annotations

import datetime
import functools
import json
import math
import pathlib
from collections.abc import Mapping, Sequence
from typing import Any

from abletoolz.plugins.converters import limitation
from abletoolz.plugins.state.arturia_v import ArturiaValue
from abletoolz.plugins.state.catalog import Converted, converter
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.korg_legacy_cell import LegacyCellBank
from abletoolz.plugins.state.korg_ms20 import EXT_MOD_SLOTS, Ms20Bank, Ms20Program, Ms20Value
from abletoolz.plugins.state.korg_ms20_v import Ms20VPatch, baseline, controls
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung

DATA = pathlib.Path(__file__).with_name("data")
_KNOB = 10.0

SAME_SWITCH: dict[str, tuple[str, dict[str, str]]] = {
    "VCO 1 Wave Form": (
        "MS20VCO_WaveformOsc1",
        {"Triangle": "Triangle", "Saw": "Saw", "Pulse Width": "Pulse", "Noise": "Noise"},
    ),
    "VCO 1 Scale": ("MS20VCO_ScaleOsc1", {"32'": "32'", "16'": "16'", "8'": "8'", "4'": "4'"}),
    "VCO 2 Wave Form": (
        "MS20VCO_WaveformOsc2",
        {"Saw": "Saw", "Pulse 50%": "Square", "Pulse 10%": "Pulse", "Ring Mod.": "Ring Mod"},
    ),
    "VCO 2 Scale": ("MS20VCO_ScaleOsc2", {"16'": "16'", "8'": "8'", "4'": "4'", "2'": "2'"}),
    "Momentary Switch": ("MomentarySwitch", {"Off": "Off", "On": "On"}),
    "MG Key Sync": ("MG_Retrig", {"Off": "Free", "On": "Reset"}),
}
SAME_PRINT = {
    "MG Wave Form": "MG_Waveform",
    "EG 1 Attack": "EG1_Attack",
    "EG 1 Release": "EG1_Release",
}
PANEL = {
    "EG 1 Delay": "EG1_Delay",
    "HPF EG 2/Ext": "VCF_FmAmountHp",
    "ESP Signal Level": "ESP_ESPInpBandPass_InpLvl",
    "ESP Low Cut": "ESP_ESPInpBandPass_BP1Lvl",
    "ESP High Cut": "ESP_ESPInpBandPass_BP2Lvl",
    "ESP CV Adjust": "ESP_ESPPitchTracker_CVAdjust",
    "ESP Threshold": "ESP_ESPEnvFollower_ThreshLvl",
    "Wheel": "VST3_CtrlModWheel",
}
JACK_SOURCES = {
    "MG TRI OUT": "Mod Gen",
    "MG PW OUT": "Mod Gen Square",
    "SAMPLE HOLD OUT": "Sample and Hold",
    "ESP AMP OUT": "ESP Amp",
    "EG1 OUT": "Env 1",
    "EG1 REV OUT": "Env 1 Rev",
    "ESP BPF OUT": "ESP BP Filter",
    "VCA OUT": "MVCA",
    "PINK NOISE OUT": "Pink Noise",
    "ESP FV CONV CV OUT": "ESP Pitch Tracker",
    "EG2 REV OUT": "Env 2 Rev",
    "WHITE NOISE OUT": "White Noise",
    "WHEEL OUT": "Mod Wheel",
    "ESP ENV OUT": "ESP Env Follower CV",
    "SIGNAL OUT": "Signal Out",
    "KBD CV OUT": "Keyboard CV",
    "KBD TRIG OUT": "Keyboard Trigger",
    "MOMENTARY SW OUT": "Manual Switch",
    "ESP TRIG OUT": "ESP Env Follower Trig",
}
JACK_DESTINATIONS = {
    "TOTAL IN": "Total",
    "SAMPLE HOLD IN": "SnH In",
    "ESP SIGNAL IN": "ESP In",
    "SAMPLE HOLD CLOCK IN": "SnH Clock",
    "VCO FREQ IN": "VCO Frequency",
    "EXT SIGNAL IN": "VCF Ext In",
    "VCA IN": "MVCA In",
    "HPF CUTOFF IN": "HPF Cutoff",
    "VCA CONTROL IN": "MVCA Control",
    "LPF CUTOFF IN": "LPF Cutoff",
    "EG1 TRIG IN": "Env 1 Trig",
    "VCO2 CV IN": "VCO 2 CV",
    "VCO CV IN": "VCO CV",
    "KBD TRIG IN": "Env Trig",
}
# The values the unnamed controls hold in the plugin's own initial program.
UNNAMED_INITIAL: dict[str, object] = {
    "Control 53": "On",
    "Control 54": 144.0,
    "Control 60": "Off",
    "Control 61": "Off",
    "Control 63": 16,
}
_SYNC_BASE = {"1/1": 1.0, "1/2": 1 / 2, "1/4": 1 / 4, "1/8": 1 / 8, "1/16": 1 / 16, "1/32": 1 / 32}
_TARGET_BARS = {"8/1": 8.0, "4/1": 4.0, "2/1": 2.0, "1/1": 1.0, "1/2": 1 / 2, "1/4": 1 / 4, "1/8": 1 / 8}
_TARGET_BARS |= {"1/16": 1 / 16, "1/32": 1 / 32}
_TARGET_TYPES = {"Bars": 1.0, "Bars Triplet": 2 / 3, "Bars Dotted": 3 / 2}

MEASURED_NOTE = (
    "Calibrated against both plugins: VCO 2 tune, fine tune, volume, the MG rate, portamento, and the EG1 and MG "
    "pitch modulation depths. Matched knob by knob by sound: both filter cutoffs, the HPF peak, the VCO levels, "
    "pulse width, EG2's attack, decay, sustain, release and hold, the LPF's EG2 amount and both MG amounts."
)
RENDERED_NOTE = (
    "Rendered at a few points: the LPF peak on MS-20 V's MK I filter. Same print: the MG waveform, EG1 attack and "
    "release. Matched by panel position, a ballpark: EG1 delay, the HPF's EG2 amount, the ESP and the saved wheel. "
    "Patch cables by jack name."
)
EXPECT_NOTE = (
    "Expect, even where every control matched: MS-20 V is 3.5 to 6 dB quieter above 4 kHz with its filter open, "
    "so conversions sound a little darker; both filters' resonance behaves differently (the HPF peak from 6 to 8 "
    "stays up to 4 dB apart, the LPF peak 3 to 6 dB from the middle up, whatever the setting); EG2's curves have "
    "another shape; and patches leaning on resonance can come out brighter or duller, and several dB quieter."
)
NOT_HEARD_NOTE = "MS-20 to MS-20 V has not been heard in Live: audition this device."


@functools.cache
def _calibration() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((DATA / "ms-20-to-korg-ms-20-v.json").read_text(encoding="utf-8"))
    return data


def _interp(x: float, points: Sequence[Sequence[float]], *, log_y: bool = False) -> float:
    """Piecewise linear through ``points`` (sorted by x), in log y when asked, held flat past either end."""
    pairs = [(float(a), math.log(b) if log_y else float(b)) for a, b in points]
    if x <= pairs[0][0]:
        y = pairs[0][1]
    elif x >= pairs[-1][0]:
        y = pairs[-1][1]
    else:
        y = next(
            y0 + (x - x0) / (x1 - x0) * (y1 - y0)
            for (x0, y0), (x1, y1) in zip(pairs, pairs[1:], strict=False)
            if x <= x1
        )
    return math.exp(y) if log_y else y


def _inverse(y: float, points: Sequence[Sequence[float]], *, log_y: bool = False) -> float:
    """The x at which the increasing ``points`` reach ``y``, held at either end."""
    return _interp(math.log(y) if log_y else y, [(math.log(b) if log_y else b, a) for a, b in points])


def _set(values: dict[str, ArturiaValue], key: str, normalized: float) -> None:
    values[key] = controls()[key].value(min(max(normalized, 0.0), 1.0))


def _matched(p: Mapping[str, Ms20Value], values: dict[str, ArturiaValue], tables: dict[str, Any],
             notices: list[str] | None) -> None:  # fmt: skip
    """Each knob-by-knob table between its measured positions; a time past MS-20 V's longest is clamped and said."""
    for key, table in tables.items():
        knob = float(p[table["knob"]])
        longest = table.get("longest_at")
        if longest is not None and knob > longest:
            limitation(notices, f"{table['what']} {knob:.1f} is longer than MS-20 V's longest, clamped")
        values[key] = _interp(knob, table["points"], log_y=table["log"])


def _pitch_depth(
    values: dict[str, ArturiaValue], key: str, knob: float, curves: dict[str, Any], what: str,
    notices: list[str] | None,
) -> None:  # fmt: skip
    cents = _interp(knob, curves["legacy"])
    top = curves["target"][-1][1]
    if cents > top * 1.02:
        limitation(notices, f"{what} sweeps {cents:.0f} cents, more than MS-20 V's {top:.0f}, clamped")
    _set(values, key, _inverse(min(cents, top), curves["target"]))


def _voices(p: Mapping[str, Ms20Value], values: dict[str, ArturiaValue], notices: list[str] | None) -> None:
    if p["Poly Mode"] == "Mono":
        values["Play Mode"] = "Mono Legato" if p["Trigger Mode"] == "Single" else "Mono Retrig"
        return
    voices = int(str(p["Poly Voices"]))
    if voices > 6:
        limitation(notices, f"{voices} voices are more than MS-20 V's 6; it plays 6")
    values["Play Mode"] = str(min(max(voices, 2), 6))


def _sync(p: Mapping[str, Ms20Value], values: dict[str, ArturiaValue], reported: list[str]) -> None:
    if p["MG Tempo Sync"] != "On":
        values["MG_MidiSync"] = "Free"
        return
    base = str(p["MG Base Note"])
    bars = _SYNC_BASE[base.rstrip("T")] * (2 / 3 if base.endswith("T") else 1.0) * int(str(p["MG Times"]))
    for kind, factor in _TARGET_TYPES.items():
        for item, length in _TARGET_BARS.items():
            if math.isclose(length * factor, bars, rel_tol=1e-9):
                values |= {"MG_MidiSync": "Sync", "MG_RateSynced": item, "MG_RateSyncedType": kind}
                return
    values["MG_MidiSync"] = "Free"
    reported.append(f"the MG synced to {base} x {p['MG Times']}, a length MS-20 V's sync does not offer")


def _transpose(p: Mapping[str, Ms20Value], values: dict[str, ArturiaValue], reported: list[str]) -> None:
    semitones = int(str(p["Transpose"]))
    octaves, rest = divmod(semitones, 12)
    shifted: dict[str, str] = {}
    for key in ("MS20VCO_ScaleOsc1", "MS20VCO_ScaleOsc2"):
        choices = controls()[key].choices
        index = choices.index(str(values[key])) + octaves
        if 0 <= index < len(choices):
            shifted[key] = choices[index]
    if rest or len(shifted) < 2:
        reported.append(f"transpose {semitones:+d} st (MS-20 V transposes by its VCO scales only, a whole octave each)")
        return
    values.update(shifted)


def plan(
    program: Ms20Program, notices: list[str] | None = None
) -> tuple[dict[str, ArturiaValue], list[tuple[str, str]], list[str]]:
    """MS-20 V's values and cables for ``program``, and what is reported as not carried.

    A setting MS-20 V cannot carry is refused without ``notices``, or carried as near as it can and said there.
    """
    p = program.values()
    data = _calibration()
    values: dict[str, ArturiaValue] = {"VCF_Type": "MKI"}
    reported: list[str] = []

    def knob(name: str) -> float:
        return float(p[name])

    # Same switch.
    for name, (key, items) in SAME_SWITCH.items():
        values[key] = items[str(p[name])]
    _voices(p, values, notices)
    _sync(p, values, reported)
    bend = round(knob("Pitch Bend Range"))
    if bend < 1:
        limitation(notices, f"pitch bend range {knob('Pitch Bend Range'):.2f} is below MS-20 V's 1 st, written as 1")
    values["Pitch Bend Range"] = min(max(bend, 1), 12)
    _transpose(p, values, reported)
    # Calibrated.
    values["MS20VCO_FineTuneOsc2"] = knob("VCO 2 Tune")
    cents = data["fine_tune_cents_per_unit"] * knob("Fine Tune") + data["pitch_offset_cents"]
    values["Master Tune"] = 440.0 * 2 ** (cents / 1200)
    volume = controls()["Master Volume"]
    level = data["volume_db_at_full"] + (20 * math.log10(knob("Volume") / _KNOB) if knob("Volume") > 0 else -math.inf)
    values["Master Volume"] = min(max(level, volume.low), volume.high)
    rate = controls()["MG_RateUnSynced"]
    intercept, slope = data["mg_hz_line"]
    hertz = intercept + slope * knob("MG Frequency")
    if hertz > rate.high:
        limitation(notices, f"MG frequency {hertz:.1f} Hz is above MS-20 V's {rate.high:g} Hz, clamped")
    values["MG_RateUnSynced"] = min(max(hertz, rate.low), rate.high)
    glide = data["glide_seconds"]
    _set(values, "Portamento_Time", _inverse(_interp(knob("Portamento Time"), glide["legacy"]), glide["target"]))
    depth = data["pitch_depth_cents"]
    _pitch_depth(values, "MS20VCO_FMAmount", knob("Pitch EG 1/Ext"), depth["eg"], "the EG1 pitch modulation", notices)
    _pitch_depth(
        values, "MS20VCO_FMTotalAmount", knob("Pitch MG/T.Ext"), depth["mg"], "the MG pitch modulation", notices
    )
    _matched(p, values, data["matched"], notices)
    # Rendered at a few points, same print, panel position.
    _set(values, "VCF_ResLp", knob("LPF Peak") / _KNOB)
    for name, key in SAME_PRINT.items():
        values[key] = knob(name)
    for name, key in PANEL.items():
        _set(values, key, knob(name) / _KNOB)
    # Patch cables by jack name.
    cables: list[tuple[str, str]] = []
    for cable in program.cables:
        source, destination = JACK_SOURCES.get(cable.out), JACK_DESTINATIONS.get(cable.to)
        if source is None or destination is None:
            reported.append(f"the cable from {cable.out} to {cable.to} (MS-20 V has no such jack)")
        else:
            cables.append((source, destination))
    # Reported.
    if knob("Analog"):
        reported.append(f"the analog drift ({knob('Analog'):.2f}; MS-20 V's dispersion stays at 0)")
    if knob("Pan"):
        reported.append(f"the pan ({knob('Pan'):+.2f})")
    if any(program.slot(slot) != 0.5 for slot in EXT_MOD_SLOTS):
        reported.append(
            f"the external modulation routes from {p['EXT.Mod.Src.1']} and {p['EXT.Mod.Src.2']} "
            "(MS-20 V has no such routing)"
        )
    if program.slot(41):
        reported.append("unison (its voice count is not measured, so MS-20 V plays without it)")
    unnamed = [
        name
        for name, initial in UNNAMED_INITIAL.items()
        if not (p[name] == initial or (type(initial) is float and math.isclose(float(p[name]), initial, abs_tol=0.05)))
    ]
    if unnamed:
        reported.append(f"{', '.join(unnamed)} (controls the plugin leaves unnamed)")
    return values, cables, reported


@functools.cache
def _base() -> Ms20VPatch:
    return Ms20VPatch.parse(baseline())


def _converted(program: Ms20Program, notices: list[str]) -> Converted:
    values, cables, reported = plan(program, notices)
    if reported:
        notices.append("Not carried: " + "; ".join(reported) + ".")
    return Converted(_base().with_values(values, cables).raw, tuple(notices))


_MEASURED = MeasuredState(StateRung.RE_ENCODE, (StateEvidence.HOSTED,), datetime.date(2026, 10, 29))


@converter("korg.ms-20", "arturia.korg-ms-20-v", _MEASURED)
def convert(patch: bytes, program: int | None = None) -> Converted:
    """An MS-20 bank's working program as an MS-20 V state. ``program`` is not used: the working program plays."""
    return _converted(Ms20Bank.parse(patch).working, [NOT_HEARD_NOTE, MEASURED_NOTE, RENDERED_NOTE, EXPECT_NOTE])


def _cell_notices(bank: LegacyCellBank, notices: list[str] | None) -> tuple[Ms20Program, list[str]]:
    cell = bank.working
    ms20 = [index for index, slot in enumerate(cell.slots) if slot.ms20 is not None]
    if not ms20:
        raise StateTransformError(
            f"Legacy Cell {cell.name!r} plays {' and '.join(cell.synths) or 'nothing'}, no MS-20: MS-20 V has "
            "nothing to carry"
        )
    first = cell.slots[ms20[0]]
    for index, slot in enumerate(cell.slots):
        if index != ms20[0] and slot.synth is not None:
            limitation(
                notices,
                f"the cell's {slot.synth} in slot {'AB'[index]} is not carried; only the MS-20 in slot "
                f"{'AB'[ms20[0]]} converts, so this device will sound different",
            )
    effects = [name for slot in cell.slots for name in slot.effects if name and name != "No Effect"]
    effects += [name for name in cell.master_effects if name and name != "No Effect"]
    reported = [f"the cell's effects ({', '.join(effects)})"] if effects else []
    reported.append("the cell's mixer and key zones (not decoded)")
    if first.ms20 is None:
        raise StateTransformError("a Legacy Cell MS-20 slot holds no program")
    return first.ms20, reported


@converter("korg.legacy-cell", "arturia.korg-ms-20-v", _MEASURED)
def convert_cell(patch: bytes, program: int | None = None) -> Converted:
    """A Legacy Cell bank's working cell, when it holds an MS-20, as an MS-20 V state; ``program`` is not used."""
    notices = [NOT_HEARD_NOTE, MEASURED_NOTE, RENDERED_NOTE, EXPECT_NOTE]
    source, cell_reported = _cell_notices(LegacyCellBank.parse(patch), notices)
    values, cables, reported = plan(source, notices)
    reported = cell_reported + reported
    notices.append("Not carried: " + "; ".join(reported) + ".")
    return Converted(_base().with_values(values, cables).raw, tuple(notices))
