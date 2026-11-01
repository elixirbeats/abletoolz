"""Ohmicide patches into Ohmicide[S], its successor, control by control in the units both print.

Ohmicide[S] keeps the original's four bands, their crossovers and every
per-band control, and adds a pre, a post and a crossover band, a filter per
band and modulation. A converted patch is the plugin's own "Neutral" program
with every control the original sets written over it, the crossover band's
split at its 20 Hz bottom so the four bands cover the spectrum as the
original's do. Each control goes one of these ways, and each was checked by
rendering the same noise, sweep and sine through the original (hosted x86,
headless) and through Ohmicide[S]:

* **the same algorithm** -- the original's 37 distortion types are Ohmicide[S]'s
  37 in the same order, several renamed (Porridge is Disto, Rectifier is Vshpe,
  Random Walk is BrwnNz, Freezer is TimeFr), and its 3x and Odd variants are
  Ohmicide[S]'s XXX and ODD families. The original packs None, the types, the
  3x and the Odd ones into one knob of 150 steps, 50 per family, the steps past
  a family's last type repeating it; Ohmicide[S] stores the type and the family
  apart. All 111 rendered alike: a median 0.05 dB apart in level, 0.4 dB in
  spectrum; Bounce, which keeps state, up to 3 dB, and a few slew and random
  types up to 4 dB on noise;
* **the same unit** -- the rest print the same quantity on both, and each is
  written as that quantity in the plain unit Ohmicide[S] stores: gains as
  linear factors, times in seconds, frequencies in hertz, percentages as
  fractions, the switches as 0 or 1. Rendered alike: shape, body, distortion
  gain, bias (its sub-bass and DC run up to 3 dB louder on Ohmicide[S]),
  alteration (Ohmicide[S]'s MOD), pan, volume, band dry/wet, mid/side, solo,
  mute, the feedback's amount, frequency and spread, master volume, input
  trim, bypass, stereo link and the gate (level within 0.3 dB on bursts). The
  tempo is written as the same BPM;
* **the same frequency, another slope** -- the crossovers sit at the same
  frequencies, but the original's filters cross at -3 dB and fall slowly
  where Ohmicide[S]'s cross at -6 dB and fall steeply: a tone half an octave
  past a crossover reaches the other band about 30 dB louder in the original.
  Nothing in Ohmicide[S] sets the slope, so a tone near a crossover drives a
  neighbouring band harder in the original than here. Rendered through
  multiband patches, noise, sweeps and drum-like bursts came out a median 0.5
  dB apart and at most 6 dB; a steady low tone up to 15 dB;
* **the same balance** -- the overall dry/wet mixes on an equal-power curve in
  the original and a linear one in Ohmicide[S] (both ease the wet in on a
  smoothstep), so it is written as the mix with the same dry-to-wet ratio and
  the master volume makes up the level: within 0.2 dB rendered;
* **carried at a cost** -- the original's tone stage and its pre-distortion
  have no counterpart, and are left out: at no tone shape and at the
  pre-distortion's -20 dB bottom the two plugins render alike. A master
  volume past +40 dB with its make-up is written at +40 dB. A direct call
  without ``notices`` refuses these instead.

Only the working settings convert; the original's own program bank does not.
Nothing here has been heard in Live.
"""

from __future__ import annotations

import datetime
import math

from abletoolz.plugins.converters import limitation
from abletoolz.plugins.state import ohmicide, ohmicide_s
from abletoolz.plugins.state.catalog import Converted, converter
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung

# Ohmicide[S] parameter indexes.
BYPASS, MIX, MASTER, TRIM, LINK = 0, 1, 2, 3, 4
SPLIT, UPPER, TEMPO = 97, (98, 99, 100), 217
_BAND_BASE, _BAND_STRIDE = 101, 29
FEEDBACK, TYPE, FAMILY, BIAS, PAN = 0, 6, 7, 8, 19
SWITCHES = {"Mid Side mode": 22, "Solo": 23, "Mute": 24}

_LOWEST_SPLIT_HZ = 20.0
# Ohmicide[S] holds the bottoms of the gain (-20 dB), attack (1 ms) and release (2 ms) ranges in single
# precision and moves a value below one up to it on load; a value at a bottom is written as that number.
_FLOORS = {5: 0.1000000014901161, 11: 0.001000000047497451, 12: 0.002000000094994903}
_MASTER_TOP = 100.0
_KNOB_STEPS = 150
_FAMILY_STEPS = 50
_TYPES = 37
FAMILIES = ("", "3x ", "Odd ")

NOTE = (
    "Converted control by control into Ohmicide[S]'s Neutral program, in the units both plugins print; every "
    "distortion type and each control rendered alike on both plugins. Ohmicide[S]'s crossovers are steeper, so "
    "a tone near a crossover drives the next band less. Not heard in Live: audition this device."
)


def band(number: int, offset: int) -> int:
    """The Ohmicide[S] index of a band's parameter, band 1 to 4."""
    return _BAND_BASE + _BAND_STRIDE * (number - 1) + offset


