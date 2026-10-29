"""Pro-53 patches into Prophet-5 V, control by control, each by the best evidence there is.

Pro-53 is a 32-bit plugin no current Live loads, and Prophet-5 V is Arturia's
model of the same synth. The two engines share the front panel and little under
it, so a patch is rebuilt into the Prophet-5 V baseline (its init patch with every
macro removed, the state its map was measured on) from both plugins' maps, in
the units each one prints. Every Pro-53 control goes one of these ways, and each
converted device lists which:

* **calibrated** -- both engines rendered one control at a time on 2026-10-21 and
  matched (``data/pro53-to-prophet-5-v.json``): oscillator pitch, oscillator B's
  fine tune, master tune, cutoff inside 32 to 111 of 127, amp and filter
  envelope attack, decay and release (matched at the time each stage takes to
  reach half way; their curves differ in shape), and the filter envelope's
  Poly-Mod depth into oscillator A's pitch;
* **rendered at a few points** -- short noise clips on both on 2026-10-28
  (``coarse`` in the data): cutoff outside 32 to 111 by its fitted corner in
  hertz, resonance by the height of its peak, filter envelope amount by how far
  it moves the cutoff. The cutoff renders agree with the calibration where they
  overlap, which also checks it on the instrument build the sets hold;
* **same unit** -- both plugins print the same quantity: LFO rate in hertz,
  pulse width in percent;
* **same switch** -- the same front-panel switch on both: waveforms, sync,
  oscillator B's low and keyboard switches, the Poly-Mod and wheel
  destinations, the LFO shapes, unison;
* **matched by display** -- a ballpark: mixer levels and the Poly-Mod
  oscillator B amount as a fraction of their range, the sustains, the wheel's
  LFO/noise mix, and keyboard follow rounded to Prophet-5 V's on or off;
* **reported** -- the device converts without it and says so: the delay effect,
  the analog drift, velocity, the external input, the saved mod wheel and pitch
  bend, glide outside unison (where Pro-53's did not act between two notes),
  and master volume away from the one level the calibration matched;
* **refused** -- no counterpart, and it changes what the patch is: the
  high-pass filter mode, filter envelope invert, LFO MIDI sync, amp hold, LFO
  envelope trigger, the release switch turned off, and glide in unison
  (Prophet-5 V's glide did not follow its own display in the same renders).

The calibration rendered Pro-53's effect build; on the instrument the sets hold,
short renders on 2026-10-28 put amp attack and release half-way times within 4%
of it at two settings each, cutoff on its curve, and pitch within 2 cents.
Nothing here has been heard in Live.
"""

from __future__ import annotations

import datetime
import functools
import json
import math
import pathlib
from collections.abc import Callable, Sequence
from typing import Any

from abletoolz.plugins.state.catalog import Converted, converter
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.pro53 import CONTROL_BY_NAME, Pro53Bank, Pro53Value
from abletoolz.plugins.state.prophet_5_v import Prophet5Patch, Prophet5Value, baseline, controls

DATA = pathlib.Path(__file__).with_name("data")
_CUTOFF_MEASURED = (32, 111)
_PITCH_BEND = 69

# Pro-53 switch -> the same Prophet-5 V switch.
SAME_SWITCH = {
    "Oscillator A Sawtooth": "Prophet5VCO_SawOn1",
    "Oscillator A Pulse": "Prophet5VCO_SquareOn1",
    "Oscillator A Sync": "Prophet5VCO_HardSyncOn",
    "Oscillator B Sawtooth": "Prophet5VCO_SawOn2",
    "Oscillator B Triangle": "Prophet5VCO_TriangleOn2",
    "Oscillator B Pulse": "Prophet5VCO_SquareOn2",
    "Oscillator B Low Freq": "Prophet5VCO_LowFreqOn2",
    "Oscillator B Key Follow": "Prophet5VCO_KeyOn2",
    "PolyMod Dest Freq A": "Prophet5VCO_Osc1FMDestOn",
    "PolyMod Dest PWidth A": "Prophet5VCO_PW1FMDestOn",
    "PolyMod Dest Filter": "PolyModFilterDestOn",
    "LFO Sawtooth": "Prophet5Lfo_RampSwitch",
    "LFO Triangle": "Prophet5Lfo_TriSwitch",
    "LFO Pulse": "Prophet5Lfo_SqrSwitch",
    "WheelMod Dest Freq A": "Prophet5VCO_LFOFreqModOn1",
    "WheelMod Dest Freq B": "Prophet5VCO_LFOFreqModOn2",
    "WheelMod Dest PWidth A": "Prophet5VCO_LFOPWModOn1",
    "WheelMod Dest PWidth B": "Prophet5VCO_LFOPWModOn2",
    "WheelMod Dest Filter": "Prophet5VCF_LFOModOn",
    "Unisono Mode": "UnisonSwitch",
}

