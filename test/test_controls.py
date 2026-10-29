"""Measured controls: sampled maps never interpolate, and curves refuse what they cannot write."""

from __future__ import annotations

from dataclasses import replace

import pytest

from abletoolz.plugins.state.controls import (
    FieldSamples,
    Parameter,
    ParameterBinding,
    ParameterCurves,
    ParameterMap,
    ParameterStatus,
)
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.model import FieldEncoding, FixedByteMap, StateField


def layout() -> FixedByteMap:
    return FixedByteMap("test/float", 4, (StateField(12, "Amount", FieldEncoding.FLOAT32_LE, 0),))


def test_a_sampled_map_answers_only_at_its_measured_positions() -> None:
    parameters = ParameterMap(
        layout(),
        (Parameter(2, "Amount", "", (FieldSamples(12, (20.0, 20000.0)),), ParameterStatus.NATIVE_CHECKED),),
        (0.0, 1.0),
        "a" * 64,
        "b" * 64,
        "Only the two captured positions are mapped",
    )
    assert parameters.sampled_values(2, 1.0) == {12: 20000.0}
    with pytest.raises(StateTransformError, match="no measurement"):
        parameters.sampled_values(2, 0.5)
    with pytest.raises(StateTransformError, match="Unknown field"):
        replace(
            parameters,
            parameters=(Parameter(2, "Gone", "", (FieldSamples(99, (0.0, 1.0)),), ParameterStatus.NATIVE_CHECKED),),
        )


def test_parameter_curves_support_new_laws_and_reject_conflicting_edits() -> None:
    class Quadratic:
        def __call__(self, normalized: float) -> float:
            return normalized * normalized

    curves = ParameterCurves(
        layout(),
        (ParameterBinding(7, 12, "Amount", "", Quadratic()),),
        "Measured fixture inputs only",
    )
    assert curves.values({7: 0.3}) == {12: 0.09}
    for index, value in ((True, 0.5), (8, 0.5), (7, float("nan")), (7, 1.1)):
        with pytest.raises(StateTransformError):
            curves.values({index: value})
    shared = replace(curves, bindings=curves.bindings + (ParameterBinding(8, 12, "Other", "", Quadratic()),))
    with pytest.raises(StateTransformError, match="same saved field"):
        shared.values({7: 0.3, 8: 0.7})
    with pytest.raises(StateTransformError, match="Duplicate executable"):
        replace(curves, bindings=curves.bindings * 2)
    with pytest.raises(StateTransformError, match="Unknown field"):
        replace(curves, bindings=(ParameterBinding(7, 999, "Missing", "", Quadratic()),))
