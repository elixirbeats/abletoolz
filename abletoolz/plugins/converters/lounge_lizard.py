"""Lounge Lizard EP-4 patches into Keyscape: its sampled Rhodes, with the EP-4's tremolo, reverb and chorus.

Lounge Lizard EP-4 models an electric piano and Keyscape plays sampled ones,
so a patch cannot cross control by control: the piano itself becomes
Keyscape's factory "Rhodes - Classic" (the installed plugin's own state for it,
:func:`~abletoolz.plugins.state.keyscape.rhodes_classic`),
and what the EP-4 patch does around the piano is written onto that patch's
own controls. Each goes one of these ways:

* **calibrated** -- Keyscape rendered (``data/lounge-lizard-to-keyscape.json``,
  a held note's envelope fitted with one sine per render): tremolo rate, the
  EP-4's hertz written as the rate Keyscape's tremolo plays at (the smoothing
  beside it set as Spectrasonics' own Rhodes patches set it for that rate);
  tremolo depth, written so each channel's gain swings as many dB as the
  EP-4's depth dips it (measured at Keyscape's own 1.45 Hz; on the EP-4 side
  the usual meaning of a depth, gain between 1 and 1 - depth, not a render);
  reverb amount, the EP-4's mix written as the send whose wet level, against
  the dry note, is the mix's wet-to-dry ratio (Keyscape's send scales the wet
  signal linearly, -9.6 dB under the dry at full send; on the EP-4 side the
  assumption is a wet path of the same loudness as Keyscape's);
* **same switch** -- tremolo on or off; its Stereo setting as Keyscape's
  Auto-Pan and Mono as its plain tremolo; the reverb on (Keyscape's studio
  reverb) or off; a chorus or phaser in either effect slot as Keyscape's
  CHORUS 1 or PHASER 1 at the Rhodes patch's own amount and rate;
* **reported** -- everything that shapes the piano itself (mallet, fork,
  pickup, damper, the EQ, compressor, velocity curve and volume: the samples
  play instead), the tremolo's wave shape and tempo sync (written at its free rate),
  the reverb's size and decay, the delay, distortion, flanger, wah and notch
  effects, a second modulation effect, and the global tune.

The EP-4's 64-bit engine plays silence hosted on its own, so nothing has
compared the two plugins' sound; every converted device says so. Nothing here
has been heard in Live.
"""

from __future__ import annotations

import datetime
import functools
import json
import math
import pathlib

from abletoolz.plugins.converters import interp
from abletoolz.plugins.state import keyscape, lounge_lizard
from abletoolz.plugins.state.catalog import Converted, converter
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung

DATA = pathlib.Path(__file__).with_name("data") / "lounge-lizard-to-keyscape.json"

# Rhodes - Classic's controls, by number, and the items of its two choices.
TREMOLO, AUTO_PAN, DEPTH, SPEED, REVERB_TYPE, REVERB_AMOUNT, MODULATION_FX = 12, 3, 2, 39, 33, 25, 24
STUDIO, NO_REVERB = 1, 0
NO_MODULATION, CHORUS_1, PHASER_1 = 0, 1, 4
MODULATION_ITEMS = {"Chorus": CHORUS_1, "Phaser": PHASER_1}
OFF, ON = 1.0, 2.0  # the EP-4's switch items
TRIANGLE, STEREO = 1.0, 2.0
FULL_SEND_WET = 10 ** (-9.63 / 20)  # Keyscape's wet level against the dry note at full send, measured

NOTE = (
    "Rebuilt on Keyscape's factory Rhodes - Classic: Keyscape's sampled Rhodes plays in place of the EP-4's "
    "modelled one, with the EP-4 patch's tremolo, reverb and chorus or phaser carried onto it. The piano's own "
    "settings (mallet, fork, pickup, damper, EQ, compressor, velocity curve, volume) are not carried. "
    "Lounge Lizard EP-4 could not be hosted to compare the two, and this has not been heard in Live: "
    "audition this device."
)


@functools.cache
def _tables() -> dict[str, list[list[float]]]:
    tables: dict[str, list[list[float]]] = json.loads(DATA.read_text(encoding="utf-8"))
    return tables


def _value(old: dict[str, float | str], name: str) -> float:
    value = old.get(name)
    if not isinstance(value, float):
        raise StateTransformError(f"this {lounge_lizard.PRODUCT} patch holds no {name}: {value!r}")
    return value