# Pro-53 percentage -> the Prophet-5 V control showing the same fraction of its range.
BY_DISPLAY = {
    "Mixer Oscillator A": "Prophet5VCO_Osc1Level",
    "Mixer Oscillator B": "Prophet5VCO_Osc2Level",
    "Mixer Noise": "Prophet5VCO_NoiseLevel",
    "PolyMod Source Osc B": "Prophet5VCO_Osc2FMAmount",
    "Filter Sustain": "VCF_Prophet5ADSR_Sustain",
    "Amplifier Sustain": "VCA_Prophet5ADSR_Sustain",
    "WheelMod LFO-Noise Mix": "Prophet5Lfo_SourceMix",
}

# Pro-53 controls the core has no counterpart for, and when a device uses one.
type Used = Callable[[Pro53Value], bool]
REPORTED: dict[str, tuple[Used, str]] = {
    "Delay Effect On": (lambda v: v is True, "the delay effect"),
    "Analog": (lambda v: v != 0, "the analog drift (Prophet-5 V's dispersion stays at its baseline)"),
    "Velocity on/off": (lambda v: v is True, "velocity"),
    "Mixer External Input": (lambda v: v != 0, "the external input"),
    "Modulation Wheel": (lambda v: v != 0, "the saved mod wheel position"),
}
REFUSED: dict[str, tuple[Used, str]] = {
    "Filter HPF-Mode": (lambda v: v is True, "the high-pass filter mode"),
    "Filter Envelope Invert": (lambda v: v is True, "filter envelope invert"),
    "LFO MIDI Sync": (lambda v: v is True, "LFO MIDI sync"),
    "Amplifier Hold": (lambda v: v is True, "amp hold"),
    "LFO Envelope Trigger": (lambda v: v is True, "LFO envelope trigger"),
    "Release on/off": (lambda v: v is False, "the release switch turned off"),
}

MEASURED_NOTE = (
    "Calibrated against both plugins: oscillator pitch and fine tune, master tune, cutoff, amp and filter "
    "envelope times (half-way times; the curves differ in shape) and the filter envelope's Poly-Mod pitch depth."
)
RENDERED_NOTE = (
    "Rendered at a few points: cutoff at its extremes, resonance and filter envelope amount. "
    "Matched by display, a ballpark: mixer levels, sustains, the Poly-Mod oscillator B amount, the wheel's "
    "LFO/noise mix and keyboard follow (rounded to on or off)."
)
NOT_HEARD_NOTE = "Pro-53 to Prophet-5 V has not been heard in Live: audition this device."


def _pro53_cutoff_hz(semitones: float) -> float:
    """Pro-53's cutoff in hertz, as fitted to its noise renders: exponential below, measured points at the top."""
    model = _coarse()["pro53_cutoff_hz"]
    top = model["measured_top"]
    if semitones <= top[0][0]:
        base, offset = model["model"]
        return float(base * 2 ** ((semitones - offset) / 12))
    return _interp(semitones, top)


@functools.cache
def _coarse() -> dict[str, Any]:
    data: dict[str, dict[str, Any]] = json.loads((DATA / "pro53-to-prophet-5-v.json").read_text(encoding="utf-8"))
    return data["coarse"]


@functools.cache
def _calibration() -> dict[str, Any]:
    data: dict[str, dict[str, Any]] = json.loads((DATA / "pro53-to-prophet-5-v.json").read_text(encoding="utf-8"))
    return data["calibration"]


def _interp(x: float, points: Sequence[Sequence[float]]) -> float:
    """Piecewise linear through ``points`` (sorted by x), held flat past either end."""
    if x <= points[0][0]:
        return float(points[0][1])
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        if x <= x1:
            return float(y0 + (x - x0) / (x1 - x0) * (y1 - y0))
    return float(points[-1][1])


def _clamp(u: float) -> float:
    return min(max(u, 0.0), 1.0)


