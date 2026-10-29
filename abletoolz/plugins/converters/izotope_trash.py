"""Measured Trash 1-to-2 distortion conversion with explicit scope and notices.

Maps own the bytes and parameter meanings. This module owns correspondence,
target defaults and known differences. Hosts retain automation and device identity.
"""

from __future__ import annotations

import datetime
import math
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from pydantic import TypeAdapter

from abletoolz.plugins.converters import limitation
from abletoolz.plugins.state.catalog import Converted, converter
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.izotope_trash import Bank
from abletoolz.plugins.state.izotope_trash2 import Trash2State
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.model import FieldKey, StateValue


@dataclass(frozen=True, slots=True)
class AlgorithmRule:
    source_id: int
    target_id: int
    invert_polarity: bool
    diagnostic_relative_rms: float


@dataclass(frozen=True, slots=True)
class _Rules:
    scope: str
    source_sha256: str
    target_engine_sha256: str
    rules: tuple[AlgorithmRule, ...]


@dataclass(frozen=True, slots=True)
class TrashConversion:
    state: Trash2State
    notices: tuple[str, ...]


@cache
def algorithm_rules() -> tuple[AlgorithmRule, ...]:
    """Return measured correspondences with their retained audio differences."""
    path = Path(__file__).with_name("data") / "trash1-to-trash2.json"
    data = TypeAdapter(_Rules).validate_json(path.read_bytes(), strict=True)
    if len({rule.source_id for rule in data.rules}) != len(data.rules):
        raise StateTransformError("Duplicate Trash algorithm correspondence")
    return data.rules


def convert_single_band_distortion(source: Bank, *, program: int, lossy: bool = False) -> TrashConversion:
    """Convert one selected revision-21 program into Trash 2's single-band distortion.

    What has no measured match -- an active module, a multiband mode, a bypass
    gain, an algorithm, differing locked gains, a value past the measured range
    -- is refused; with ``lossy`` it is left out or clamped and said in the
    notices. Container rewriting and host automation are handled elsewhere.
    """
    settings = source.program(program).settings
    notices = ["Distortion-only conversion; sound agreement varies with controls."]
    left_out = notices if lossy else None
    for module in ("PreFilter", "PostFilter", "BoxModel", "Squash", "Buzz"):
        if settings.value(f"{module}: Bypass") is not True:
            limitation(left_out, f"the active {module} module has no measured match in Trash 2; it is left out")
    for mode in ("Multi-Band", "Multi-Rate", "Solo Mute"):
        if settings.value(f"Trash: {mode}") is not False:
            limitation(left_out, f"Trash {mode} has no measured match in Trash 2; it is off")
    if settings.value("Bypass Gain") != 0:
        limitation(left_out, "the bypass gain has no measured match in Trash 2; it is left out")
    target = Trash2State.default()
    changes: dict[FieldKey, StateValue] = {
        "Limiter Enable": False,
        "FILT1: Bypass": True,
        "FILT2: Bypass": True,
        "DYN: Module Bypass": True,
        "CONV: Bypass": True,
        "DLY: Bypass": True,
        "TRASH: Enable Multiband": False,
        "TRASH: Module Bypass": settings.value("Trash: Bypass"),
        "TRASH: Bypass": False,
    }
    for stage, prefix in enumerate(("First", "Second"), 1):
        algorithm = settings.value(f"Trash: {prefix} Algorithm")
        rule = next(
            (rule for rule in algorithm_rules() if type(algorithm) is int and rule.source_id == algorithm), None
        )
        if rule is None:
            limitation(
                left_out, f"stage {stage}'s algorithm {algorithm} has no Trash 2 counterpart; it keeps Trash 2's"
            )
        else:
            changes[f"TRASH: Single Stg{stage} Algorithm"] = rule.target_id
        for suffix, enabled in (("Enable", True), ("DC Offset", False), ("Log Mode", False), ("Filter Enable", False)):
            changes[f"TRASH: Stg{stage} {suffix}"] = enabled
        for source_suffix, target_suffix, low, high in (
            ("Input Gain", "Pre", -30, 20),
            ("Mix", "Mix", 0, 100),
            ("Gain", "Gain", -40, 10),
            ("Overdrive", "Drive", 0, 10),
            ("Trash", "Style", 0, 1),
        ):
            value = settings.value(f"Trash: {prefix} {source_suffix}")
            changes[f"TRASH: Stg{stage} {target_suffix}"] = _number(value, low, high, source_suffix, left_out)
        if rule is not None and rule.invert_polarity:
            target = target.with_stage_polarity(stage, inverted=True)
        if rule is not None and rule.diagnostic_relative_rms >= 1e-4:
            notices.append(
                f"Original algorithm {algorithm} differs at a measured high-drive/Style setting; listen to this stage."
            )
    for side in ("Input", "Output"):
        left = _number(settings.value(f"Gain: {side} Left"), -144, 10, f"{side} Left gain", left_out)
        right = _number(settings.value(f"Gain: {side} Right"), -144, 10, f"{side} Right gain", left_out)
        locked = settings.value(f"{side} Gain Slider Lock")
        if locked and left != right:
            limitation(left_out, f"the locked {side.lower()} gains differ; both take the left one")
            right = left
        changes[f"GAIN: {side} L (unlocked)"] = left
        changes[f"GAIN: {side} R (unlocked)"] = right
        changes[f"GAIN: {side} (locked)"] = left
        changes[f"GAIN: {side} Lock"] = locked
        if not locked:
            notices.append(f"Unlocked {side} gains lack audio calibration.")
    return TrashConversion(target.with_values(changes), tuple(notices))


def _number(value: StateValue, low: float, high: float, name: str, left_out: list[str] | None = None) -> float:
    """``value`` as a number in the measured range; one outside it is refused, or with ``left_out`` clamped."""
    if type(value) not in (int, float) or not math.isfinite(value):
        raise StateTransformError(f"{name} is not a number")
    if not low <= value <= high:
        limitation(left_out, f"{name} of {value} is outside the measured range {low}..{high}; it is clamped")
    return float(min(max(value, low), high))


@converter(
    "izotope.trash1",
    "izotope.trash2",
    MeasuredState(StateRung.RE_ENCODE, (StateEvidence.HOSTED,), datetime.date(2026, 10, 22)),
)
def trash_1_to_trash_2(patch: bytes, program: int | None) -> Converted:
    """The bank program a Trash 1 device has selected, as Trash 2's own patch."""
    if program is None:
        raise StateTransformError("This Trash 1 device does not say which of its bank's programs it plays")
    converted = convert_single_band_distortion(Bank.parse(patch), program=program, lossy=True)
    return Converted(converted.state.encode(), converted.notices)
