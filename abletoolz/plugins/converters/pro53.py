"""Pro-53 patches into Prophet-5 V, control by control, each by the best evidence there is.

Pro-53 is a 32-bit plugin no current Live loads, and Prophet-5 V is Arturia's
model of the same synth. The two engines share the front panel and little under
it, so a patch is rebuilt into the Prophet-5 V baseline (its init patch with every
macro removed, the state its map was measured on) from both plugins' maps, in
the units each one prints. Every Pro-53 control goes one of these ways, and each
converted device lists which:

* **calibrated** -- both engines rendered one control at a time and matched
  (``data/pro53-to-prophet-5-v.json``): oscillator pitch, oscillator B's
  fine tune, master tune, amp attack and decay and the filter envelope's
  attack, decay and release (matched at the time each stage takes to reach
  half way; their curves differ in shape), and the filter envelope's Poly-Mod
  depth into oscillator A's pitch;
* **matched by renders** -- both plugins kept loaded, one control at a time
  from this converter's own output, each Pro-53 display set to the Prophet-5 V
  value that sounded nearest (``matched`` in the data): oscillator B and noise
  levels, cutoff from 22.7 to 113.4 st by the filter's response, filter
  envelope amount by the response it holds at full sustain, amp sustain by
  its held level and amp release by its whole curve. Prophet-5 V's noise at
  full is quieter than Pro-53's above 40%, so louder noise is written at full;
* **rendered at a few points** -- short clips rendered on both (``coarse`` in
  the data): cutoff outside the matched span by its fitted corner in hertz,
  resonance by the height of its peak. The delay goes to Prophet-5
  V's FX slot 1 delay from noise-burst echoes: its time and its feedback (a
  gain per repeat, the percent on Pro-53, the value on Prophet-5 V) are the
  same number on both, and both filter every repeat inside the loop; its mix
  keeps Pro-53's echo-to-dry balance (Pro-53 mixes on an equal-power curve,
  Prophet-5 V holds each side at full to 50%) and the master volume makes up
  the level; its low and high cut go over at their half-power corners
  (Pro-53's fall 6 dB per octave, Prophet-5 V's 12; a corner below Prophet-5
  V's 20 Hz is written there). Master volume: Pro-53's percent is a straight
  gain with 50% as unity, Prophet-5 V prints its gain in dB. The saved mod
  wheel is written as the same position, which sweeps pulse width as far on
  both (within 3 points of duty at five positions); it bends pitch half as
  far on Prophet-5 V, so a wheel routed to pitch alone is written at twice
  its position;
* **same unit** -- both plugins print the same quantity: LFO rate in hertz,
  pulse width in percent;
* **same switch** -- the same front-panel switch on both: waveforms, sync,
  oscillator B's low and keyboard switches, the Poly-Mod and wheel
  destinations, the LFO shapes, unison;
* **matched by display** -- a ballpark: oscillator A's level and the Poly-Mod
  oscillator B amount as a fraction of their range, the filter sustain, the
  wheel's LFO/noise mix, and keyboard follow rounded to Prophet-5 V's on or
  off;
* **reported** -- the device converts without it and says so: the delay's
  invert, its time modulation (depth, rate and Sync, which Prophet-5 V's
  delay does not have) and its MIDI sync, the analog drift, velocity (Pro-53's
  scales the amplifier and the filter envelope; Prophet-5 V's velocity routes
  reach no amplifier), the external input, the saved pitch bend, and glide
  outside unison (where Pro-53's did not act between two notes);
* **carried at a cost** -- no counterpart, and it changes what the patch is, so
  the device converts as near as it can and warns that it will likely sound
  different: the high-pass filter mode (Prophet-5 V's filter is low-pass only;
  it is written fully open), filter envelope invert (its amount is written at
  zero), the release switch turned off (both releases at their shortest), glide
  in unison (Pro-53's measured half-way time, though Prophet-5 V's glide did
  not follow its own display in the same renders), Poly-Mod depth into pulse
  width or the filter, the delay's spread (written as Prophet-5 V's stereo
  width, the right echo up to 20% of the time later, where Pro-53 splits each
  side's echo in two taps), a saved wheel that bends pitch along with other
  destinations (written at its own position, so the vibrato is shallower),
  settings outside Prophet-5 V's range (clamped), and LFO MIDI sync, amp hold
  and LFO envelope trigger (left out). A direct call without ``notices``
  refuses these instead.

The calibration rendered Pro-53's effect build; on the instrument build,
short renders put amp attack half-way times within 4% of it at two settings,
and pitch within 2 cents. Nothing here has been heard in Live.
"""

