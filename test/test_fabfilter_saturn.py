"""Saturn 1 into Saturn 2, each control in Saturn 2's own stored units.

Real patches, converted and restored in Saturn 2, print what the original prints for them -- every list, modulation
source and target, and every number within 1% -- except envelope times of 0 ms, which Saturn 2 starts at 1.451 ms.
Factory presets convert as FabFilter's own importer imports them, apart from the modulation targets below.
"""

from __future__ import annotations

import pytest

from abletoolz.plugins.converters import fabfilter as conversion

TABLE = conversion._table("saturn-1-to-saturn-2.json.gz")
NAMES: list[str] = TABLE["source_controls"]
TARGETS: list[str] = TABLE["target_controls"]


def converted(**changes: float) -> dict[str, float]:
    bank = [0.0] * len(NAMES)
    for name, value in changes.items():
        bank[NAMES.index(name.replace("_", " "))] = value
    return dict(zip(TARGETS, conversion.saturn1_to_saturn2_parameters(bank, []), strict=True))


@pytest.mark.parametrize("target", ["EG 1 Attack", "EG 3 Decay", "EG 6 Hold", "EG 2 Delay"])
@pytest.mark.parametrize("position", [0.1, 0.37, 0.5, 0.83])
def test_an_envelope_time_is_written_where_saturn_2_prints_what_saturn_printed(target: str, position: float) -> None:
    """Saturn 2 stores these knobs' positions, so the float written is where it prints the original's time."""
    pairing = TABLE["numbers"][target]
    wanted = conversion._printed(pairing["source"], position)
    written = converted(**{pairing["from"]: position})[target]
    assert conversion._printed(pairing["target"], written) == pytest.approx(wanted, rel=1e-3)


def test_a_knob_both_print_alike_keeps_its_position_in_saturn_2_s_units() -> None:
    """Band 1 Drive is 0 to 100% on both, which Saturn 2 stores as a fraction."""
    assert converted(Band_1_Drive=0.25)["Band 1 Drive"] == pytest.approx(0.25, abs=1e-6)


@pytest.mark.parametrize(("item", "style"), [(0, 1.0), (4, 6.0), (11, 18.0), (15, 27.0)])
def test_the_sixteen_styles_keep_their_names_in_saturn_2_s_twenty_eight(item: int, style: float) -> None:
    """Clean Tube, Warm Tape, Gentle Saturation and Destroy, as FabFilter's importer numbers them too."""
    assert converted(Band_1_Style=item / 15)["Band 1 Style"] == style


def test_a_modulation_target_is_the_one_saturn_printed() -> None:
    """Old item 138 prints "EF 1 Attack", Saturn 2's item 174. FabFilter's importer writes 175, "EF 1 Release":
    it moves every modulator target one place along, so "EF 4 Release" would land on "Slot 1 Level"."""
    assert converted(Slot_5_Target=138 / 195)["Slot 5 Target"] == 174.0


@pytest.mark.parametrize(
    ("source", "gain", "depth"),
    [(21, 7.268, 0.5323), (1, 0.0, 0.8309)],  # EF 1, XY 1 Hor
)
def test_a_depth_on_a_gain_is_rewritten_as_fabfilter_s_importer_does(source: int, gain: float, depth: float) -> None:
    """What FabFilter's importer wrote for synthetic presets: a depth of 0.3 on an output gain of 0 dB."""
    out = converted(
        Num_Active_Slots=1 / 50, Slot_1_Source=source / 34, Slot_1_Target=3 / 195, Slot_1_Level=0.3, Output_Gain=0.5
    )
    assert (out["Output Gain"], out["Slot 1 Level"]) == (pytest.approx(gain, abs=0.01), pytest.approx(depth, abs=1e-3))


def test_auto_mute_feedback_is_unrestricted_feedback_turned_round() -> None:
    assert converted(Auto_Mute_Feedback=1.0)["Unrestricted Feedback"] == 0.0
    assert converted(Auto_Mute_Feedback=0.0)["Unrestricted Feedback"] == 1.0


def test_saturn_2_s_new_controls_are_set_as_fabfilter_s_importer_sets_them() -> None:
    assert converted()["Band 1 Crossover Slope"] == 2.0


def test_an_envelope_in_use_gets_saturn_s_curve() -> None:
    out = converted(Num_Active_EGs=2 / 6)
    assert [out[f"EG 2 {part} Slope"] for part in ("Attack", "Decay", "Release")] == [0.2, 0.6, 0.6]
    assert out["EG 3 Attack Slope"] == 0.5
