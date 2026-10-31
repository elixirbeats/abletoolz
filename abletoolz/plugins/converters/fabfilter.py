"""The re-encodes from Pro-Q 1 and 2 into Pro-Q 3, and from the other older FabFilters into their successors.

Neither older Pro-Q migrates a patch into Pro-Q 3 by itself -- the host rig
asked Pro-Q 3 to read real patches of both and it refused -- so each is a
parser for the older bank and a serializer for Pro-Q 3's own chunk. The
formats themselves are FabFilter's and live in
:mod:`abletoolz.plugins.state.fabfilter`; this module only turns one into the
other.
"""

from __future__ import annotations

import datetime
import functools
import gzip
import json
import math
import pathlib
import re
from collections.abc import Callable, Sequence
from typing import Any

from abletoolz.plugins.converters import interp, limitation
from abletoolz.plugins.state.catalog import Converted, converter
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.fabfilter import (
    FFBS_VERSION,
    PRO_Q1_BAND_SLOTS,
    PRO_Q1_BANDS,
    PRO_Q1_GLOBALS,
    PRO_Q1_PARAMETERS,
    PRO_Q2_BAND_SLOTS,
    PRO_Q2_PARAMETERS,
    PRO_Q3_BAND_SLOTS,
    PRO_Q3_BANDS,
    PRO_Q3_DEFAULT_PARAMETERS,
    PRO_Q3_EDITOR_MAGIC,
    PRO_Q3_STATE_VERSION,
    SATURN_2_EDITOR_MAGIC,
    TIMELESS_3_EDITOR_MAGIC,
    VOLCANO_3_EDITOR_MAGIC,
    FfbsState,
    Q1BandField,
    Q3BandField,
    default_editor_state,
)
from abletoolz.plugins.state.fxbk import LegacyBank
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung

_DATA = pathlib.Path(__file__).with_name("data")

# Pro-Q 1's frequency knob is logarithmic over its full 10 Hz to 30 kHz range, so
# a normalized 0.57519 is 10 * 3000 ** 0.57519 Hz. Cross-checked against Pro-Q 3,
# which stores the same default as log2 999.99.
_Q1_FREQUENCY_FLOOR_HZ = 10.0
_Q1_FREQUENCY_DECADES = 3000.0

# Gain is linear and centred: 0.5 is 0 dB and the ends are -30 and +30.
_Q1_GAIN_SPAN_DB = 60.0
_Q1_GAIN_CENTRE = 0.5


# -- and how one becomes the other ------------------------------------------

# The band shapes each version offers, in the order it numbers them. Pro-Q 3
# added four and reordered nothing, so the map is a name lookup rather than a
# table of numbers -- a reordering would fail at import instead of quietly
# turning every low cut into a notch.
_Q1_SHAPES = ("Bell", "Low Shelf", "Low Cut", "High Shelf", "High Cut")
_Q3_SHAPES = (
    "Bell",
    "Low Shelf",
    "Low Cut",
    "High Shelf",
    "High Cut",
    "Notch",
    "Band Pass",
    "Tilt Shelf",
    "Flat Tilt",
)
_Q1_TO_Q3_SHAPE: tuple[int, ...] = tuple(_Q3_SHAPES.index(shape) for shape in _Q1_SHAPES)

# The cut slopes, likewise. Pro-Q 1 offers four of the nine Pro-Q 3 offers, so
# its slope 2 (24 dB/oct) is Pro-Q 3's slope 3, not its slope 2.
_Q1_SLOPES_DB = (6, 12, 24, 48)
_Q3_SLOPES_DB = (6, 12, 18, 24, 30, 36, 48, 72, 96)
_Q1_TO_Q3_SLOPE: tuple[int, ...] = tuple(_Q3_SLOPES_DB.index(slope) for slope in _Q1_SLOPES_DB)

# Pro-Q 3 numbers its placements Left, Right, Stereo, Mid, Side.
_Q3_STEREO_PLACEMENT = 2.0

# The globals' settings, in the order each version numbers them: Pro-Q 1's as the
# original plugin prints them, Pro-Q 3's as the Pro-Q 2 -> 3 map writes them.
# Pro-Q 1's Process Mode is Zero Latency, then "Linear Phase - <this> Latency";
# Pro-Q 3 splits that into a mode (Zero Latency, Natural Phase, Linear Phase) and
# a resolution, joined here by the resolution's name.
_Q1_LINEAR_PHASE_RESOLUTIONS = ("Low", "Medium", "High", "Maximum")
_Q3_RESOLUTIONS = ("Low", "Medium", "High", "Very High", "Maximum")
_Q1_TO_Q3_RESOLUTION = tuple(_Q3_RESOLUTIONS.index(name) for name in _Q1_LINEAR_PHASE_RESOLUTIONS)
_Q3_LINEAR_PHASE = 2.0
_Q1_DISPLAY_RANGES_DB = (6, 12, 30)
_Q3_DISPLAY_RANGES_DB = (3, 6, 12, 30)
_Q1_TO_Q3_DISPLAY_RANGE = tuple(_Q3_DISPLAY_RANGES_DB.index(db) for db in _Q1_DISPLAY_RANGES_DB)
# Pro-Q 1's Analyzer is Off, Pre-EQ or Post-EQ; Pro-Q 3 has a show switch for each.
_Q1_ANALYZER_TO_Q3_PRE_POST = ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0))

# Both versions run 10 Hz to 30 kHz and ±30 dB -- Pro-Q 3 says so itself, and its
# floor and both gain ends round-trip exactly. The ceiling is the one place they
# disagree, and only in the last two bits: log2 30000 rounded to float32 is
# 14.872674942016602, and what Pro-Q 3 stores for its own top frequency is this,
# 0.02 Hz lower. Measured by handing it three real patches with a band at the
# top of the range and watching it hand back its own number instead. Clamping
# keeps an exact readback meaningful: unclamped, those three patches fail it over
# a difference no one could hear.
PRO_Q3_MAX_FREQUENCY_LOG2 = 14.872673988342285


