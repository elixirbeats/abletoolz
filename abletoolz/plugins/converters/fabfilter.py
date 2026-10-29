"""The re-encodes from Pro-Q 1 and Pro-Q 2 into Pro-Q 3.

Neither older Pro-Q migrates a patch into Pro-Q 3 by itself -- the host rig
asked Pro-Q 3 to read real patches of both and it refused -- so each is a
parser for the older bank and a serializer for Pro-Q 3's own chunk. The
formats themselves are FabFilter's and live in
:mod:`abletoolz.plugins.state.fabfilter`; this module only turns one into the
other.
"""

from __future__ import annotations

import datetime
import math
from collections.abc import Sequence

from abletoolz.plugins.converters import limitation
from abletoolz.plugins.state.catalog import Converted, converter
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.fabfilter import (
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
    FfbsState,
    Q1BandField,
    Q3BandField,
    default_editor_state,
)
from abletoolz.plugins.state.fxbk import LegacyBank
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung

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