def _switch(old: dict[str, float | str], name: str) -> bool:
    value = _value(old, name)
    if value not in (OFF, ON):
        raise StateTransformError(f"{name} holds {value:g}, not off (1) or on (2)")
    return value == ON


def _dips_db(depth: float) -> float:
    """How far a tremolo of ``depth`` dips the gain, in dB: the gain swings between 1 and 1 - depth."""
    return -20 * math.log10(max(1.0 - depth, 1e-3))


def _lookup(table: str, value: float, what: str, reported: list[str] | None) -> float:
    """``value`` through a calibration table; said in ``reported`` when past the measured span (held at its end)."""
    points = _tables()[table]
    if reported is not None and not points[0][0] <= value <= points[-1][0]:
        end = points[0] if value < points[0][0] else points[-1]
        reported.append(f"{what} {value:.3g}, past what Keyscape was measured to reach (written as {end[0]:.3g})")
    return interp(value, points)


def _send(mix: float, reported: list[str]) -> float:
    """The reverb send whose wet level against the dry note is ``mix``'s wet-to-dry ratio (a 0 to 1 crossfade)."""
    if mix <= 0:
        return 0.0
    send = mix / (1 - mix) / FULL_SEND_WET if mix < 1 else math.inf
    if send > 1:
        reported.append(f"the reverb mix {mix:.3g}, wetter than Keyscape's full send (written at full send)")
    return min(send, 1.0)


def plan(state: bytes, notices: list[str] | None = None) -> dict[str, float | int | bool]:
    """What the EP-4 patch sets on Keyscape's Rhodes - Classic, by control, and the notices for the rest."""
    old = lounge_lizard.values(state)
    out: dict[str, float | int | bool] = {}
    reported: list[str] = []

    out["tremolo"] = _switch(old, "Tremolo/On")
    said = reported if out["tremolo"] else None
    stereo = _value(old, "Tremolo/Stereo") == STEREO
    out["auto_pan"] = stereo
    hertz = _value(old, "Tremolo/Speed")
    out["rate"] = _lookup("rate_by_hertz", hertz, "the tremolo rate (Hz)", said)
    out["speed_knob"] = _lookup("speed_knob_by_rate", float(out["rate"]), "Keyscape's tremolo rate", None)
    swing = _dips_db(_value(old, "Tremolo/Depth"))
    out["swing"] = _lookup("swing_by_db_pan" if stereo else "swing_by_db", swing, "the tremolo swing (dB)", said)
    if out["tremolo"]:
        if _value(old, "Tremolo/Shape") == TRIANGLE:
            reported.append("the tremolo's triangle wave (Keyscape's own shape plays)")
        if _switch(old, "Tremolo/Sync On"):
            reported.append(f"the tremolo's tempo sync (it plays at its free rate, {hertz:.2f} Hz)")

    out["reverb"] = _switch(old, "Reverb/On")
    mix = _value(old, "Reverb/Mix")
    out["reverb_amount"] = _send(mix, reported) if out["reverb"] else 0.0
    if out["reverb"]:
        reported.append("the reverb's size and decay (Keyscape's studio reverb at the Rhodes patch's time)")

    out["modulation"] = NO_MODULATION
    for slot in ("Effect 1", "Effect 2"):
        if not _switch(old, f"{slot}/On"):
            continue
        kind = lounge_lizard.EFFECT_TYPES.get(int(_value(old, f"{slot}/Type")), "an unknown effect")
        item = MODULATION_ITEMS.get(kind)
        if item is None:
            reported.append(f"the {kind.lower()} in {slot.lower()}")
        elif out["modulation"] != NO_MODULATION:
            reported.append(f"a second modulation effect, the {kind.lower()} in {slot.lower()}")
        else:
            out["modulation"] = item
    tune = _value(old, "Global/Tune")
    if tune != 0:
        reported.append(f"the global tune ({tune:+g})")
    if reported:
        message = "Not carried: " + "; ".join(reported) + "."
        if notices is None:
            raise StateTransformError(message)
        notices.append(message)
    return out