def _enum_index(normalized: float, choices: tuple[int, ...], field: str) -> int:
    """Read a normalized enum slot as one of ``choices``, or refuse it.

    Live stores an enum as the index over its top index, so a five-way shape is
    0, 0.25, 0.5, 0.75 or 1. Rounding recovers the index; a value that rounds
    outside the range is a bank this parser has misread, and continuing would
    write a band nobody asked for.
    """
    index = round(normalized * (len(choices) - 1))
    if not 0 <= index < len(choices):
        raise StateTransformError(f"a Pro-Q 1 {field} of {normalized} is not one of its {len(choices)} settings")
    return choices[index]


def _setting(value: float, count: int) -> int:
    """A normalized enum as its index; both plugins round half up, so 0.5 of two is the second."""
    return math.floor(value * (count - 1) + 0.5)


def pro_q1_to_pro_q3_parameters(bank: Sequence[float], notices: list[str] | None = None) -> tuple[float, ...]:
    """Remap Pro-Q 1's normalized parameter bank onto Pro-Q 3's plugin-unit floats.

    Slot 0 of the bank is the used-band count over 24; each band that count
    covers is read out of its seven slots and written into Pro-Q 3's thirteen.
    The eight globals after the bands each land on their Pro-Q 3 counterpart.
    Bands past the count, and every Pro-Q 3 field Pro-Q 1 has no counterpart for,
    keep :data:`PRO_Q3_DEFAULT_PARAMETERS`. A band placed off stereo is refused,
    or with ``notices`` written stereo and said there.
    """
    if len(bank) != PRO_Q1_PARAMETERS:
        raise StateTransformError(
            f"a Pro-Q 1 bank has {PRO_Q1_PARAMETERS} parameters, not {len(bank)}. "
            "Another FabFilter's bank read as one would land on a patch nobody chose."
        )
    if any(not math.isfinite(value) or not 0 <= value <= 1 for value in bank):
        raise StateTransformError("Pro-Q 1 parameters must be finite normalized values between 0 and 1")
    used = round(bank[0] * PRO_Q1_BANDS)
    if not 0 <= used <= PRO_Q1_BANDS:
        raise StateTransformError(f"a Pro-Q 1 bank claiming {used} of {PRO_Q1_BANDS} bands is not readable")

    parameters = list(PRO_Q3_DEFAULT_PARAMETERS)
    for band in range(used):
        start = 1 + band * PRO_Q1_BAND_SLOTS
        source = bank[start : start + PRO_Q1_BAND_SLOTS]
        if source[Q1BandField.PLACEMENT] != 1.0:
            limitation(
                notices, f"band {band + 1}'s left, right, mid or side placement has no measured match; it is stereo"
            )
        target = band * PRO_Q3_BAND_SLOTS
        frequency = _Q1_FREQUENCY_FLOOR_HZ * _Q1_FREQUENCY_DECADES ** source[Q1BandField.FREQUENCY]
        parameters[target + Q3BandField.USED] = 1.0
        parameters[target + Q3BandField.ENABLED] = source[Q1BandField.ENABLED]
        parameters[target + Q3BandField.FREQUENCY] = min(math.log2(frequency), PRO_Q3_MAX_FREQUENCY_LOG2)
        parameters[target + Q3BandField.GAIN] = (source[Q1BandField.GAIN] - _Q1_GAIN_CENTRE) * _Q1_GAIN_SPAN_DB
        # Both versions normalize Q the same way; confirmed in Live with the rest.
        parameters[target + Q3BandField.Q] = source[Q1BandField.Q]
        parameters[target + Q3BandField.SHAPE] = _enum_index(source[Q1BandField.SHAPE], _Q1_TO_Q3_SHAPE, "shape")
        parameters[target + Q3BandField.SLOPE] = _enum_index(source[Q1BandField.SLOPE], _Q1_TO_Q3_SLOPE, "slope")
        parameters[target + Q3BandField.PLACEMENT] = _Q3_STEREO_PLACEMENT

    globals_ = bank[PRO_Q1_PARAMETERS - PRO_Q1_GLOBALS :]
    gain, pan, display_range, process_mode, channel_mode, bypass, receive_midi, analyzer = globals_
    # Output Gain prints Pro-Q 2's Output Level curve point for point, so it is
    # carried the way the Pro-Q 2 map carries that.
    parameters[315] = gain * 2.0 - 1.0
    # Pro-Q 1 prints the pan position, -1 to 1, which is what Pro-Q 3 stores.
    parameters[316] = pan * 2.0 - 1.0
    parameters[331] = float(_Q1_TO_Q3_DISPLAY_RANGE[_setting(display_range, len(_Q1_DISPLAY_RANGES_DB))])
    mode = _setting(process_mode, 1 + len(_Q1_LINEAR_PHASE_RESOLUTIONS))
    if mode:
        parameters[312] = _Q3_LINEAR_PHASE
        parameters[313] = float(_Q1_TO_Q3_RESOLUTION[mode - 1])
    parameters[317] = float(_setting(channel_mode, 2))
    parameters[318] = float(_setting(bypass, 2))
    parameters[332] = float(_setting(receive_midi, 2))
    parameters[321], parameters[322] = _Q1_ANALYZER_TO_Q3_PRE_POST[_setting(analyzer, 3)]
    return tuple(parameters)


def _pro_q3_chunk(bank: LegacyBank, parameters: tuple[float, ...]) -> bytes:
    """Pro-Q 3's own patch: its floats, then an editor state carrying the bank's preset name."""
    editor = default_editor_state(PRO_Q3_EDITOR_MAGIC, bank.preset_name)
    return FfbsState(version=PRO_Q3_STATE_VERSION, parameters=parameters, tail=editor.encode()).encode()


@converter(
    "fabfilter.pro-q-1",
    "fabfilter.pro-q-3",
    MeasuredState(StateRung.RE_ENCODE, (StateEvidence.EAR, StateEvidence.HOSTED), datetime.date(2026, 8, 13)),
)
def pro_q1_to_pro_q3(patch: bytes, program: int | None = None) -> Converted:
    """A Pro-Q 1 host bank as Pro-Q 3's own patch, its VST2 chunk.

    Opened in Live 12 on 2026-08-09 with the bands where the Pro-Q 1 had them,
    and read back by Pro-Q 3 in the host rig on 2026-08-13.
    """
    bank = LegacyBank.parse(patch)
    notices: list[str] = []
    return Converted(_pro_q3_chunk(bank, pro_q1_to_pro_q3_parameters(bank.parameters, notices)), tuple(notices))