def _half_time(fit: Sequence[float], x: float) -> float:
    """Seconds a Pro-53 envelope stage takes to reach half way at knob position ``x``, as fitted."""
    scale, rate, offset = fit
    return float(scale * math.exp(rate * x) + offset)


def _amp_stage(kind: str, x: float) -> float:
    model = _calibration()["amp_envelope"][kind]
    half = _half_time(model["source_half_time"], x)
    curve = sorted(model["target_half_times"])
    if model["log_time"]:
        # Release is interpolated on log time, as the calibration fitted it.
        return _interp(math.log(max(half, curve[0][1])), sorted((math.log(t), u) for u, t in curve))
    return _interp(half, sorted((t, u) for u, t in curve))


def _filter_stage(kind: str, x: float) -> float:
    model = _calibration()["filter_envelope"][kind]
    half = _half_time(model["source_half_time"], x)
    target = model["target"]
    if "table" in target:
        # Half time is affine in the seconds the plugin declares; the declared table is then inverted.
        declared = (half - target["offset_seconds"]) / target["slope"]
        table = target["table"]
        return _clamp(_interp(declared, [(seconds, k / (len(table) - 1)) for k, seconds in enumerate(table)]))
    return _clamp(1 + math.log(max(half, 1e-6) / model["target_max_half_time"]) / target["rate"])


