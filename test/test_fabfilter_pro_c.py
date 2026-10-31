"""Pro-C 1 into Pro-C 2, each control by what both plugins print.

The table both sides are read through is the two plugins' own prints at every knob step. Real patches, restored in
the original x86 Pro-C, and their conversions, restored in Pro-C 2, printed the same threshold within 0.01 dB, the
same ratio, levels, side-chain level and auto-release speed, and the same style and switches; only an attack or
release past Pro-C 2's range differed, written at its end.
"""

from __future__ import annotations

import math

import pytest

from abletoolz.plugins.converters import fabfilter as conversion
from abletoolz.plugins.state.errors import StateTransformError

TABLE = conversion._table("pro-c-1-to-pro-c-2.json")
NAMES: list[str] = TABLE["source_controls"]
TARGETS: list[str] = TABLE["target_controls"]
# Pro-C's own defaults, in control order.
DEFAULT = [0.0, 0.4707, 0.6078, 0.0, 0.6278, 0.2443, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.4632, 0.5, 0.0, 0.5, 1.0, 0.0]
DEFAULT += [0.5, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.5, 0.5, 1.0, 0.0, 1.0]


def converted(**changes: float) -> dict[str, float]:
    bank = list(DEFAULT)
    for name, value in changes.items():
        bank[NAMES.index(name.replace("_", " "))] = value
    return dict(zip(TARGETS, conversion.pro_c1_to_pro_c2_parameters(bank, []), strict=True))


@pytest.mark.parametrize("source", sorted(TABLE["numbers"]))
@pytest.mark.parametrize("position", [0.1, 0.37, 0.5, 0.83])
def test_each_number_is_written_where_pro_c_2_prints_what_pro_c_printed(source: str, position: float) -> None:
    pairing = TABLE["numbers"][source]
    wanted = conversion._printed(pairing["source"], position)
    got = conversion._printed(pairing["target"], converted(**{source: position})[pairing["to"]])
    if wanted > pairing["target"][-1]:
        assert got == pairing["target"][-1]
    elif wanted < pairing["target"][0]:  # a side-chain filter below Pro-C 2's 20 Hz
        assert got == pairing["target"][0]
    elif not math.isinf(wanted):
        assert got == pytest.approx(wanted, rel=1e-3, abs=1e-3)


@pytest.mark.parametrize(("value", "style"), [(0.0, 0.0), (0.5, 1 / 7), (1.0, 2 / 7)])
def test_characteristic_becomes_the_style_of_the_same_name(value: float, style: float) -> None:
    """Pro-C's Clean, Classic and Opto are the first three of Pro-C 2's eight styles."""
    assert converted(Characteristic=value)["Style"] == pytest.approx(style)


def test_with_auto_release_on_the_release_knob_takes_pro_c_s_auto_release_speed() -> None:
    """In auto, Pro-C 2's release knob prints the speed, 0 to 100% in a straight line."""
    assert converted(Auto_Release=1.0, Auto_Release_Speed=0.33)["Release"] == pytest.approx(0.33)
    manual = converted(Auto_Release=0.0, Release=0.2443)
    release_ms = conversion._printed(TABLE["numbers"]["Release"]["target"], manual["Release"])
    assert release_ms == pytest.approx(99.5, rel=1e-3)


def test_a_hard_knee_is_zero_and_the_side_chain_filters_switch_on_once_moved() -> None:
    hard = converted(Knee_Shape=1.0, High_Pass_Frequency=0.4, Low_Pass_Frequency=0.7)
    assert hard["Knee"] == 0.0
    assert (hard["Side Chain Low Enabled"], hard["Side Chain High Enabled"]) == (1.0, 1.0)
    plain = converted()
    assert (plain["Side Chain Low Enabled"], plain["Side Chain High Enabled"]) == (0.0, 0.0)


def test_an_attack_past_pro_c_2_s_longest_is_written_there_and_said() -> None:
    bank = list(DEFAULT)
    bank[NAMES.index("Attack")] = 1.0
    notices: list[str] = []
    parameters = conversion.pro_c1_to_pro_c2_parameters(bank, notices)
    assert parameters[TARGETS.index("Attack")] == 1.0
    assert any("attack 500 ms is longer than Pro-C 2's 250 ms" in notice for notice in notices)
    with pytest.raises(StateTransformError, match="attack 500 ms"):
        conversion.pro_c1_to_pro_c2_parameters(bank)


def test_what_pro_c_2_has_no_place_for_is_said() -> None:
    bank = list(DEFAULT)
    bank[NAMES.index("Channel Processing")] = 1.0
    bank[NAMES.index("Right Side Chain Level")] = 0.7
    notices: list[str] = []
    conversion.pro_c1_to_pro_c2_parameters(bank, notices)
    assert any("Mid-Side" in notice for notice in notices)
    assert any("one side-chain level" in notice for notice in notices)