def pro_q2_to_pro_q3_parameters(bank: Sequence[float], notices: list[str] | None = None) -> tuple[float, ...]:
    """Convert a measured Pro-Q 2 bank; an unresolved monitoring mode left on is refused, or with ``notices`` left off.

    Source enumeration and sweeps use the recovered native x64 Pro-Q 2 DLL.
    Thirteen wrapped-device buffers restored all 191 source floats unchanged.
    Left/right placement becomes mid/side when the source Channel Mode is on;
    Pro-Q 3 stores that choice per band and for the output pan. The source's
    pan text still says left/right; stereo renders confirm the coupled mode.
    Automation indices need a separate map.
    """
    if len(bank) != PRO_Q2_PARAMETERS:
        raise StateTransformError(f"a Pro-Q 2 bank has {PRO_Q2_PARAMETERS} parameters, not {len(bank)}")
    if any(not math.isfinite(value) or not 0 <= value <= 1 for value in bank):
        raise StateTransformError("Pro-Q 2 parameters must be finite normalized values between 0 and 1")
    for index, name in ((176, "Auto Gain"), (179, "Analyzer Side-chain"), (188, "Solo Band"), (190, "Ex Auto Gain")):
        if bank[index] != 0:
            limitation(notices, f"{name} was on, and has no measured match in Pro-Q 3; it is off")

    parameters = list(PRO_Q3_DEFAULT_PARAMETERS)
    mid_side = _setting(bank[170], 2)
    for band in range(PRO_Q3_BANDS):
        source = bank[band * PRO_Q2_BAND_SLOTS : (band + 1) * PRO_Q2_BAND_SLOTS]
        target = band * PRO_Q3_BAND_SLOTS
        state = _setting(source[0], 3)
        placement = _setting(source[6], 3)
        parameters[target + Q3BandField.USED] = float(state != 2)
        parameters[target + Q3BandField.ENABLED] = float(state != 0)
        parameters[target + Q3BandField.FREQUENCY] = min(
            math.log2(10.0) + source[1] * math.log2(3000.0), PRO_Q3_MAX_FREQUENCY_LOG2
        )
        parameters[target + Q3BandField.GAIN] = 60.0 * source[2] - 30.0
        parameters[target + Q3BandField.Q] = source[3]
        parameters[target + Q3BandField.SHAPE] = float(_setting(source[4], 8))
        parameters[target + Q3BandField.SLOPE] = float(_setting(source[5], 9))
        parameters[target + Q3BandField.PLACEMENT] = float(placement + 3 if mid_side and placement < 2 else placement)

    for source_index, target_index, count in (
        (168, 312, 3),
        (169, 313, 5),
        (174, 318, 2),
        (175, 319, 2),
        (177, 321, 2),
        (178, 322, 2),
        (180, 324, 3),
        (181, 325, 4),
        (182, 326, 5),
        (183, 327, 5),
        (184, 328, 2),
        (185, 330, 2),
        (186, 331, 4),
        (187, 332, 2),
    ):
        parameters[target_index] = float(_setting(bank[source_index], count))
    parameters[314] = bank[171] * 2.0
    parameters[315] = bank[172] * 2.0 - 1.0
    parameters[316] = bank[173] * 2.0 - 1.0
    parameters[317] = float(mid_side)
    parameters[333] = bank[189] * 40.0 - 20.0
    return tuple(parameters)


@converter(
    "fabfilter.pro-q-2",
    "fabfilter.pro-q-3",
    MeasuredState(StateRung.RE_ENCODE, (StateEvidence.HOSTED,), datetime.date(2026, 10, 20)),
)
def pro_q2_to_pro_q3(patch: bytes, program: int | None = None) -> Converted:
    """A Pro-Q 2 host bank as Pro-Q 3's own patch, its VST2 chunk."""
    bank = LegacyBank.parse(patch)
    notices: list[str] = []
    return Converted(_pro_q3_chunk(bank, pro_q2_to_pro_q3_parameters(bank.parameters, notices)), tuple(notices))


# -- older FabFilters into their successors, by what both print -------------
#
# Pro-C and Pro-L are 32-bit plugins no current Live loads. Each converts into its successor control by control:
# the table under data/ holds what both plugins print at every knob step, the originals hosted on the legacy desktop,
# and each control is written where the successor prints what the original did.


@functools.cache
def _table(name: str) -> dict[str, Any]:
    raw = (_DATA / name).read_bytes()
    data: dict[str, Any] = json.loads(gzip.decompress(raw) if name.endswith(".gz") else raw)
    for pairing in data.get("numbers", {}).values():
        for side in ("source", "target"):
            pairing[side] = [-math.inf if value == "-inf" else float(value) for value in pairing[side]]
    return data


def _printed(table: Sequence[float], normalized: float) -> float:
    """What a plugin prints, or stores, at ``normalized``, between the two knob steps around it."""
    position = min(max(normalized, 0.0), 1.0) * (len(table) - 1)
    k = min(int(position), len(table) - 2)
    low, high = table[k], table[k + 1]
    if math.isinf(low):
        return low if position - k < 0.5 else high
    return low + (position - k) * (high - low)


def _knob(table: Sequence[float], value: float) -> float:
    """The knob position at which a plugin prints ``value``; held at either end."""
    if value <= table[0]:
        return 0.0
    for k in range(len(table) - 1):
        low, high = table[k], table[k + 1]
        if value <= high:
            fraction = 1.0 if math.isinf(low) else (value - low) / (high - low)
            return (k + fraction) / (len(table) - 1)
    return 1.0


def _past_end(notices: list[str] | None, name: str, ms: float, successor: str, longest: float) -> None:
    if ms > longest * 1.0001:
        limitation(notices, f"{name.lower()} {ms:.0f} ms is longer than {successor}'s {longest:.0f} ms, written there")