def write(settings: dict[str, float | int | bool]) -> bytes:
    """Keyscape's Rhodes - Classic with ``settings`` (as :func:`plan` gives them) on its controls; the rest as it is."""
    patch = keyscape.rhodes_classic()
    if "tremolo" in settings:
        on = 1.0 if settings["tremolo"] else 0.0
        patch = keyscape.edit(patch, TREMOLO, lambda _a, points, _s: interp(on, points))
    if "auto_pan" in settings:
        pan = 1.0 if settings["auto_pan"] else 0.0
        patch = keyscape.edit(patch, AUTO_PAN, lambda _a, points, _s: interp(pan, points))
    if "swing" in settings:
        swing = float(settings["swing"])
        patch = keyscape.edit(patch, DEPTH, lambda _a, _p, _s: swing)
    if "rate" in settings:
        rate, knob = float(settings["rate"]), float(settings["speed_knob"])
        patch = keyscape.edit(patch, SPEED, lambda attribute, _p, _s: rate if attribute == "rate" else knob)
    if "reverb" in settings:
        patch = keyscape.choose(patch, REVERB_TYPE, STUDIO if settings["reverb"] else NO_REVERB)
        amount = float(settings["reverb_amount"])
        patch = keyscape.edit(patch, REVERB_AMOUNT, lambda _a, _p, _s: amount)
    if "modulation" in settings:
        patch = keyscape.choose(patch, MODULATION_FX, int(settings["modulation"]))
    return patch


@converter(
    "aas.lounge-lizard-ep-4",
    "spectrasonics.keyscape",
    MeasuredState(StateRung.RE_ENCODE, (StateEvidence.HOSTED,), datetime.date(2026, 10, 31)),
)
def convert(patch: bytes, program: int | None = None) -> Converted:
    """A Lounge Lizard EP-4 patch as Keyscape's Rhodes - Classic. ``program`` is not used: the state holds one patch."""
    notices = [NOTE]
    name = lounge_lizard.program_name(patch)
    if name:
        notices.insert(0, f"Lounge Lizard EP-4 program {name!r}.")
    return Converted(write(plan(patch, notices)), tuple(notices))


NOTE_3 = (
    "Rebuilt on Keyscape's factory Rhodes - Classic: Keyscape's sampled Rhodes plays in place of Lounge Lizard 3's "
    "modelled one. Only its tremolo and reverb are carried, switched as the patch has them, the tremolo's depth and "
    "the reverb's mix read as Lounge Lizard EP-4's (both are AAS modules; Lounge Lizard 3's own units were not "
    "measured), with Keyscape's own tremolo rate and stereo. Its effects slots, piano settings and tremolo rate are "
    "not carried. Lounge Lizard 3 could not be hosted, and this has not been heard in Live: audition this device."
)


def plan_3(patch: lounge_lizard.Lounge3Patch, reported: list[str]) -> dict[str, float | int | bool]:
    """A Lounge Lizard 3 working patch on Keyscape's Rhodes - Classic; what it cannot reach goes in ``reported``."""

    def number(key: str) -> float:
        try:
            value = float(patch.fields[key])
        except (KeyError, ValueError):
            raise StateTransformError(f"this {lounge_lizard.PRODUCT_3} patch holds no number {key}") from None
        if not math.isfinite(value):
            raise StateTransformError(f"{key} holds {value}")
        return value

    def switch(key: str) -> bool:
        value = number(key)
        if value not in (0.0, 1.0):
            raise StateTransformError(f"{key} holds {value:g}, not off (0) or on (1)")
        return value == 1.0

    stereo = bool(keyscape.knobs(keyscape.rhodes_classic())[AUTO_PAN])
    depth = number("tremolo:Tremolo/depth")
    reverb = switch("fx_wrapper:Reverb/on_off") and not switch("reverberator:Reverb_Module/bypass")
    mix = number("reverberator:Reverb_Module/mix")
    tremolo = switch("tremolo:Tremolo/on_off")
    swing = _lookup(
        "swing_by_db_pan" if stereo else "swing_by_db",
        _dips_db(depth),
        "the tremolo swing (dB)",
        reported if tremolo else None,
    )
    return {
        "tremolo": tremolo,
        "swing": swing,
        "reverb": reverb,
        "reverb_amount": _send(mix, reported) if reverb else 0.0,
    }


@converter(
    "aas.lounge-lizard-3",
    "spectrasonics.keyscape",
    MeasuredState(StateRung.RE_ENCODE, (StateEvidence.HOSTED,), datetime.date(2026, 10, 31)),
)
def convert_3(state: bytes, program: int | None = None) -> Converted:
    """A Lounge Lizard 3 state's working patch as Keyscape's Rhodes - Classic. ``program`` is not used."""
    patch = lounge_lizard.lounge3(state)
    notices = [NOTE_3]
    if patch.program:
        notices.insert(0, f"Lounge Lizard 3 program {patch.program!r}.")
    reported: list[str] = []
    written = write(plan_3(patch, reported))
    if reported:
        notices.append("Not carried: " + "; ".join(reported) + ".")
    return Converted(written, tuple(notices))