from __future__ import annotations

import datetime
import functools
import json
import math
import pathlib
from collections.abc import Callable, Sequence
from typing import Any

from abletoolz.plugins.converters import limitation
from abletoolz.plugins.state.catalog import Converted, converter
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.pro53 import CONTROL_BY_NAME, Pro53Bank, Pro53Value
from abletoolz.plugins.state.prophet_5_v import Prophet5Patch, Prophet5Value, baseline, controls

DATA = pathlib.Path(__file__).with_name("data")
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
    "PolyMod Source Osc B": "Prophet5VCO_Osc2FMAmount",
    "Filter Sustain": "VCF_Prophet5ADSR_Sustain",
    "WheelMod LFO-Noise Mix": "Prophet5Lfo_SourceMix",
}

# Pro-53 controls the core has no counterpart for, and when a device uses one.
type Used = Callable[[Pro53Value], bool]
REPORTED: dict[str, tuple[Used, str]] = {
    "Analog": (lambda v: v != 0, "the analog drift (Prophet-5 V's dispersion stays at its baseline)"),
    "Velocity on/off": (
        lambda v: v is True,
        "velocity (Pro-53's scales the amplifier and the filter envelope; Prophet-5 V's routes reach no amplifier)",
    ),
    "Mixer External Input": (lambda v: v != 0, "the external input"),
}
DELAY_REPORTED: dict[str, tuple[Used, str]] = {
    "Delay Effect Invert": (lambda v: v is True, "the delay's invert (Prophet-5 V's delay has no polarity switch)"),
    "Delay Effect MIDI Sync": (
        lambda v: v is True,
        "the delay's MIDI sync (Prophet-5 V's delay runs free at the time Pro-53's knob shows)",
    ),
}
LEFT_OUT = {
    "LFO MIDI Sync": "LFO MIDI sync has no measured match; the LFO runs free at the rate its knob shows",
    "Amplifier Hold": "amp hold has no match in Prophet-5 V; it is left out",
    "LFO Envelope Trigger": "LFO envelope trigger has no match in Prophet-5 V; it is left out",
}