def _by_print(
    table: str, product: str, successor: str, bank: Sequence[float], notices: list[str] | None
) -> tuple[dict[str, float], dict[str, float]]:
    """The older product's controls by name, and its successor's written where each prints the same."""
    data = _table(table)
    names = data["source_controls"]
    if len(bank) != len(names):
        raise StateTransformError(f"a {product} bank holds {len(names)} controls, not {len(bank)}")
    source = dict(zip(names, bank, strict=True))
    out = dict(zip(data["target_controls"], data["target_defaults"], strict=True))
    for name, pairing in data["numbers"].items():
        value = _printed(pairing["source"], source[name])
        target = pairing["target"]
        if name in data["warn_past_end"]:
            _past_end(notices, name, value, successor, target[-1])
        out[pairing["to"]] = _knob(target, value)
    for name, pairing in data["choices"].items():
        label = pairing["source"][round(min(max(source[name], 0.0), 1.0) * 127)]
        out[pairing["to"]] = pairing["target"][label]
    for name, to in data["positions"].items():
        out[to] = source[name]
    out |= data["set"]
    return source, out


type _Parameters = Callable[[Sequence[float], list[str]], tuple[float, ...]]


def _rebanked(patch: bytes, notices: list[str], parameters: _Parameters) -> Converted:
    bank = LegacyBank.parse(patch)
    return Converted(LegacyBank(bank.preset_name, parameters(bank.parameters, notices)).encode(), tuple(notices))


def _ffbs_converted(patch: bytes, magic: bytes, notices: list[str], parameters: _Parameters) -> Converted:
    """A host bank as its FFBS successor's own patch, the VST2 chunk, keeping the preset name."""
    bank = LegacyBank.parse(patch)
    editor = default_editor_state(magic, bank.preset_name).encode()
    return Converted(FfbsState(FFBS_VERSION, parameters(bank.parameters, notices), editor).encode(), tuple(notices))


_BY_PRINT_MEASURED = MeasuredState(StateRung.RE_ENCODE, (StateEvidence.HOSTED,), datetime.date(2026, 10, 30))

PRO_C_NOT_HEARD_NOTE = "Pro-C to Pro-C 2 has not been heard in Live: audition this device."
PRO_C_MEASURED_NOTE = (
    "Matched by what both plugins print at every knob step: threshold, ratio, attack, release, the levels, the "
    "side-chain level and filters, the auto-release speed, the style and the switches. Pro-C's soft knee is "
    "written as Pro-C 2's default 18 dB knee and its side-chain filters at Pro-C 2's default 24 dB/oct slope, "
    "a ballpark."
)


def pro_c1_to_pro_c2_parameters(bank: Sequence[float], notices: list[str] | None = None) -> tuple[float, ...]:
    """Pro-C 2's 46 normalized controls for Pro-C 1's 31, each by what both plugins print.

    A setting past Pro-C 2's range is refused without ``notices``, or written at its end and said there.
    """
    source, out = _by_print("pro-c-1-to-pro-c-2.json", "Pro-C", "Pro-C 2", bank, notices)
    if source["Knee Shape"] >= 0.5:  # Hard Knee
        out["Knee"] = 0.0
    # Pro-C has no switch for its side-chain filters: one is in circuit once moved off its open end.
    out["Side Chain Low Enabled"] = float(source["High Pass Frequency"] > 0.0)
    out["Side Chain High Enabled"] = float(source["Low Pass Frequency"] < 1.0)
    if source["Auto Release"] >= 0.5:
        # With Auto Release on, Pro-C 2's release knob is the auto-release speed, 0 to 100% in a straight line, where
        # Pro-C keeps a speed knob of its own, 0 to 1: the same position.
        out["Release"] = source["Auto Release Speed"]
    if source["Channel Processing"] >= 0.5:
        limitation(notices, "Pro-C's Mid-Side processing has no match in Pro-C 2; it compresses left and right")
    if source["Left Side Chain Mix"] != 0.5 or source["Right Side Chain Mix"] != 0.5:
        limitation(notices, "Pro-C's side-chain mixes have no match in Pro-C 2 and are left out")
    if source["Right Side Chain Level"] != source["Left Side Chain Level"]:
        limitation(notices, "Pro-C 2 has one side-chain level; the left one is written")
    if source["Dry Panning"] != 0.5 and source["Dry Level"] > 0.0:
        limitation(notices, "Pro-C's dry panning is left and right, Pro-C 2's is mid and side; it is left centred")
    return tuple(out.values())


@converter("fabfilter.pro-c-1", "fabfilter.pro-c-2", _BY_PRINT_MEASURED)
def pro_c1_to_pro_c2(patch: bytes, program: int | None = None) -> Converted:
    """A Pro-C host bank as Pro-C 2's, its VST2 bank."""
    return _rebanked(patch, [PRO_C_NOT_HEARD_NOTE, PRO_C_MEASURED_NOTE], pro_c1_to_pro_c2_parameters)


@converter("fabfilter.pro-c-1-sc", "fabfilter.pro-c-2", _BY_PRINT_MEASURED)
def pro_c1_sc_to_pro_c2(patch: bytes, program: int | None = None) -> Converted:
    """Pro-C's side-chain build, whose controls print as Pro-C's, as Pro-C 2's VST2 bank."""
    return _rebanked(patch, [PRO_C_NOT_HEARD_NOTE, PRO_C_MEASURED_NOTE], pro_c1_to_pro_c2_parameters)


PRO_L_NOT_HEARD_NOTE = "Pro-L to Pro-L 2 has not been heard in Live: audition this device."
PRO_L_MEASURED_NOTE = (
    "Matched by what both plugins print at every knob step: gain, lookahead, attack, release, the stereo links, "
    "the output level, the style, oversampling, dithering and the switches. Pro-L limits sample peaks, so Pro-L 2's "
    "true-peak limiting is written off."
)


def pro_l1_to_pro_l2_parameters(bank: Sequence[float], notices: list[str] | None = None) -> tuple[float, ...]:
    """Pro-L 2's 32 normalized controls for Pro-L 1's 19, each by what both plugins print."""
    _, out = _by_print("pro-l-1-to-pro-l-2.json", "Pro-L", "Pro-L 2", bank, notices)
    return tuple(out.values())


