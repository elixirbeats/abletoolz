"""Bounded Trash 2 saved fields, algorithm domains and linear waveshaper states.

Only the measured VST2 build and opaque template are mapped. This is a codec
for that subset, independent of any correspondence with the original Trash.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from pydantic import TypeAdapter

from abletoolz.plugins.state import StateTransformError
from abletoolz.plugins.state.controls import BooleanSwitch, Float32Affine, ParameterBinding, ParameterCurves
from abletoolz.plugins.state.model import (
    EnumValue,
    FieldEncoding,
    FieldKey,
    FixedByteMap,
    MappedState,
    StateField,
    StateValue,
)
from abletoolz.plugins.state.plugin_map import PluginMap, StateFormat

WRAPPER_SHA256 = "f5d41f22665691526c988eb09dc6551262143412fe5d8a17078d0abe47d02673"
ENGINE_SHA256 = "625fe654b2818cde66c229745f72939e1a2f840359aa8125601d16650093ddc0"
SCOPE = (
    "VST2 version word 2050; fixed default framing; 16 native-checked parameter curves; "
    "additional saved fields from public sweeps, selectors and linear curves from native reloads; "
    "no general Trash 1 conversion claim"
)
STATE_FORMAT = "izotope.trash2.core/vst2-2050-template1"

# Public indices and saved-field IDs are separate even where the numbers coincide.
_CONTROLS = (
    ParameterBinding(0, 0, "Global Bypass", "", BooleanSwitch()),
    ParameterBinding(4, 4, "GAIN: Input (locked)", "dB", Float32Affine(154.0, -144.0)),
    ParameterBinding(9, 9, "GAIN: Output (locked)", "dB", Float32Affine(154.0, -144.0)),
    ParameterBinding(11, 11, "Global Mix", "", Float32Affine(100.0, 0.0)),
    ParameterBinding(13, 13, "Limiter Enable", "", BooleanSwitch()),
    ParameterBinding(14, 14, "FILT1: Bypass", "", BooleanSwitch()),
    ParameterBinding(17, 17, "FILT1: Node 1 Freq", "", Float32Affine(19980.0, 20.0)),
    ParameterBinding(18, 18, "FILT1: Node 1 Gain", "", Float32Affine(60.0, -30.0)),
    ParameterBinding(19, 19, "FILT1: Node 1 Q", "", Float32Affine(11.8, 0.2)),
    ParameterBinding(118, 118, "TRASH: Module Bypass", "", BooleanSwitch()),
    ParameterBinding(120, 120, "TRASH: Stg1 Enable", "", BooleanSwitch()),
    ParameterBinding(122, 122, "TRASH: Stg1 Pre", "", Float32Affine(50.0, -30.0)),
    ParameterBinding(123, 123, "TRASH: Stg1 Drive", "", Float32Affine(10.0, 0.0)),
    ParameterBinding(124, 124, "TRASH: Stg1 Style", "", Float32Affine(1.0, 0.0)),
    ParameterBinding(125, 125, "TRASH: Stg1 Gain", "", Float32Affine(50.0, -40.0)),
    ParameterBinding(128, 128, "TRASH: Stg1 Mix", "", Float32Affine(100.0, 0.0)),
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


def parameter_bindings() -> tuple[ParameterBinding, ...]:
    return _CONTROLS


@cache
def algorithms() -> tuple[EnumValue, ...]:
    """The 52 built-in selectors; user-defined algorithms are outside this map."""
    path = Path(__file__).with_name("data") / "trash2-algorithms.json"
    return TypeAdapter(tuple[EnumValue, ...]).validate_json(path.read_bytes(), strict=True)


def _additional_fields() -> tuple[StateField, ...]:
    bools = (
        (3, "GAIN: Input Lock", 2453),
        (8, "GAIN: Output Lock", 2454),
        (119, "TRASH: Bypass", 619),
        (121, "TRASH: Stg1 Shape Enable", 732),
        (129, "TRASH: Stg1 Log Mode", 630),
        (130, "TRASH: Stg1 DC Offset", 620),
        (131, "TRASH: Stg1 Filter Enable", 656),
        (138, "TRASH: Stg2 Enable", 707),
        (139, "TRASH: Stg2 Shape Enable", 733),
        (147, "TRASH: Stg2 Log Mode", 631),
        (148, "TRASH: Stg2 DC Offset", 621),
        (149, "TRASH: Stg2 Filter Enable", 657),
        (156, "TRASH: Enable Multiband", 116),
        (308, "FILT2: Bypass", 4760),
        (412, "DYN: Module Bypass", 2211),
        (485, "CONV: Bypass", 20),
        (491, "DLY: Bypass", 58),
    )
    numbers = (
        (1, "GAIN: Input L (unlocked)", 2421),
        (2, "GAIN: Input R (unlocked)", 2433),
        (5, "GAIN: Input (locked offset)", 2429),
        (6, "GAIN: Output L (unlocked)", 2437),
        (7, "GAIN: Output R (unlocked)", 2449),
        (10, "GAIN: Output (locked offset)", 2445),
        (140, "TRASH: Stg2 Pre", 626),
        (141, "TRASH: Stg2 Drive", 652),
        (142, "TRASH: Stg2 Style", 728),
        (143, "TRASH: Stg2 Gain", 644),
        (146, "TRASH: Stg2 Mix", 636),
    )
    result = [StateField(ident, name, FieldEncoding.BOOL8, offset) for ident, name, offset in bools]
    result.extend(StateField(ident, name, FieldEncoding.FLOAT32_LE, offset) for ident, name, offset in numbers)
    for band_index, (band, start) in enumerate(
        (("Bnd1", 118), ("Bnd2", 241), ("Bnd3", 364), ("Bnd4", 487), ("Single", 611))
    ):
        for stage in range(2):
            result.append(
                StateField(
                    1000 + band_index * 2 + stage,
                    f"TRASH: {band} Stg{stage + 1} Algorithm",
                    FieldEncoding.UINT32_LE,
                    start + stage * 4,
                    choices=algorithms(),
                )
            )
    for stage, blob in ((1, 1378), (2, 1452)):
        for index, (name, relative) in enumerate((("Start X", 49), ("Start Y", 53), ("End X", 57), ("End Y", 61))):
            result.append(
                StateField(
                    1100 + (stage - 1) * 5 + index,
                    f"TRASH: Stg{stage} Shape {name}",
                    FieldEncoding.FLOAT32_LE,
                    blob + relative,
                )
            )
        result.append(
            StateField(1104 + (stage - 1) * 5, f"TRASH: Stg{stage} Shape Bipolar", FieldEncoding.BOOL8, blob + 65)
        )
    return tuple(result)


@cache
def byte_map() -> FixedByteMap:
    fields = tuple(
        StateField(
            control.field_id,
            control.name,
            FieldEncoding.BOOL8 if isinstance(control.curve, BooleanSwitch) else FieldEncoding.FLOAT32_LE,
            _OFFSETS[control.field_id],
        )
        for control in _CONTROLS
    )
    return FixedByteMap(
        STATE_FORMAT,
        5354,
        fields + _additional_fields(),
        opaque_sha256="98baf57657510813b93d5f78693fb02384a19afbdc75c9d1d3f1c5440033a129",
    )


# Compatibility for the original research API.
state_map = byte_map
controls = parameter_bindings


@cache
def parameter_curves() -> ParameterCurves:
    return ParameterCurves(byte_map(), parameter_bindings(), SCOPE)


@dataclass(frozen=True, slots=True)
class Trash2State:
    settings: MappedState

    def __post_init__(self) -> None:
        if self.settings.map != byte_map():
            raise StateTransformError("Trash 2 state requires its measured core map")
        for stage in (1, 2):
            prefix = f"TRASH: Stg{stage} Shape "
            points = tuple(self.settings.value(prefix + name) for name in ("Start X", "Start Y", "End X", "End Y"))
            bipolar = self.settings.value(prefix + "Bipolar")
            if (points, bipolar) not in (
                ((0.0, 0.0, 1.0, 1.0), False),
                ((-1.0, -1.0, 1.0, 1.0), True),
                ((-1.0, 1.0, 1.0, -1.0), True),
            ):
                raise StateTransformError("Trash 2 map supports only measured linear waveshaper states")

    @classmethod
    def parse(cls, raw: bytes) -> Trash2State:
        return cls(byte_map().decode(raw))

    @classmethod
    def default(cls) -> Trash2State:
        """Start from the fingerprinted native template included with this map."""
        return cls.parse(_default_bytes())

    def with_values(self, changes: Mapping[FieldKey, StateValue]) -> Trash2State:
        return Trash2State(self.settings.with_values(changes))

    def with_parameters(self, changes: Mapping[int, float]) -> Trash2State:
        """Apply measured curves only after the opaque-template guard has matched."""
        return self.with_values(parameter_curves().values(changes))

    with_controls = with_parameters

    def with_stage_polarity(self, stage: int, *, inverted: bool) -> Trash2State:
        """Use a native-checked full bipolar curve for the selected single-band stage."""
        if type(stage) is not int or stage not in (1, 2) or type(inverted) is not bool:
            raise StateTransformError("Stage polarity requires stage 1 or 2 and an explicit Boolean")
        prefix = f"TRASH: Stg{stage} Shape "
        return self.with_values(
            {
                prefix + "Start X": -1.0,
                prefix + "Start Y": 1.0 if inverted else -1.0,
                prefix + "End X": 1.0,
                prefix + "End Y": -1.0 if inverted else 1.0,
                prefix + "Bipolar": True,
            }
        )

    def encode(self) -> bytes:
        return self.settings.encode()


@dataclass(frozen=True, slots=True)
class _Template:
    raw_hex: str
    sha256: str


@cache
def _default_bytes() -> bytes:
    path = Path(__file__).with_name("data") / "trash2-default.json"
    template = TypeAdapter(_Template).validate_json(path.read_bytes(), strict=True)
    raw = bytes.fromhex(template.raw_hex)
    if hashlib.sha256(raw).hexdigest() != template.sha256:
        raise StateTransformError("Trash 2 native template hash mismatch")
    return raw


@cache
def plugin_map() -> PluginMap[Trash2State]:
    return PluginMap(
        id="izotope.trash2",
        name="iZotope Trash 2",
        version=None,
        scope=SCOPE,
        binary_sha256=(WRAPPER_SHA256, ENGINE_SHA256),
        formats=(
            StateFormat(
                id=STATE_FORMAT,
                decoder=Trash2State.parse,
                scope=SCOPE,
                byte_maps=(byte_map(),),
                parameter_curves=(parameter_curves(),),
            ),
        ),
    )