MEASURED_NOTE = (
    "Calibrated against both plugins: oscillator pitch and fine tune, master tune, amp attack and decay and the "
    "filter envelope's times (half-way times; the curves differ in shape) and the filter envelope's Poly-Mod "
    "pitch depth. Matched by renders: oscillator B and noise levels, cutoff, filter envelope amount, amp sustain "
    "and amp release."
)
RENDERED_NOTE = (
    "Rendered at a few points: cutoff at its extremes, resonance, the delay (time, feedback, mix and cuts), "
    "master volume and the saved mod wheel. "
    "Matched by display, a ballpark: oscillator A's level, the filter sustain, the Poly-Mod oscillator B amount, "
    "the wheel's LFO/noise mix and keyboard follow (rounded to on or off)."
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


@functools.cache
def _matched() -> dict[str, Any]:
    data: dict[str, dict[str, Any]] = json.loads((DATA / "pro53-to-prophet-5-v.json").read_text(encoding="utf-8"))
    controls_: dict[str, Any] = data["matched"]["controls"]
    return controls_


def _match(key: str, shown: float) -> float:
    """Prophet-5 V's ``key`` at the Pro-53 display ``shown``, through its matched points."""
    return _interp(shown, _matched()[key]["points"])


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
    return _interp(half, sorted((t, u) for u, t in model["target_half_times"]))


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


def _cut_hz(points: Sequence[Sequence[float]], semitones: float) -> float:
    """A delay cut's half-power corner in hertz at ``semitones``: measured points, a semitone per step past them."""
    if semitones <= points[0][0]:
        return float(points[0][1] * 2 ** ((semitones - points[0][0]) / 12))
    if semitones >= points[-1][0]:
        return float(points[-1][1] * 2 ** ((semitones - points[-1][0]) / 12))
    return math.exp(_interp(semitones, [(st, math.log(hz)) for st, hz in points]))


def _wet(percent: float) -> tuple[float, float]:
    """Prophet-5 V's Dry/Wet holding Pro-53's echo-to-dry balance at ``percent``, and the gain in dB that levels it.

    Pro-53 mixes on an equal-power curve (dry the cosine, echo the sine of a quarter turn). Prophet-5 V keeps
    the dry at full up to 50% and the echo at full from 50%, each falling in a straight line to its end.
    """
    turn = percent / 100 * math.pi / 2
    dry, echo = math.cos(turn), math.sin(turn)
    if echo <= dry:
        return 50 * echo / dry, 20 * math.log10(dry)
    return 100 - 50 * dry / echo, 20 * math.log10(echo)


def _delay(
    p: dict[str, Pro53Value], values: dict[str, Prophet5Value], reported: list[str], notices: list[str] | None
) -> float:
    """Pro-53's delay into Prophet-5 V's FX slot 1 delay; the gain in dB the master volume adds to level its mix."""
    target = controls()
    delay = _coarse()["delay"]
    values["FX1_ModuleType"] = "Delay"
    values["FX1_Bypass"] = True
    time = target["FX1_Delay_DelayTimeMs"]
    values["FX1_Delay_DelayTimeMs"] = max(float(p["Delay Effect Time"]), time.low)
    if float(p["Delay Effect Time"]) < time.low:
        limitation(
            notices, f"delay time {p['Delay Effect Time']:.1f} ms is below Prophet-5 V's {time.low:g} ms, clamped"
        )
    values["FX1_Delay_Feedback"] = float(p["Delay Effect Feedback"]) / 100
    low_pass = target["FX1_Delay_LPFreq"]
    high_cut = _cut_hz(delay["high_cut_hz"], float(p["Delay Effect High Cut"]))
    values["FX1_Delay_LPFreq"] = min(max(high_cut, low_pass.low), low_pass.high)
    if high_cut < low_pass.low:
        limitation(notices, f"delay high cut {high_cut:.0f} Hz is below Prophet-5 V's {low_pass.low:g} Hz, clamped")
    high_pass = target["FX1_Delay_HPFreq"]
    values["FX1_Delay_HPFreq"] = min(
        max(_cut_hz(delay["low_cut_hz"], float(p["Delay Effect Low Cut"])), high_pass.low), high_pass.high
    )
    spread = float(p["Delay Effect Spread"])
    width = target["FX1_Delay_StereoWidth"]
    values["FX1_Delay_StereoWidth"] = min(spread / 100, width.high)
    if spread > 0:
        limitation(
            notices,
            f"delay spread {spread:.0f}% is written as Prophet-5 V's stereo width, the right echo "
            f"{100 * float(values['FX1_Delay_StereoWidth']):.0f}% of the delay time later (its widest is "
            f"{100 * width.high:g}%); Pro-53 splits each side's echo in two taps, so the stereo image will differ",
        )
    mix, gain = _wet(float(p["Delay Effect Wet"]))
    values["FX1_Dry_Wet"] = mix
    reported += [what for name, (used, what) in DELAY_REPORTED.items() if used(p[name])]
    if p["Delay Effect Depth"] != CONTROL_BY_NAME["Delay Effect Depth"].curve.value(0.0):
        synced = ", its Sync on" if p["Delay Effect Sync"] else ""
        reported.append(
            f"the delay's time modulation (depth {p['Delay Effect Depth']:.2g}, rate {p['Delay Effect Rate']:.3g} Hz"
            f"{synced}; Prophet-5 V's delay has none)"
        )
    return gain


def plan(bank: Pro53Bank, notices: list[str] | None = None) -> tuple[dict[str, Prophet5Value], list[str]]:
    """Prophet-5 V's values for ``bank``'s working patch, by key in its units, and what is reported as not carried.

    A setting Prophet-5 V cannot carry is refused without ``notices``, or carried as near as it can and said there.
    """
    p = bank.values()
    calibration = _calibration()
    rendered = _coarse()
    target = controls()
    values: dict[str, Prophet5Value] = {}
    reported: list[str] = []

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
    shown = float(p["Filter Cutoff"])
    matched = _matched()["Prophet5VCF_Freq"]["points"]
    cutoff = target["Prophet5VCF_Freq"]
    if p["Filter HPF-Mode"]:
        values["Prophet5VCF_Freq"] = cutoff.high
        limitation(
            notices,
            "the high-pass filter mode has no setting in Prophet-5 V, whose filter is low-pass only; it is written "
            "fully open, so this patch will likely sound different",
        )
    elif matched[0][0] <= shown <= matched[-1][0]:
        values["Prophet5VCF_Freq"] = _match("Prophet5VCF_Freq", shown)
    else:
        hertz = _pro53_cutoff_hz(shown)
        values["Prophet5VCF_Freq"] = min(max(hertz, cutoff.low), cutoff.high)
        if hertz > cutoff.high:
            reported.append(f"cutoff above Prophet-5 V's {cutoff.high:g} Hz (Pro-53's measured {hertz:.0f} Hz)")
    for kind, name, key in (
        ("attack", "Amplifier Attack", "VCA_Prophet5ADSR_Attack"),
        ("decay", "Amplifier Decay", "VCA_Prophet5ADSR_Decay"),
    ):
        values[key] = target[key].value(_clamp(_amp_stage(kind, knob(name))))
    # Matched by renders.
    for name, key in (
        ("Amplifier Release", "VCA_Prophet5ADSR_Release"),
        ("Amplifier Sustain", "VCA_Prophet5ADSR_Sustain"),
        ("Mixer Oscillator B", "Prophet5VCO_Osc2Level"),
        ("Mixer Noise", "Prophet5VCO_NoiseLevel"),
    ):
        values[key] = _match(key, float(p[name]))
    noise = _matched()["Prophet5VCO_NoiseLevel"]
    if float(p["Mixer Noise"]) > noise["loudest_at"]:
        short = _interp(float(p["Mixer Noise"]), noise["quieter_db"])
        limitation(
            notices,
            f"noise at {p['Mixer Noise']:.0f}% is louder than Prophet-5 V's noise goes; it is written at full, "
            f"about {short:.0f} dB quieter",
        )
    for kind, name, key in (
        ("attack", "Filter Attack", "VCF_Prophet5ADSR_Attack"),
        ("decay", "Filter Decay", "VCF_Prophet5ADSR_Decay"),
        ("release", "Filter Release", "VCF_Prophet5ADSR_Release"),
    ):
        values[key] = target[key].value(_filter_stage(kind, knob(name)))
    if p["Release on/off"] is False:
        for key in ("VCA_Prophet5ADSR_Release", "VCF_Prophet5ADSR_Release"):
            values[key] = target[key].low
        limitation(
            notices,
            "the release switch turned off has no setting in Prophet-5 V; both envelopes' releases are written at "
            "their shortest, which is not measured against Pro-53",
        )
    routes = [p["PolyMod Dest Freq A"], p["PolyMod Dest PWidth A"], p["PolyMod Dest Filter"]]
    if not any(routes) or p["PolyMod Source Filt Env"] == 0:
        values["PolyModFilterEnvAmount"] = 0.0
    elif routes[0]:
        poly = calibration["poly_mod_envelope_pitch"]
        a, b, c = poly["source_ratio_quadratic"]
        x = knob("PolyMod Source Filt Env")
        pitch_cents = 1200 * math.log2(a * x * x + b * x + c)
        slope, intercept = poly["target_cents_line"]
        values["PolyModFilterEnvAmount"] = target["PolyModFilterEnvAmount"].value(
            _clamp((pitch_cents - intercept) / slope)
        )
        if any(routes[1:]):
            limitation(notices, "Poly-Mod filter envelope depth into pulse width or the filter follows its pitch depth")
    else:
        poly_amount = target["PolyModFilterEnvAmount"]
        values["PolyModFilterEnvAmount"] = poly_amount.high * float(p["PolyMod Source Filt Env"]) / 100
        limitation(notices, "Poly-Mod filter envelope depth into pulse width or the filter is matched by display")
    # Same unit.
    rate = float(p["LFO Frequency"])
    lfo = target["Prophet5Lfo_RateUnSynced"]
    values["Prophet5Lfo_RateUnSynced"] = min(max(rate, lfo.low), lfo.high)
    if not lfo.low <= rate <= lfo.high:
        limitation(notices, f"LFO rate {rate:.3g} Hz is outside Prophet-5 V's {lfo.low:g}-{lfo.high:g} Hz, clamped")
    for name, key in (("Oscillator A PulseWidth", "Prophet5VCO_PW1"), ("Oscillator B PulseWidth", "Prophet5VCO_PW2")):
        width = float(p[name])
        values[key] = min(max(width, target[key].low), target[key].high)
        if not target[key].low <= width <= target[key].high:
            limitation(notices, f"{name.lower()} {width:.0f}% is below Prophet-5 V's {target[key].low:g}%, clamped")
    # Same switch.
    for name, key in SAME_SWITCH.items():
        values[key] = p[name]
    # Rendered at a few points.
    peaks = rendered["resonance_peak_db"]
    peak = _interp(float(p["Filter Resonance"]), peaks["pro53_percent"])
    values["Prophet5VCF_Res"] = _interp(peak, [(db, value) for value, db in peaks["prophet_value"]])
    values["Prophet5VCF_EnvAmt"] = _match("Prophet5VCF_EnvAmt", float(p["Filter Envelope Amount"]))
    if p["Filter Envelope Invert"]:
        values["Prophet5VCF_EnvAmt"] = 0.0
        limitation(
            notices,
            "filter envelope invert has no setting in Prophet-5 V, whose envelope amount is positive only; the "
            "amount is written at zero, so the cutoff stays where its knob is",
        )
    # Matched by display.
    for name, key in BY_DISPLAY.items():
        control = target[key]
        values[key] = control.low + float(p[name]) / 100 * (control.high - control.low)
    values["Prophet5VCF_KeyFolOn"] = float(p["Filter Keyboard Follow"]) >= 50
    values["Glider_Time"] = 0.0
    # The data holds knob displays to one decimal; 37.0% is 47 of 127, where glide had not begun.
    half = _interp(round(float(p["Glide Time"]), 1), rendered["glide_half_seconds_unison"]["pro53_percent"])
    if p["Glide Time"] != 0 and not p["Unisono Mode"]:
        reported.append(f"glide {p['Glide Time']:.0f}% (outside unison Pro-53's did not act between two notes)")
    elif p["Unisono Mode"] and half > 0:
        values["Glider_Time"] = min(half, target["Glider_Time"].high)
        limitation(
            notices,
            f"glide in unison is written as Prophet-5 V's glide at {half:.2f} s, Pro-53's measured half-way time, "
            "but Prophet-5 V's glide did not follow its own display in renders, so the slide will likely differ",
        )
    if bank.sound[_PITCH_BEND] != 0.5:
        reported.append(f"the saved pitch bend ({bank.sound[_PITCH_BEND]:.2f}, centre is 0.50)")
    # The saved wheel: the same position moves pulse width as far on both, pitch half as far on Prophet-5 V.
    wheel = float(p["Modulation Wheel"]) / 100
    others = any(p[f"WheelMod Dest {name}"] for name in ("PWidth A", "PWidth B", "Filter"))
    if wheel and (p["WheelMod Dest Freq A"] or p["WheelMod Dest Freq B"]):
        wheel *= 1 if others else 2
        if others or wheel > 1:
            limitation(
                notices,
                f"the saved mod wheel ({p['Modulation Wheel']:.0f}%) bends pitch half as far in Prophet-5 V; it is "
                f"written at {100 * min(wheel, 1):.0f}%, so the vibrato will be shallower",
            )
    values["VST3_CtrlModWheel"] = min(wheel, 1.0)
    # Rendered at a few points: the delay and the master volume.
    values["FX_Enable"] = p["Delay Effect On"]
    gain = _delay(p, values, reported, notices) if p["Delay Effect On"] else 0.0
    # Pro-53's master volume is a straight gain, 50% the unit; Prophet-5 V's prints its gain in dB.
    volume = target["Volume"]
    level = float(volume.value(calibration["master_volume"])) + gain
    master = float(p["Master Volume"])
    level += 20 * math.log10(master / 50) if master > 0 else -math.inf
    if level < volume.low:
        limitation(notices, f"master volume {master:.0f}% is below Prophet-5 V's {volume.low:g} dB, written there")
    values["Volume"] = min(max(level, volume.low), volume.high)
    # Reported, left out.
    reported += [what for name, (used, what) in REPORTED.items() if used(p[name])]
    for name, message in LEFT_OUT.items():
        if p[name]:
            limitation(notices, message)
    return values, reported


@converter(
    "ni.pro-53",
    "arturia.prophet-5-v",
    MeasuredState(StateRung.RE_ENCODE, (StateEvidence.HOSTED,), datetime.date(2026, 10, 29)),
)
def convert(patch: bytes, program: int | None = None) -> Converted:
    """A Pro-53 bank's working patch as a Prophet-5 V state. ``program`` is not used: the working patch plays."""
    notices = [NOT_HEARD_NOTE, MEASURED_NOTE, RENDERED_NOTE]
    values, reported = plan(Pro53Bank.parse(patch), notices)
    if reported:
        notices.append("Not carried: " + "; ".join(reported) + ".")
    return Converted(Prophet5Patch.parse(baseline()).with_values(values).raw, tuple(notices))