@converter("fabfilter.pro-l-1", "fabfilter.pro-l-2", _BY_PRINT_MEASURED)
def pro_l1_to_pro_l2(patch: bytes, program: int | None = None) -> Converted:
    """A Pro-L host bank as Pro-L 2's, its VST2 bank."""
    return _rebanked(patch, [PRO_L_NOT_HEARD_NOTE, PRO_L_MEASURED_NOTE], pro_l1_to_pro_l2_parameters)


# -- older FabFilters into an FFBS successor, in its own stored units ---------
#
# Saturn 2 keeps a chunk of floats in its own units, so the table under data/ also holds the float it stores at every
# knob step, and each control is written as the float Saturn 2 keeps where it prints what the original did. FabFilter's
# own importer, fed the original's factory presets, settled the rest: which list items match, and what Saturn 2's new
# controls are set to.


def _stored_by_print(
    table: str, product: str, successor: str, bank: Sequence[float], notices: list[str] | None
) -> tuple[dict[str, float], dict[str, float]]:
    """The older product's controls by name, and the successor's stored floats, each where it prints the same."""
    data = _table(table)
    names = data["source_controls"]
    if len(bank) != len(names):
        raise StateTransformError(f"a {product} bank holds {len(names)} controls, not {len(bank)}")
    source = dict(zip(names, bank, strict=True))
    stored = data["tables"]
    out = dict(zip(data["target_controls"], data["target_defaults"], strict=True))
    for name, (older, at) in data["positions"].items():
        out[name] = _printed(stored[at], source[older])
    for name, pairing in data["numbers"].items():
        value = _printed(pairing["source"], source[pairing["from"]])
        target = pairing["target"]
        _past_end(notices, pairing["from"], value, successor, target[-1])
        out[name] = _printed(stored[pairing["table"]], _knob(target, value))
    for name, pairing in data["choices"].items():
        items = pairing["stored"]
        out[name] = items[_item(source[pairing["from"]], len(items))]
    out |= data["set"]
    return source, out


def _older_envelope_curves(source: dict[str, float], out: dict[str, float]) -> None:
    """The successor's envelopes have slopes; the importer gives each one in use the older envelope's fixed curve.

    An envelope not in use and still at the older default threshold gets the successor's default instead.
    """
    active = _item(source["Num Active EGs"], 7)
    for envelope in range(1, active + 1):
        out[f"EG {envelope} Attack Slope"] = 0.2
        out[f"EG {envelope} Decay Slope"] = out[f"EG {envelope} Release Slope"] = 0.6
    for envelope in range(active + 1, 7):
        if math.isclose(source[f"EG {envelope} Threshold"], _OLDER_EG_THRESHOLD, abs_tol=1e-6):
            out[f"EG {envelope} Threshold"] = _NEWER_EG_THRESHOLD


_OLDER_EG_THRESHOLD = 0.3
_NEWER_EG_THRESHOLD = 0.27976304292678833


def _item(normalized: float, count: int) -> int:
    """Which of ``count`` list items a normalized control holds: the older FabFilters round."""
    return round(min(max(normalized, 0.0), 1.0) * (count - 1))


SATURN_NOT_HEARD_NOTE = "Saturn to Saturn 2 has not been heard in Live: audition this device."
SATURN_MEASURED_NOTE = (
    "Matched control by control in Saturn 2's own stored units: the knob position where both print alike, the "
    "envelope times by what both print, every list by its label, and Saturn 2's new controls as FabFilter's own "
    "importer sets them."
)


def saturn1_to_saturn2_parameters(bank: Sequence[float], notices: list[str] | None = None) -> tuple[float, ...]:
    """Saturn 2's 951 stored floats for Saturn's 899 normalized controls."""
    source, out = _stored_by_print("saturn-1-to-saturn-2.json.gz", "Saturn", "Saturn 2", bank, notices)
    _older_envelope_curves(source, out)
    for slot in range(1, round(out["Num Active Slots"]) + 1):
        target = _SATURN_GAINS.get(out[f"Slot {slot} Target"])
        driver = next(kind for first, kind in _SATURN_SOURCES if out[f"Slot {slot} Source"] >= first)
        if target and driver:
            inverted, depth = bool(out[f"Slot {slot} Inverted"]), out[f"Slot {slot} Level"]
            out[target], out[f"Slot {slot} Level"], silent = _older_gain_depth(driver, inverted, depth, out[target])
            if silent:
                limitation(
                    notices, f"slot {slot} swings {target} through silence; written as FabFilter's importer does"
                )
    return tuple(out.values())


# Saturn 2's stored modulation targets that are gains, and the first stored source of each kind.
_SATURN_GAINS = {1.0: "Input Gain", 3.0: "Output Gain"}
_SATURN_SOURCES = ((29.0, "MIDI"), (25.0, "EF"), (19.0, "EG"), (13.0, "XLFO"), (1.0, "XY"), (0.0, ""))


@converter("fabfilter.saturn-1", "fabfilter.saturn-2", _BY_PRINT_MEASURED)
def saturn1_to_saturn2(patch: bytes, program: int | None = None) -> Converted:
    """A Saturn host bank as Saturn 2's own patch, its VST2 chunk."""
    notices = [SATURN_NOT_HEARD_NOTE, SATURN_MEASURED_NOTE]
    return _ffbs_converted(patch, SATURN_2_EDITOR_MAGIC, notices, saturn1_to_saturn2_parameters)


VOLCANO_TABLE = "volcano-2-to-volcano-3.json.gz"
VOLCANO_NOT_HEARD_NOTE = "Volcano 2 to Volcano 3 has not been heard in Live: audition this device."
VOLCANO_MEASURED_NOTE = (
    "Matched control by control in Volcano 3's own stored units, as Saturn is, with each filter's characteristic and "
    "response as Volcano 3's style and shape, its filters laid out and its modulation depths scaled as FabFilter's "
    "own importer does."
)

# What FabFilter's importer multiplies a printed modulation depth by, for an XLFO or envelope driving a filter's peak or
# pan: measured exact over every such slot in the factory presets it imported. Envelope followers and MIDI keep theirs.
_VOLCANO_DEPTH = {"Peak": 0.4, "Pan": 0.9}
# Targets whose depth the importer was seen to carry unchanged from every source; any other is said where it is used.
_VOLCANO_DEPTH_KEPT = ("Filter {} Freq", "Filter {} Delay", "Mix", "Input Pan", "Output Pan", "Slot {} Level")


