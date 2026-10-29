"""Measured plugin parameters, separate from saved fields and conversion policy."""

from __future__ import annotations

import enum
import math
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.model import FieldKey, FixedByteMap, StateValue


class ParameterStatus(enum.StrEnum):
    NATIVE_CHECKED = "native_checked_at_tested_inputs"
    ROUNDING_UNRESOLVED = "rounding_unresolved"
    NO_SAVED_CHANGE = "no_saved_change_in_sweep"


@dataclass(frozen=True, slots=True)
class FieldSamples:
    field_id: int
    values: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class Parameter:
    """One public knob can affect several saved fields, depending on context."""

    index: int
    name: str
    unit: str
    fields: tuple[FieldSamples, ...]
    status: ParameterStatus


@dataclass(frozen=True, slots=True)
class ParameterMap:
    """A sampled relationship with explicit source identity and measurement scope.

    No interpolation is implied. Joint setter order, lock modes and native checked
    inputs belong to ``scope`` and the pinned evidence, not to storage semantics.
    """

    byte_map: FixedByteMap
    parameters: tuple[Parameter, ...]
    positions: tuple[float, ...]
    source_sha256: str
    evidence_sha256: str
    scope: str

    def __post_init__(self) -> None:
        if type(self.positions) is not tuple:
            raise StateTransformError("Parameter positions must be immutable")
        if not self.scope or not self.positions or tuple(sorted(set(self.positions))) != self.positions:
            raise StateTransformError("Parameter map requires explicit scope and unique increasing positions")
        if any(not math.isfinite(p) or not 0 <= p <= 1 for p in self.positions):
            raise StateTransformError("Parameter sample positions must be finite normalized values")
        if type(self.parameters) is not tuple or len({c.index for c in self.parameters}) != len(self.parameters):
            raise StateTransformError("Parameter indices must be unique and immutable")
        for digest in (self.source_sha256, self.evidence_sha256):
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise StateTransformError("Parameter map provenance requires lowercase SHA256 digests")
        for parameter in self.parameters:
            if type(parameter.index) is not int or parameter.index < 0:
                raise StateTransformError("Parameter indices must be nonnegative integers")
            if not isinstance(parameter.status, ParameterStatus) or type(parameter.fields) is not tuple:
                raise StateTransformError("Parameters require explicit status and immutable bindings")
            if len({binding.field_id for binding in parameter.fields}) != len(parameter.fields):
                raise StateTransformError("Duplicate saved-field binding")
            for binding in parameter.fields:
                self.byte_map.field(binding.field_id)
                if type(binding.values) is not tuple:
                    raise StateTransformError("Parameter samples must be immutable")
                if len(binding.values) != len(self.positions) or any(not math.isfinite(v) for v in binding.values):
                    raise StateTransformError(
                        "Parameter field samples disagree with positions or contain nonfinite values"
                    )

    def parameter(self, index: int) -> Parameter:
        for parameter in self.parameters:
            if type(index) is int and parameter.index == index:
                return parameter
        raise StateTransformError(f"Unmapped public parameter {index!r}")

    def sampled_values(self, index: int, normalized: float) -> dict[int, float]:
        """Read a captured sample; refuse to invent values between measurements."""
        parameter = self.parameter(index)
        if type(normalized) not in (int, float) or normalized not in self.positions:
            raise StateTransformError("This sampled map has no measurement at that position")
        if not parameter.fields:
            raise StateTransformError("No saved-field change was observed for this parameter")
        position = self.positions.index(normalized)
        return {binding.field_id: binding.values[position] for binding in parameter.fields}


def float32(value: float) -> float:
    try:
        if not math.isfinite(value):
            raise StateTransformError("Float32 value must be finite")
        result: float = struct.unpack("<f", struct.pack("<f", value))[0]
        return result
    except (OverflowError, struct.error) as error:
        raise StateTransformError("Value cannot be represented as float32") from error


def _normalized(value: float) -> float:
    if type(value) not in (float, int) or not 0 <= value <= 1:
        raise StateTransformError("Normalized parameters require a finite value in 0..1")
    return float32(value)


@dataclass(frozen=True, slots=True)
class Float32Affine:
    """A measured curve with rounding after each multiply and add."""

    scale: float
    bias: float

    def __post_init__(self) -> None:
        float32(self.scale)
        float32(self.bias)

    def __call__(self, normalized: float) -> float:
        value = _normalized(normalized)
        return float32(float32(value * float32(self.scale)) + float32(self.bias))


@dataclass(frozen=True, slots=True)
class BooleanSwitch:
    def __call__(self, normalized: float) -> bool:
        return _normalized(normalized) >= 0.5


class ParameterCurve(Protocol):
    """An executable parameter-to-saved-value relationship within a measured scope."""

    def __call__(self, normalized: float) -> StateValue: ...


@dataclass(frozen=True, slots=True)
class ParameterBinding:
    """An executable normalized-to-saved curve within its vendor model's scope."""

    index: int
    field_id: int
    name: str
    unit: str
    curve: ParameterCurve

    def value(self, normalized: float) -> StateValue:
        if type(normalized) not in (float, int) or not 0 <= normalized <= 1:
            raise StateTransformError("Normalized parameters require a finite value in 0..1")
        return self.curve(normalized)


@dataclass(frozen=True, slots=True)
class ParameterCurves:
    """Executable bindings for one byte map, bounded by a measured context."""

    byte_map: FixedByteMap
    bindings: tuple[ParameterBinding, ...]
    scope: str

    def __post_init__(self) -> None:
        if not self.scope.strip() or type(self.bindings) is not tuple or not self.bindings:
            raise StateTransformError("Parameter curves require scope and immutable bindings")
        if len({binding.index for binding in self.bindings}) != len(self.bindings):
            raise StateTransformError("Duplicate executable parameter index")
        for binding in self.bindings:
            if type(binding.index) is not int or binding.index < 0 or not binding.name:
                raise StateTransformError("Executable parameters require an index and name")
            self.byte_map.field(binding.field_id)
            if not callable(binding.curve):
                raise StateTransformError("Executable parameters require a curve")

    def binding(self, index: int) -> ParameterBinding:
        for binding in self.bindings:
            if type(index) is int and binding.index == index:
                return binding
        raise StateTransformError(f"Unmapped executable parameter {index!r}")

    def values(self, changes: Mapping[int, float]) -> dict[FieldKey, StateValue]:
        values: dict[FieldKey, StateValue] = {}
        for index, normalized in changes.items():
            binding = self.binding(index)
            if binding.field_id in values:
                raise StateTransformError("Several requested parameters address the same saved field")
            values[binding.field_id] = binding.value(normalized)
        return values
