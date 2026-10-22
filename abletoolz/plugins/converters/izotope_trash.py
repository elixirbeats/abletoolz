"""Experimental Trash 1-to-2 distortion conversion with explicit scope and notices.

Maps own the bytes and parameter meanings. This module owns correspondence,
target defaults and known differences. Hosts retain automation and device identity.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from pydantic import TypeAdapter

from abletoolz.plugins.state import StateTransformError
from abletoolz.plugins.state.izotope_trash import Bank
from abletoolz.plugins.state.izotope_trash2 import Trash2State
from abletoolz.plugins.state.model import FieldKey, StateValue


class NoticeCode(enum.StrEnum):
    EXPERIMENTAL = "experimental"
    ALGORITHM_DIFFERENCE = "algorithm_difference"
    CHANNEL_GAINS_UNCALIBRATED = "channel_gains_uncalibrated"


@dataclass(frozen=True, slots=True)
class Notice:
    code: NoticeCode
    message: str
    stage: int | None = None


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
    program: int
    notices: tuple[Notice, ...]


@cache
def algorithm_rules() -> tuple[AlgorithmRule, ...]:
    """Return measured candidates; their names alone never establish sound agreement."""
    path = Path(__file__).with_name("data") / "trash1-to-trash2.json"
    data = TypeAdapter(_Rules).validate_json(path.read_bytes(), strict=True)
    if len({rule.source_id for rule in data.rules}) != len(data.rules):
        raise StateTransformError("Duplicate Trash algorithm correspondence")
    return data.rules


def convert_single_band_distortion(source: Bank, *, program: int) -> TrashConversion:
    """Convert one selected revision-21 program, refusing active unmapped modules.

    Output is an experimental VST2 state. Notices identify known tonal differences;
    container rewriting and host automation need separate application-level handling.
    """
    settings = source.program(program).settings
    for module in ("PreFilter", "PostFilter", "BoxModel", "Squash", "Buzz"):
        if settings.value(f"{module}: Bypass") is not True:
            raise StateTransformError(f"Active {module} is outside single-band distortion conversion")
    for mode in ("Multi-Band", "Multi-Rate", "Solo Mute"):
        if settings.value(f"Trash: {mode}") is not False:
            raise StateTransformError(f"Trash {mode} is outside single-band distortion conversion")
    if settings.value("Bypass Gain") != 0:
        raise StateTransformError("Nonzero bypass gain is unmapped")
    notices = [Notice(NoticeCode.EXPERIMENTAL, "Distortion-only conversion; sound agreement varies with controls.")]
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
            raise StateTransformError(f"No Trash 2 counterpart for original algorithm {algorithm}")
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
            changes[f"TRASH: Stg{stage} {target_suffix}"] = _number(value, low, high, source_suffix)
        if rule.invert_polarity:
            target = target.with_stage_polarity(stage, inverted=True)
        if rule.diagnostic_relative_rms >= 1e-4:
            notices.append(
                Notice(
                    NoticeCode.ALGORITHM_DIFFERENCE,
                    f"Original algorithm {algorithm} differs at a measured high-drive/Style setting; "
                    "listen to this stage.",
                    stage,
                )
            )
    for side in ("Input", "Output"):
        left = _number(settings.value(f"Gain: {side} Left"), -144, 10, f"{side} Left gain")
        right = _number(settings.value(f"Gain: {side} Right"), -144, 10, f"{side} Right gain")
        locked = settings.value(f"{side} Gain Slider Lock")
        if locked and left != right:
            raise StateTransformError(f"Locked {side} channel gains differ")
        changes[f"GAIN: {side} L (unlocked)"] = left
        changes[f"GAIN: {side} R (unlocked)"] = right
        changes[f"GAIN: {side} (locked)"] = left
        changes[f"GAIN: {side} Lock"] = locked
        if not locked:
            notices.append(
                Notice(NoticeCode.CHANNEL_GAINS_UNCALIBRATED, f"Unlocked {side} gains lack audio calibration.")
            )
    return TrashConversion(target.with_values(changes), program, tuple(notices))


def _number(value: StateValue, low: float, high: float, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise StateTransformError(f"{name} is outside the measured target range {low}..{high}")
    return float(value)