def _depth(knob: float) -> float:
    """What a FabFilter modulation depth knob prints, 0 to 1."""
    return interp(knob, _DEPTH_CURVE)


def _depth_knob(printed: float) -> float:
    return interp(printed, [(y, x) for x, y in _DEPTH_CURVE])


# Piecewise linear through these corners: every one of the 128 readouts of the knob, and every depth FabFilter's
# importer wrote for synthetic presets, lands on it.
_DEPTH_CURVE = ((0.0, 0.0), (0.1, 0.002), (0.25, 0.05), (0.5, 0.16), (0.7, 0.36), (0.9, 0.91), (1.0, 1.0))


def _older_gain_depth(driver: str, inverted: bool, depth: float, gain: float) -> tuple[float, float, bool]:
    """A slot's depth on a gain in dB, and that gain, as FabFilter's importer rewrites them; and whether it fell silent.

    An XLFO, MIDI or XY source keeps its gain and gets ten times the depth. An envelope follower or envelope swung the
    older gain in linear amplitude; the importer draws a straight line in dB through that amplitude's size at 30% and
    80% of the source's travel, its value at none the gain and its slope the depth, none when the slope runs against
    the slot. An inverted swing past silence still gets that line, which no longer follows it.
    """
    if driver not in _GAIN_SWING:
        return gain, _depth_knob(min(_GAIN_DEPTH_TIMES * _depth(depth), 1.0)), False
    swing = _GAIN_SWING[driver] * _depth(depth) * (-1.0 if inverted else 1.0)
    low, high = (max(abs(10 ** (gain / 20) + swing * at), 1e-12) for at in _GAIN_FIT)
    slope = 20 * (math.log10(high) - math.log10(low)) / (_GAIN_FIT[1] - _GAIN_FIT[0])
    level = max(-36.0, min(36.0, 20 * math.log10(low) - slope * _GAIN_FIT[0]))
    knob = _depth_knob(min(max(-slope if inverted else slope, 0.0) / _GAIN_DB_PER_DEPTH, 1.0))
    return level, knob, 10 ** (gain / 20) + swing * _GAIN_FIT[1] < 0


# Measured with FabFilter's importers on synthetic Volcano 2 and Timeless 2 presets, one slot on a gain: an envelope's
# linear swing at full depth (36 dB), an envelope follower's twice that, where the importer samples the swing, the dB
# a printed depth of 1 spans in Volcano 3, and what it multiplies any other source's depth by.
_GAIN_SWING = {"EG": 10 ** (36 / 20), "EF": 2 * 10 ** (36 / 20)}
_GAIN_FIT = (0.3, 0.8)
_GAIN_DB_PER_DEPTH = 72.0
_GAIN_DEPTH_TIMES = 10.0


def volcano2_to_volcano3_parameters(bank: Sequence[float], notices: list[str] | None = None) -> tuple[float, ...]:
    """Volcano 3's 896 stored floats for Volcano 2's 666 normalized controls.

    Volcano 2 counts its active filters; Volcano 3 marks each one used, and in a split routing (per channel or
    mid/side) with four filters it holds Volcano 2's second and third the other way round.
    """
    source, out = _stored_by_print(VOLCANO_TABLE, "Volcano 2", "Volcano 3", bank, notices)
    _older_envelope_curves(source, out)
    modulation = _table(VOLCANO_TABLE)["modulation"]
    filters = _item(source["Num Active Filters"], 4) + 1
    split = _item(source["Routing Mode"], 3) != 0
    if split and filters == 3:
        limitation(notices, "a split routing of three filters is not measured; its filters are written in order")
    used = (1, 1, 0, 0) if split and filters < 3 else tuple(int(f < filters) for f in range(4))
    for f, on in enumerate(used, 1):
        out[f"Filter {f} Used"] = float(on)
    swapped = split and filters == 4
    if swapped:
        for name in [name for name in out if name.startswith("Filter 2 ")]:
            other = "Filter 3 " + name.removeprefix("Filter 2 ")
            out[name], out[other] = out[other], out[name]
    new_targets = modulation["new_targets"]
    for slot in range(1, _item(source["Num Active Slots"], 25) + 1):
        depth = source[f"Slot {slot} Level"]
        target = modulation["targets"][_item(source[f"Slot {slot} Target"], len(modulation["targets"]))]
        kind = re.sub(r"\d+", "{}", target)
        if swapped and target[:9] in ("Filter 2 ", "Filter 3 "):
            moved = ("Filter 3 " if target[7] == "2" else "Filter 2 ") + target[9:]
            out[f"Slot {slot} Target"] = float(new_targets.index(moved))
        driver = modulation["sources"][_item(source[f"Slot {slot} Source"], len(modulation["sources"]))].split(" ")[0]
        part = target.rsplit(" ", 1)[-1]
        if target.startswith("Filter ") and part in _VOLCANO_DEPTH and driver in ("XLFO", "EG"):
            out[f"Slot {slot} Level"] = _depth_knob(_VOLCANO_DEPTH[part] * _depth(depth))
        elif target in ("Input Gain", "Output Gain") and driver in ("XLFO", "MIDI", "XY", "EF", "EG"):
            inverted = bool(_item(source[f"Slot {slot} Inverted"], 2))
            out[target], out[f"Slot {slot} Level"], silent = _older_gain_depth(driver, inverted, depth, out[target])
            if silent:
                limitation(
                    notices, f"slot {slot} swings {target} through silence; written as FabFilter's importer does"
                )
        elif depth > 0 and target != "< Target >" and kind not in _VOLCANO_DEPTH_KEPT and part not in _VOLCANO_DEPTH:
            limitation(notices, f"slot {slot}'s depth on {target} is kept; how Volcano 3 rescales it is unmeasured")
    return tuple(out.values())


@converter("fabfilter.volcano-2", "fabfilter.volcano-3", _BY_PRINT_MEASURED)
def volcano2_to_volcano3(patch: bytes, program: int | None = None) -> Converted:
    """A Volcano 2 host bank as Volcano 3's own patch, its VST2 chunk."""
    notices = [VOLCANO_NOT_HEARD_NOTE, VOLCANO_MEASURED_NOTE]
    return _ffbs_converted(patch, VOLCANO_3_EDITOR_MAGIC, notices, volcano2_to_volcano3_parameters)


