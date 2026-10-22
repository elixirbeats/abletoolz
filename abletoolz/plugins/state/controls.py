"""Measured public controls, separate from saved fields and conversion policy."""

from __future__ import annotations

import enum
import math
import struct
from dataclasses import dataclass

from abletoolz.plugins.state import StateTransformError
from abletoolz.plugins.state.model import StateMap, StateValue


class ControlStatus(enum.StrEnum):
    NATIVE_CHECKED = "native_checked_at_tested_inputs"
    ROUNDING_UNRESOLVED = "rounding_unresolved"
    NO_SAVED_CHANGE = "no_saved_change_in_sweep"


@dataclass(frozen=True, slots=True)
class FieldSamples:
    field_id: int
    values: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class Control:
    """One public knob can affect several saved fields, depending on context."""

    index: int
    name: str
    unit: str
    fields: tuple[FieldSamples, ...]
    status: ControlStatus


@dataclass(frozen=True, slots=True)
class ControlMap:
    """A sampled relationship with explicit source identity and measurement scope.

    No interpolation is implied. Joint setter order, lock modes and native checked
    inputs belong to ``scope`` and the pinned evidence, not to storage semantics.
    """

    state_map: StateMap
    controls: tuple[Control, ...]
    positions: tuple[float, ...]
    source_sha256: str
    evidence_sha256: str
    scope: str

    def __post_init__(self) -> None:
        if type(self.positions) is not tuple:
            raise StateTransformError("Control positions must be immutable")
        if not self.scope or not self.positions or tuple(sorted(set(self.positions))) != self.positions:
            raise StateTransformError("Control map requires explicit scope and unique increasing positions")
        if any(not math.isfinite(p) or not 0 <= p <= 1 for p in self.positions):
            raise StateTransformError("Control sample positions must be finite normalized values")
        if type(self.controls) is not tuple or len({c.index for c in self.controls}) != len(self.controls):
            raise StateTransformError("Control indices must be unique and immutable")
        for digest in (self.source_sha256, self.evidence_sha256):
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise StateTransformError("Control map provenance requires lowercase SHA256 digests")
        for control in self.controls:
            if type(control.index) is not int or control.index < 0:
                raise StateTransformError("Control indices must be nonnegative integers")
            if not isinstance(control.status, ControlStatus) or type(control.fields) is not tuple:
                raise StateTransformError("Controls require explicit status and immutable bindings")
            if len({binding.field_id for binding in control.fields}) != len(control.fields):
                raise StateTransformError("Duplicate saved-field binding")
            for binding in control.fields:
                self.state_map.field(binding.field_id)
                if type(binding.values) is not tuple:
                    raise StateTransformError("Control samples must be immutable")
                if len(binding.values) != len(self.positions) or any(not math.isfinite(v) for v in binding.values):
                    raise StateTransformError(
                        "Control field samples disagree with positions or contain nonfinite values"
                    )

    def control(self, index: int) -> Control:
        for control in self.controls:
            if type(index) is int and control.index == index:
                return control
        raise StateTransformError(f"Unmapped public control {index!r}")

    def sampled_values(self, index: int, normalized: float) -> dict[int, float]:
        """Read a captured sample; refuse to invent values between measurements."""
        control = self.control(index)
        if type(normalized) not in (int, float) or normalized not in self.positions:
            raise StateTransformError("This sampled map has no measurement at that position")
        if not control.fields:
            raise StateTransformError("No saved-field change was observed for this control")
        position = self.positions.index(normalized)
        return {binding.field_id: binding.values[position] for binding in control.fields}


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
        raise StateTransformError("Normalized controls require a finite value in 0..1")
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


type ControlCurve = Float32Affine | BooleanSwitch


@dataclass(frozen=True, slots=True)
class ControlBinding:
    """An executable normalized-to-saved curve within its vendor model's scope."""

    index: int
    field_id: int
    name: str
    unit: str
    curve: ControlCurve

    def value(self, normalized: float) -> StateValue:
        return self.curve(normalized)
