"""Volcano 2 into Volcano 3, each control in Volcano 3's own stored units.

Real patches, converted and restored in Volcano 3, print what the original prints for them, filters laid out as the
importer lays them, except envelope times of 0 ms, which Volcano 3 starts at 1.451 ms. The modulation depth rules
hold exactly over every slot FabFilter's own importer converted in its factory presets.
"""

from __future__ import annotations

import pytest

from abletoolz.plugins.converters import fabfilter as conversion
from abletoolz.plugins.state.errors import StateTransformError

TABLE = conversion._table(conversion.VOLCANO_TABLE)
NAMES: list[str] = TABLE["source_controls"]
TARGETS: list[str] = TABLE["target_controls"]
SOURCES: list[str] = TABLE["modulation"]["sources"]
SLOT_TARGETS: list[str] = TABLE["modulation"]["targets"]
NEW_TARGETS: list[str] = TABLE["modulation"]["new_targets"]
SPLIT = {"Routing Mode": 1.0, "Num Active Filters": 1.0}  # mid/side, four filters


def bank(**changes: float) -> list[float]:
    values = [0.0] * len(NAMES)
    for name, value in changes.items():
        values[NAMES.index(name.replace("_", " "))] = value
    return values


def converted(**changes: float) -> dict[str, float]:
    return dict(zip(TARGETS, conversion.volcano2_to_volcano3_parameters(bank(**changes), []), strict=True))


def slot(source: str, target: str, depth: float, **changes: float) -> dict[str, float]:
    return {
        "Num Active Slots": 1 / 24,
        "Slot 1 Source": SOURCES.index(source) / (len(SOURCES) - 1),
        "Slot 1 Target": SLOT_TARGETS.index(target) / (len(SLOT_TARGETS) - 1),
        "Slot 1 Level": depth,
    } | changes


@pytest.mark.parametrize(
    ("characteristic", "response", "style", "shape"),
    [(0, 0, 0.0, 0.0), (7, 1, 7.0, 1.0), (10, 2, 10.0, 2.0)],
)
def test_characteristic_and_response_are_volcano_3_s_style_and_shape(
    characteristic: int, response: int, style: float, shape: float
) -> None:
    """FabFilter One low pass is Classic Low Pass; Tube high pass and Clean band pass keep their names."""
    out = converted(Filter_1_Characteristic=characteristic / 10, Filter_1_Response=response / 2)
    assert (out["Filter 1 Style"], out["Filter 1 Shape"]) == (style, shape)


def test_the_filters_in_use_are_marked_used() -> None:
    assert [converted(Num_Active_Filters=1 / 3)[f"Filter {f} Used"] for f in range(1, 5)] == [1.0, 1.0, 0.0, 0.0]
    split_one = converted(Routing_Mode=1.0)
    assert [split_one[f"Filter {f} Used"] for f in range(1, 5)] == [1.0, 1.0, 0.0, 0.0]


def test_a_split_routing_of_four_filters_swaps_the_second_and_third() -> None:
    out = converted(**SPLIT, Filter_2_Delay=0.5)
    assert out["Filter 3 Delay"] == pytest.approx(0.5) and out["Filter 2 Delay"] == 0.0
    moved = converted(**slot("XLFO 1", "Filter 2 Freq", 0.5, **SPLIT))
    assert NEW_TARGETS[int(moved["Slot 1 Target"])] == "Filter 3 Freq"


def test_a_split_routing_of_three_filters_is_refused() -> None:
    with pytest.raises(StateTransformError, match="three filters"):
        conversion.volcano2_to_volcano3_parameters(bank(Routing_Mode=1.0, Num_Active_Filters=2 / 3))


@pytest.mark.parametrize(("source", "factor"), [("XLFO 1", 0.4), ("EG 2", 0.4), ("EF 1", 1.0), ("MIDI 1", 1.0)])
def test_a_filter_peak_s_depth_is_scaled_from_an_xlfo_or_envelope_only(source: str, factor: float) -> None:
    written = converted(**slot(source, "Filter 1 Peak", 1.0))["Slot 1 Level"]
    assert conversion._depth(written) == pytest.approx(factor)


def test_the_depth_curve_prints_what_volcano_prints() -> None:
    """Three of the 128 readouts of the depth knob, which all land on its corners to the printed digit."""
    assert [round(conversion._depth(k / 127), 3) for k in (16, 64, 100)] == [0.010, 0.164, 0.6]


# What FabFilter's importer wrote for synthetic presets of one slot on a gain: (source, inverted, depth, gain in dB)
# into the gain and the slot's depth.
@pytest.mark.parametrize(
    ("source", "inverted", "depth", "gain", "new_gain", "new_depth"),
    [
        ("XLFO 1", 0, 0.15, 10.0, 10.0, 0.52),
        ("MIDI 1", 1, 0.3, -20.0, -20.0, 0.8309),
        ("EF 1", 0, 0.3, 0.0, 7.268, 0.5323),
        ("EG 1", 0, 0.12, -20.0, -15.407, 0.51),
        ("EG 1", 1, 0.5, 20.0, 23.573, 0.6504),
    ],
)
def test_a_depth_on_a_gain_is_rewritten_as_the_importer_does(
    source: str, inverted: int, depth: float, gain: float, new_gain: float, new_depth: float
) -> None:
    changes = slot(source, "Output Gain", depth, Slot_1_Inverted=float(inverted), Output_Gain=(gain + 36) / 72)
    out = converted(**changes)
    assert (out["Output Gain"], out["Slot 1 Level"]) == (
        pytest.approx(new_gain, abs=0.01),
        pytest.approx(new_depth, abs=1e-3),
    )


def test_an_inverted_swing_through_silence_is_written_as_the_importer_writes_it_and_said() -> None:
    notices: list[str] = []
    changes = slot("EG 1", "Input Gain", 0.2, Slot_1_Inverted=1.0, Input_Gain=0.5)
    out = dict(zip(TARGETS, conversion.volcano2_to_volcano3_parameters(bank(**changes), notices), strict=True))
    assert (out["Input Gain"], out["Slot 1 Level"]) == (pytest.approx(-12.588, abs=0.01), 0.0)
    assert "through silence" in notices[0]


def test_an_envelope_in_use_gets_volcano_2_s_curve() -> None:
    out = converted(Num_Active_EGs=1 / 6)
    assert [out[f"EG 1 {part} Slope"] for part in ("Attack", "Decay", "Release")] == [0.2, 0.6, 0.6]
    assert out["EG 2 Attack Slope"] == 0.5


def test_an_envelope_not_in_use_at_the_old_default_threshold_gets_volcano_3_s() -> None:
    out = converted(Num_Active_EGs=1 / 6, EG_1_Threshold=0.3, EG_2_Threshold=0.3, EG_3_Threshold=0.5)
    assert out["EG 1 Threshold"] == pytest.approx(0.3)
    assert (out["EG 2 Threshold"], out["EG 3 Threshold"]) == (pytest.approx(0.27976, abs=1e-5), pytest.approx(0.5))