def plan(bank: Pro53Bank) -> tuple[dict[str, Prophet5Value], list[str], list[str]]:
    """Prophet-5 V's values for ``bank``'s working patch, what is reported as not carried, and why it is refused.

    The values are by Prophet-5 V key in its units; a device with any refusal is not converted.
    """
    p = bank.values()
    calibration = _calibration()
    rendered = _coarse()
    target = controls()
    values: dict[str, Prophet5Value] = {}
    reported: list[str] = []
    refused: list[str] = []

    def knob(name: str) -> float:
        return bank.normalized(CONTROL_BY_NAME[name])

    # Measured.
    values["Prophet5VCO_Freq1"] = p["Oscillator A Frequency"]
    rows = calibration["osc_b_fine"]
    fine_cents = _interp(
        knob("Oscillator B Freq Fine"), [(r["q"] / 127, r["coarse"] * 100 + (r["fine"] - 0.5) * 120) for r in rows]
    )
    cents = int(p["Oscillator B Frequency"]) * 100 + fine_cents
    coarse = min(max(round(cents / 100), -24), 36)
    values["Prophet5VCO_Freq2"] = coarse
    values["Prophet5VCO_FineTune"] = min(max((cents - coarse * 100) / 100, -0.6), 0.6)
    reference_hz = target["MasterTuneFactor"].value(calibration["master_tune"])
    values["MasterTuneFactor"] = float(reference_hz) * 2 ** (float(p["Master Tune"]) / 1200)
    q = round(knob("Filter Cutoff") * 127)
    cutoff = target["Prophet5VCF_Freq"]
    if _CUTOFF_MEASURED[0] <= q <= _CUTOFF_MEASURED[1]:
        values["Prophet5VCF_Freq"] = cutoff.value(_interp(q, calibration["cutoff"]))
    else:
        hertz = _pro53_cutoff_hz(float(p["Filter Cutoff"]))
        values["Prophet5VCF_Freq"] = min(max(hertz, cutoff.low), cutoff.high)
        if hertz > cutoff.high:
            reported.append(f"cutoff above Prophet-5 V's {cutoff.high:g} Hz (Pro-53's measured {hertz:.0f} Hz)")
    for kind, name, key in (
        ("attack", "Amplifier Attack", "VCA_Prophet5ADSR_Attack"),
        ("decay", "Amplifier Decay", "VCA_Prophet5ADSR_Decay"),
        ("release", "Amplifier Release", "VCA_Prophet5ADSR_Release"),
    ):
        values[key] = target[key].value(_clamp(_amp_stage(kind, knob(name))))
    for kind, name, key in (
        ("attack", "Filter Attack", "VCF_Prophet5ADSR_Attack"),
        ("decay", "Filter Decay", "VCF_Prophet5ADSR_Decay"),
        ("release", "Filter Release", "VCF_Prophet5ADSR_Release"),
    ):
        values[key] = target[key].value(_filter_stage(kind, knob(name)))
    routes = [p["PolyMod Dest Freq A"], p["PolyMod Dest PWidth A"], p["PolyMod Dest Filter"]]
    if not any(routes) or p["PolyMod Source Filt Env"] == 0:
        values["PolyModFilterEnvAmount"] = 0.0
    elif routes == [True, False, False]:
        poly = calibration["poly_mod_envelope_pitch"]
        a, b, c = poly["source_ratio_quadratic"]
        x = knob("PolyMod Source Filt Env")
        pitch_cents = 1200 * math.log2(a * x * x + b * x + c)
        slope, intercept = poly["target_cents_line"]
        values["PolyModFilterEnvAmount"] = target["PolyModFilterEnvAmount"].value(
            _clamp((pitch_cents - intercept) / slope)
        )
    else:
        refused.append("Poly-Mod filter envelope depth into pulse width or the filter is not measured")
    # Same unit.
    rate = float(p["LFO Frequency"])
    lfo = target["Prophet5Lfo_RateUnSynced"]
    if lfo.low <= rate <= lfo.high:
        values["Prophet5Lfo_RateUnSynced"] = rate
    else:
        refused.append(f"LFO rate {rate:.3g} Hz is outside Prophet-5 V's {lfo.low:g}-{lfo.high:g} Hz")
    for name, key in (("Oscillator A PulseWidth", "Prophet5VCO_PW1"), ("Oscillator B PulseWidth", "Prophet5VCO_PW2")):
        width = float(p[name])
        if target[key].low <= width <= target[key].high:
            values[key] = width
        else:
            refused.append(f"{name.lower()} {width:.0f}% is below Prophet-5 V's {target[key].low:g}%")
    # Same switch.
    for name, key in SAME_SWITCH.items():
        values[key] = p[name]
    # Rendered at a few points.
    peaks = rendered["resonance_peak_db"]
    peak = _interp(float(p["Filter Resonance"]), peaks["pro53_percent"])
    values["Prophet5VCF_Res"] = _interp(peak, [(db, value) for value, db in peaks["prophet_value"]])
    shift = rendered["env_amount_shift_st"]
    moved = _interp(float(p["Filter Envelope Amount"]), shift["pro53_display_vs_measured"])
    values["Prophet5VCF_EnvAmt"] = _interp(moved, [(st, value) for value, st in shift["prophet_value"]])
    # Matched by display.
    for name, key in BY_DISPLAY.items():
        control = target[key]
        values[key] = control.low + float(p[name]) / 100 * (control.high - control.low)
    values["Prophet5VCF_KeyFolOn"] = float(p["Filter Keyboard Follow"]) >= 50
    values["Glider_Time"] = 0.0
    if p["Glide Time"] != 0:
        if p["Unisono Mode"]:
            refused.append("glide in unison, where Prophet-5 V's glide did not follow its display")
        else:
            reported.append(f"glide {p['Glide Time']:.0f}% (outside unison Pro-53's did not act between two notes)")
    if bank.sound[_PITCH_BEND] != 0.5:
        reported.append(f"the saved pitch bend ({bank.sound[_PITCH_BEND]:.2f}, centre is 0.50)")
    # Reported, refused.
    values["Volume"] = target["Volume"].value(calibration["master_volume"])
    if p["Master Volume"] != 50.0:
        reported.append(f"master volume {p['Master Volume']:.0f}% (written at the level matched to 50%)")
    reported += [what for name, (used, what) in REPORTED.items() if used(p[name])]
    refused += [what for name, (used, what) in REFUSED.items() if used(p[name])]
    return values, reported, refused


@converter(
    "ni.pro-53",
    "arturia.prophet-5-v",
    MeasuredState(StateRung.RE_ENCODE, (StateEvidence.HOSTED,), datetime.date(2026, 10, 28)),
)
def convert(patch: bytes, program: int | None = None) -> Converted:
    """A Pro-53 bank's working patch as a Prophet-5 V state. ``program`` is not used: the working patch plays."""
    values, reported, refused = plan(Pro53Bank.parse(patch))
    if refused:
        raise StateTransformError("Pro-53 to Prophet-5 V cannot carry " + "; ".join(refused))
    notices = [NOT_HEARD_NOTE, MEASURED_NOTE, RENDERED_NOTE]
    if reported:
        notices.append("Not carried: " + "; ".join(reported) + ".")
    return Converted(Prophet5Patch.parse(baseline()).with_values(values).raw, tuple(notices))