def distortion(stored: float) -> tuple[int, int]:
    """The distortion type (0 is None) and family (0 standard, 1 3x, 2 Odd) the original's type knob holds."""
    step = math.floor(stored * _KNOB_STEPS + 0.5)
    family = min(max(step - 1, 0) // _FAMILY_STEPS, len(FAMILIES) - 1)
    return min(step - _FAMILY_STEPS * family, _TYPES), family


def _db(decibels: float) -> float:
    return float(10 ** (decibels / 20))


def _rising(stored: float, ratio: float) -> float:
    """0 to 1 along the original's exponential curves: ``(ratio**v - 1) / (ratio - 1)``."""
    return float((ratio**stored - 1) / (ratio - 1))


def _smoothstep(x: float) -> float:
    return 3 * x * x - 2 * x**3


def _inverse_smoothstep(y: float) -> float:
    return 0.5 - math.sin(math.asin(1 - 2 * y) / 3)


def _switch(stored: float) -> float:
    return 1.0 if stored >= 0.5 else 0.0


def plan(chunk: bytes, notices: list[str] | None = None) -> dict[int, float]:
    """Ohmicide[S]'s plain values for an Ohmicide chunk, by parameter index.

    A setting carried at a cost is refused without ``notices``, or written as near as it goes and said there.
    """
    old = ohmicide.controls(chunk)
    out = {
        BYPASS: _switch(old["Bypass"]),
        TRIM: _db(80 * old["Input trim"] - 40),
        LINK: _switch(old["Stereo link"]),
        SPLIT: _LOWEST_SPLIT_HZ,
        TEMPO: 60 + 180 * old["Tempo"],
    }
    for number, index in enumerate(UPPER, start=1):
        out[index] = 20 * 1024 ** old[f"B{number} Upper Freq"]
    wet = _smoothstep(old["Dry/Wet mix"])
    dry = math.sqrt(1 - wet * wet)
    out[MIX] = _inverse_smoothstep(wet / (wet + dry))
    master = _MASTER_TOP * _rising(old["Master Vol"], 2.0**18) * (wet + dry)
    if master > _MASTER_TOP:
        limitation(notices, "the master volume with its dry/wet make-up is past Ohmicide[S]'s +40 dB; written there")
    out[MASTER] = min(master, _MASTER_TOP)
    for number in range(1, 5):
        prefix = f"B{number} "
        values = (
            old[f"{prefix}Fdbk Amnt"],
            16 * 64 ** old[f"{prefix}Fdbk Freq"],
            2 * old[f"{prefix}Fdbk Spread"] - 1,
            old[f"{prefix}Shape"],
            old[f"{prefix}Body"],
            _db(80 * old[f"{prefix}Disto Gain"] - 20),
        )
        out.update({band(number, FEEDBACK + offset): value for offset, value in enumerate(values)})
        kind, family = distortion(old[f"{prefix}Disto Type"])
        out[band(number, TYPE)] = float(kind)
        out[band(number, FAMILY)] = float(family)
        gate = (
            2 * old[f"{prefix}Disto Bias"] - 1,
            old[f"{prefix}Disto Alteration"],
            _rising(old[f"{prefix}Gate Threshold"], 64),
            (1 + 199 * _rising(old[f"{prefix}Gate Atk Time"], 16)) / 1000,
            (2 + 398 * _rising(old[f"{prefix}Gate Rls Time"], 16)) / 1000,
            2 * old[f"{prefix}Gate Amount"] - 1,
        )
        out.update({band(number, BIAS + offset): value for offset, value in enumerate(gate)})
        out[band(number, PAN)] = 2 * old[f"{prefix}Pan"] - 1
        out[band(number, PAN + 1)] = 10 * _rising(old[f"{prefix}Volume"], 100)
        out[band(number, PAN + 2)] = old[f"{prefix}Dry/Wet mix"]
        for name, offset in SWITCHES.items():
            out[band(number, offset)] = _switch(old[f"{prefix}{name}"])
        for offset, floor in _FLOORS.items():
            out[band(number, offset)] = max(out[band(number, offset)], floor)
    # The original prints both to a tenth of its unit; below that each reads as at rest.
    if old["Tone Shape"] >= 0.001:
        limitation(notices, f"the tone stage (shape {100 * old['Tone Shape']:.1f}%) has no Ohmicide[S] counterpart")
    if old["Pre Disto"] >= 0.000625:
        limitation(notices, f"the pre-distortion at {80 * old['Pre Disto'] - 20:.1f} dB has no Ohmicide[S] counterpart")
    return out


@converter(
    "ohm-force.ohmicide",
    "ohm-force.ohmicide-s",
    MeasuredState(StateRung.RE_ENCODE, (StateEvidence.HOSTED,), datetime.date(2026, 10, 31)),
)
def convert(patch: bytes, program: int | None = None) -> Converted:
    """An Ohmicide chunk as an Ohmicide[S] patch. ``program`` is not used: the working settings are the patch."""
    notices = [NOTE]
    values = plan(patch, notices)
    return Converted(ohmicide_s.with_settings(ohmicide_s.baseline(), values, name="Ohmicide"), tuple(notices))