TIMELESS_TABLE = "timeless-2-to-timeless-3.json.gz"
TIMELESS_NOT_HEARD_NOTE = "Timeless 2 to Timeless 3 has not been heard in Live: audition this device."
TIMELESS_MEASURED_NOTE = (
    "Written as FabFilter's own importer writes Timeless 2's presets into Timeless 3, learned from the 408 factory "
    "presets it imported: the two delays folded into one with a time pan, their feedback into one feedback and cross "
    "mix, the modulation slots and modulators it keeps, and its own map of envelope times."
)
_TIMELESS_MODULATORS = {
    "XLFO": "Num Active XLFOs",
    "EG": "Num Active EGs",
    "EF": "Num Active EFs",
    "MIDI": "Num Active MIDI Sources",
}
# Where a Timeless 2 modulation target lives in Timeless 3. Delay 2's, both delay pans and the dry level have no home
# there, so the importer drops those slots.
_TIMELESS_TARGETS = {
    "Delay 1 Time": "Delay Time",
    "Delay 1 Offset": "Delay Offset",
    "Delay 1 Feedback": "Feedback",
    "Delay 1 Cross Feedback": "Feedback Mix",
    "Dry Pan": "Input Pan",
} | {f"XLFO {x} Offset": f"XLFO {x} Freq Offset" for x in range(1, 7)}
_TIMELESS_RENAMED = {"Delay Read Mode": "Delay Buffer Read Style"} | {
    f"Filter {x} {new}": f"Filter {x} {old}"
    for x in (1, 2)
    for new, old in (("Style", "Characteristic"), ("Shape", "Response"))
}
_TIMELESS_MODULE = re.compile(r"(XLFO|EG|EF|MIDI Source|Slot) \d|Num Active (XLFO|EG|EF|MIDI|Slots)")
# Timeless 3's free-running XLFOs stop short of Timeless 2's fastest; the importer holds them there.
_TIMELESS_FASTEST_XLFO = 0.98


@functools.cache
def _timeless_table() -> dict[str, Any]:
    data = _table(TIMELESS_TABLE)
    data["level_db"] = [-math.inf if value is None else value for value in data["level_db"]]
    return data


def _timeless_units(bank: Sequence[float]) -> dict[str, float]:
    """Timeless 2's knob positions in its own units, as its presets hold them."""
    data = _timeless_table()
    names = data["source_controls"]
    if len(bank) != len(names):
        raise StateTransformError(f"a Timeless 2 bank holds {len(names)} controls, not {len(bank)}")
    units = {}
    for name, normalized in zip(names, bank, strict=True):
        low, high, steps = data["ranges"][name]
        units[name] = low + (_item(normalized, round(high - low) + 1) if steps else normalized * (high - low))
    return units


def _timeless_delays(old: dict[str, float], out: dict[str, float]) -> None:
    """Two delay lines into Timeless 3's one, with a time pan between its left and right."""
    t1, off1, s1, t2, off2, s2 = (old[f"Delay {d} {part}"] for d in (1, 2) for part in ("Time", "Offset", "Sync"))
    log2_ms = _timeless_table()["delay_log2_ms"]
    if old["Delay Link"] or (s1 == 0) != (s2 == 0):  # linked, or one synced and one free: delay 1 as it is
        sync, offset, time, pan = s1, off1, t1, 0.0
    elif s1 == 0:
        sync, offset, time, pan = 0.0, off1, min(t1, t2), _printed(log2_ms, t2) - _printed(log2_ms, t1)
    else:  # a note length per sync item, scaled by its offset: the shorter delay is the base
        l1, l2 = off1 - s1, off2 - s2
        sync, offset = (s1, off1) if l1 <= l2 else (s2, off2)
        time, pan = t1, l1 - l2
    out |= {"Delay Sync": sync, "Delay Offset": offset, "Delay Time": time, "Delay Time Pan": max(-2.0, min(2.0, pan))}


def _timeless_feedback(old: dict[str, float], out: dict[str, float]) -> None:
    """Each delay's own and cross feedback, averaged into one feedback and the share of it that crosses over."""
    data = _timeless_table()
    f1, x1 = old["Delay 1 Feedback"], old["Delay 1 Cross Feedback"]
    f2, x2 = (f1, x1) if old["Feedback Link"] else (old["Delay 2 Feedback"], old["Delay 2 Cross Feedback"])
    own = min((_printed(data["feedback"], f1) + _printed(data["feedback"], f2)) / 2, 1.0)
    cross = (_printed(data["cross_feedback"], x1) + _printed(data["cross_feedback"], x2)) / 2
    out["Feedback"] = own + cross
    out["Feedback Cross Mix"] = cross / (own + cross) if own + cross else 0.0
    out["Feedback Invert Left"] = old["Delay 1 Feedback Invert"]
    out["Feedback Invert Right"] = old["Delay 2 Feedback Invert"]
    out["Stereo Width"] = abs(old["Delay 1 Pan"] - old["Delay 2 Pan"]) / 2


def _timeless_levels(old: dict[str, float], out: dict[str, float]) -> None:
    """The dry level as Timeless 3's mix; the input gain, which drove only Timeless 2's delays, kept on the wet side."""
    level_db = _timeless_table()["level_db"]
    dry = 10 ** (_printed(level_db, old["Dry Level"]) / 20) if old["Dry Enabled"] and old["Dry Level"] > 0 else 0.0
    gain = old["Input Level"]
    out |= {"Mix": 1 - dry, "Input Level": gain, "Output Level": -gain}
    wet = old["Wet Level"]
    out["Wet Level"] = _knob(level_db, _printed(level_db, wet) + gain) if wet > 0 else 0.0


