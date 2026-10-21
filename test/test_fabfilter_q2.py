"""Pro-Q 2's band state and coupled placement survive its newer state format."""

from __future__ import annotations

import math

import pytest

from abletoolz.plugins.state import StateTransformError, custom_state
from abletoolz.plugins.state.fabfilter import (
    FABFILTER_PROCESSOR_TRAILER,
    PRO_Q2_TO_PRO_Q3,
    PRO_Q3_CONTROLLER,
    PRO_Q3_DEFAULT_PARAMETERS,
    EditorState,
    FfbsState,
    pro_q2_to_pro_q3_parameters,
)
from abletoolz.plugins.state.fxbk import LegacyBank


def source() -> list[float]:
    band = [1.0, math.log(100, 3000), 0.5, 0.5, 0.0, 0.125, 1.0]
    return band * 24 + [
        0.0,
        0.25,
        0.0,
        0.5,
        0.5,
        0.5,
        0.0,
        0.0,
        0.0,
        1.0,
        1.0,
        0.0,
        0.5,
        1 / 3,
        0.5,
        0.75,
        0.0,
        1.0,
        2 / 3,
        0.0,
        0.0,
        0.5,
        0.0,
    ]


@pytest.mark.parametrize(("state", "used", "enabled"), [(0.0, 1.0, 0.0), (0.5, 1.0, 1.0), (1.0, 0.0, 1.0)])
def test_disabled_bands_remain_present(state: float, used: float, enabled: float) -> None:
    bank = source()
    bank[0] = state
    converted = pro_q2_to_pro_q3_parameters(bank)
    assert converted[:2] == (used, enabled)
    assert converted[2] == pytest.approx(math.log2(1000))


@pytest.mark.parametrize(
    ("channel", "placement", "expected"),
    [
        (0.0, 0.0, 0),
        (0.0, 0.5, 1),
        (0.0, 1.0, 2),
        (1.0, 0.0, 3),
        (1.0, 0.5, 4),
        (1.0, 1.0, 2),
    ],
)
def test_channel_mode_changes_each_bands_placement(channel: float, placement: float, expected: int) -> None:
    bank = source()
    bank[0] = 0.5
    bank[6] = placement
    bank[170] = channel
    converted = pro_q2_to_pro_q3_parameters(bank)
    assert converted[10] == expected
    assert converted[317] == channel


def test_halfway_switches_use_native_round_half_up() -> None:
    bank = source()
    bank[0] = 0.25
    bank[4] = 0.5
    bank[174] = 0.5
    converted = pro_q2_to_pro_q3_parameters(bank)
    assert converted[:2] == (1.0, 1.0)
    assert converted[8] == 4.0
    assert converted[318] == 1.0


def test_wider_target_enums_keep_the_source_setting() -> None:
    bank = source()
    bank[4] = 1.0
    bank[5] = 1.0
    converted = pro_q2_to_pro_q3_parameters(bank)
    assert converted[8] == 7.0
    assert converted[9] == 8.0


def test_globals_keep_units_and_new_dynamics_keep_defaults() -> None:
    bank = source()
    bank[171:174] = [0.25, 0.75, 0.25]
    bank[189] = 0.25
    converted = pro_q2_to_pro_q3_parameters(bank)
    assert converted[314:317] == (0.5, 0.5, -0.5)
    assert converted[333] == -10.0
    assert converted[4:7] == PRO_Q3_DEFAULT_PARAMETERS[4:7]
    assert converted[334:] == PRO_Q3_DEFAULT_PARAMETERS[334:]


@pytest.mark.parametrize("index", [176, 179, 188, 190])
def test_unresolved_modes_fail_before_writing(index: int) -> None:
    bank = source()
    bank[index] = 1.0
    with pytest.raises(StateTransformError, match="unresolved"):
        pro_q2_to_pro_q3_parameters(bank)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.001, 1.001])
def test_invalid_normalized_values_are_refused(value: float) -> None:
    bank = source()
    bank[1] = value
    with pytest.raises(StateTransformError, match="finite normalized"):
        pro_q2_to_pro_q3_parameters(bank)


def test_wrong_product_count_is_refused() -> None:
    with pytest.raises(StateTransformError, match="191 parameters"):
        pro_q2_to_pro_q3_parameters(source()[:-1])


def test_registered_transform_and_controller_preserve_name() -> None:
    bank = LegacyBank("Recovered patch", tuple(source())).encode()
    processor = FfbsState.parse(custom_state(PRO_Q2_TO_PRO_Q3)(bank))
    assert len(processor.parameters) == 358
    assert processor.tail == FABFILTER_PROCESSOR_TRAILER
    assert EditorState.parse(PRO_Q3_CONTROLLER.build(bank)).preset_name == "Recovered patch"
