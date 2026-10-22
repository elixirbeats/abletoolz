"""A bounded Trash 2 core-state model with sixteen native-checked control curves.

Only the measured VST2 build and opaque template are mapped. This is a codec
for that subset, independent of any correspondence with the original Trash.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache

from abletoolz.plugins.state import StateTransformError
from abletoolz.plugins.state.controls import BooleanSwitch, ControlBinding, Float32Affine
from abletoolz.plugins.state.model import FieldEncoding, FieldKey, MappedState, StateField, StateMap, StateValue

WRAPPER_SHA256 = "f5d41f22665691526c988eb09dc6551262143412fe5d8a17078d0abe47d02673"
ENGINE_SHA256 = "625fe654b2818cde66c229745f72939e1a2f840359aa8125601d16650093ddc0"
SCOPE = "VST2 version word 2050; captured default modes; 16 controls, three native save/reload vectors; no audio claim"

# Public indices and saved-field IDs are separate even where the numbers coincide.
_CONTROLS = (
    ControlBinding(0, 0, "Global Bypass", "", BooleanSwitch()),
    ControlBinding(4, 4, "GAIN: Input (locked)", "dB", Float32Affine(154.0, -144.0)),
    ControlBinding(9, 9, "GAIN: Output (locked)", "dB", Float32Affine(154.0, -144.0)),
    ControlBinding(11, 11, "Global Mix", "", Float32Affine(100.0, 0.0)),
    ControlBinding(13, 13, "Limiter Enable", "", BooleanSwitch()),
    ControlBinding(14, 14, "FILT1: Bypass", "", BooleanSwitch()),
    ControlBinding(17, 17, "FILT1: Node 1 Freq", "", Float32Affine(19980.0, 20.0)),
    ControlBinding(18, 18, "FILT1: Node 1 Gain", "", Float32Affine(60.0, -30.0)),
    ControlBinding(19, 19, "FILT1: Node 1 Q", "", Float32Affine(11.8, 0.2)),
    ControlBinding(118, 118, "TRASH: Module Bypass", "", BooleanSwitch()),
    ControlBinding(120, 120, "TRASH: Stg1 Enable", "", BooleanSwitch()),
    ControlBinding(122, 122, "TRASH: Stg1 Pre", "", Float32Affine(50.0, -30.0)),
    ControlBinding(123, 123, "TRASH: Stg1 Drive", "", Float32Affine(10.0, 0.0)),
    ControlBinding(124, 124, "TRASH: Stg1 Style", "", Float32Affine(1.0, 0.0)),
    ControlBinding(125, 125, "TRASH: Stg1 Gain", "", Float32Affine(50.0, -40.0)),
    ControlBinding(128, 128, "TRASH: Stg1 Mix", "", Float32Affine(100.0, 0.0)),
)
_OFFSETS = {
    0: 2467,
    4: 2425,
    9: 2441,
    11: 2480,
    13: 2642,
    14: 4283,
    17: 4368,
    18: 4392,
    19: 4416,
    118: 117,
    120: 706,
    122: 622,
    123: 648,
    124: 724,
    125: 640,
    128: 632,
}


def controls() -> tuple[ControlBinding, ...]:
    return _CONTROLS


@cache
def state_map() -> StateMap:
    fields = tuple(
        StateField(
            control.field_id,
            control.name,
            FieldEncoding.BOOL8 if isinstance(control.curve, BooleanSwitch) else FieldEncoding.FLOAT32_LE,
            _OFFSETS[control.field_id],
        )
        for control in _CONTROLS
    )
    return StateMap(
        "izotope.trash2.core/vst2-2050-template1",
        5354,
        fields,
        opaque_sha256="886698c651284e422ba5589f1f51c944f7f7087c95401bf8fda0e57d6e5fe051",
    )


@dataclass(frozen=True, slots=True)
class Trash2State:
    settings: MappedState

    def __post_init__(self) -> None:
        if self.settings.map != state_map():
            raise StateTransformError("Trash 2 state requires its measured core map")

    @classmethod
    def parse(cls, raw: bytes) -> Trash2State:
        return cls(state_map().decode(raw))

    def with_values(self, changes: Mapping[FieldKey, StateValue]) -> Trash2State:
        return Trash2State(self.settings.with_values(changes))

    def with_controls(self, changes: Mapping[int, float]) -> Trash2State:
        """Apply measured curves only after the opaque-template guard has matched."""
        values: dict[FieldKey, StateValue] = {}
        for index, normalized in changes.items():
            binding = next((c for c in _CONTROLS if type(index) is int and c.index == index), None)
            if binding is None:
                raise StateTransformError(f"Unmapped Trash 2 public control {index!r}")
            values[binding.field_id] = binding.value(normalized)
        return self.with_values(values)

    def encode(self) -> bytes:
        return self.settings.encode()