def _timeless_control(old: dict[str, float], older: str, name: str) -> float:
    """One Timeless 2 control in the Timeless 3 control's stored units."""
    data = _timeless_table()
    value = old[older]
    part = name.rsplit(" ", 1)[-1]
    if re.fullmatch(r"EG \d (Attack|Decay|Release)", name):
        return interp(value, data["envelope"][part])
    if re.fullmatch(r"EG \d (Hold|Delay)", name):  # shortcut: too few imported presets use these to learn the map
        before, after = data["envelope_seconds"][part]
        return _knob(after, _printed(before, value))
    if re.fullmatch(r"Filter \d Freq", name):
        scale, offset = data["filter_freq"]
        return float(scale * value + offset)
    if re.fullmatch(r"XLFO \d Frequency", name) and not old[older.replace("Frequency", "Sync Mode")]:
        return min(value, _TIMELESS_FASTEST_XLFO)
    return value


def _timeless_modulation(old: dict[str, float], out: dict[str, float], notices: list[str] | None) -> None:
    """The slots Timeless 3 can hold, sorted by source then target, on the modulators still in use."""
    data = _timeless_table()
    new_sources, new_targets = data["new_sources"], data["new_targets"]
    kept: dict[int, list[Any]] = {}
    for slot in range(1, round(old["Num Active Slots"]) + 1):
        source = data["sources"][round(old[f"Slot {slot} Source"])]
        target = data["targets"][round(old[f"Slot {slot} Target"])]
        target = _TIMELESS_TARGETS.get(target, target)
        if source in new_sources and (target in new_targets or target.startswith("Slot ")):
            settings = (old[f"Slot {slot} {part}"] for part in ("Level", "Bypassed", "Inverted"))
            kept[slot] = [source, *settings, target]
    while True:  # a slot on a dropped slot's depth, or on a modulator nothing drives now, goes too
        used = {entry[0] for entry in kept.values()}
        alive = {slot: entry for slot, entry in kept.items() if _timeless_slot_lives(entry[4], kept, used)}
        if len(alive) == len(kept):
            break
        kept = alive
    renamed = _timeless_modulators(old, out, sorted(used, key=new_sources.index))
    for entry in kept.values():
        entry[0] = renamed.get(entry[0], entry[0])
        if module := re.match(r"(XLFO|EG|EF) \d+", entry[4]):
            entry[4] = renamed[module[0]] + entry[4][len(module[0]) :]
        if entry[4] == "Input Level":
            driver = entry[0].split(" ")[0]
            level, entry[1], silent = _older_gain_depth(driver, bool(entry[3]), entry[1], old["Input Level"])
            if silent:  # Timeless 3's importer moves the input level only down, and only then
                out["Input Level"] = min(out["Input Level"], level)
                limitation(
                    notices, "a slot swings the input level through silence; written as FabFilter's importer does"
                )

    def order(slot: int) -> tuple[int, int]:
        target = kept[slot][4]
        at = len(new_targets) + int(target.split()[1]) if target.startswith("Slot ") else new_targets.index(target)
        return new_sources.index(kept[slot][0]), at

    placed = sorted(kept, key=order)
    where = {slot: k for k, slot in enumerate(placed, 1)}
    out["Num Active Slots"] = float(len(placed))
    for k, slot in enumerate(placed, 1):
        source, depth, bypassed, inverted, target = kept[slot]
        if target.startswith("Slot "):
            target = f"Slot {where[int(target.split()[1])]} Level"
        out[f"Slot {k} Source"] = float(new_sources.index(source))
        out[f"Slot {k} Level"], out[f"Slot {k} Bypassed"], out[f"Slot {k} Inverted"] = depth, bypassed, inverted
        out[f"Slot {k} Target"] = float(new_targets.index(target))


def _timeless_slot_lives(target: str, kept: dict[int, list[Any]], used: set[str]) -> bool:
    if target.startswith("Slot "):
        return int(target.split()[1]) in kept
    module = re.match(r"(XLFO|EG|EF) \d+", target)
    return module is None or module[0] in used


def _timeless_modulators(old: dict[str, float], out: dict[str, float], used: list[str]) -> dict[str, str]:
    """Each kind of modulator still driving a slot, numbered again from 1 with its own controls; their new names."""
    renamed = {}
    for kind, count in _TIMELESS_MODULATORS.items():
        members = [source for source in used if source.split(" ")[0] == kind]
        out[count] = float(len(members))
        prefix = "MIDI Source " if kind == "MIDI" else f"{kind} "
        for k, source in enumerate(members, 1):
            renamed[source] = f"{kind} {k}"
            was = source.split(" ")[1]
            for older in old:
                rest = older.removeprefix(prefix + was)
                if rest != older and (rest.startswith(" ") or rest == "Input"):
                    name = f"{prefix}{k} {rest.strip()}"
                    if name in out:
                        out[name] = _timeless_control(old, older, name)
            if kind == "XLFO":
                out[f"Num Active XLFO {k} Steps"] = old[f"Num Active XLFO {was} Steps"]
            if kind == "EG":
                out[f"EG {k} Attack Slope"] = 0.2
                out[f"EG {k} Decay Slope"] = out[f"EG {k} Release Slope"] = 0.6
    return renamed


def timeless2_to_timeless3_parameters(bank: Sequence[float], notices: list[str] | None = None) -> tuple[float, ...]:
    """Timeless 3's 1007 stored floats for Timeless 2's 705 normalized controls."""
    data = _timeless_table()
    old = _timeless_units(bank)
    out = dict(zip(data["target_controls"], data["base"], strict=True))
    for name in out:
        older = _TIMELESS_RENAMED.get(name, name)
        if older in old and not _TIMELESS_MODULE.match(name):
            out[name] = _timeless_control(old, older, name)
    _timeless_delays(old, out)
    _timeless_feedback(old, out)
    _timeless_levels(old, out)
    _timeless_modulation(old, out, notices)
    return tuple(out.values())


@converter("fabfilter.timeless-2", "fabfilter.timeless-3", _BY_PRINT_MEASURED)
def timeless2_to_timeless3(patch: bytes, program: int | None = None) -> Converted:
    """A Timeless 2 host bank as Timeless 3's own patch, its VST2 chunk."""
    notices = [TIMELESS_NOT_HEARD_NOTE, TIMELESS_MEASURED_NOTE]
    return _ffbs_converted(patch, TIMELESS_3_EDITOR_MAGIC, notices, timeless2_to_timeless3_parameters)
