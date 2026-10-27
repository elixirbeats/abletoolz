"""Measured recovery identities and the native binaries they resolve to."""

from __future__ import annotations

import enum
import hashlib
import importlib.resources
import pathlib
import struct
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from abletoolz.plugins.loadable import PluginFolders


class RecoveryEvidence(enum.StrEnum):
    """What was measured to show a recovery route keeps a patch, as the route data spells it."""

    BYTE_EXACT_BANK = "byte_exact_bank"
    PROGRAM_FLOAT_PRESERVATION = "program_float_preservation"
    NUMERIC_STATE_PRESERVATION = "numeric_state_preservation"
    LEGACY_READER_RETENTION = "legacy_reader_retention"


class StateMode(enum.StrEnum):
    """How a route carries the saved bank: as it is, or as it is into a larger bank."""

    REFERENCE = "reference"
    REFERENCE_WITH_PROGRAM_CAPACITY = "reference_with_program_capacity"


class VerifiedState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    program_numbers: tuple[int, ...] = Field(min_length=1)


class NativeBinary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    file_name: str
    name: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parameter_count: int | None = Field(default=None, gt=0, le=65536)
    program_count: int | None = Field(default=None, gt=0, le=65536)

    @field_validator("file_name")
    @classmethod
    def _basename(cls, value: str) -> str:
        if pathlib.PureWindowsPath(value).name != value:
            raise ValueError("Native binary must name one file, without a directory")
        if not value.casefold().endswith(".dll"):
            raise ValueError("Native VST2 binary must end in .dll")
        return value


class RecoveryRoute(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    source_names: tuple[str, ...] = Field(min_length=1)
    unique_id: int = Field(ge=0, lt=1 << 32)
    parameter_count: int = Field(gt=0, le=65536)
    program_count: int = Field(gt=0, le=65536)
    preset_type: Literal["FBCh", "FxBk"]
    states: tuple[VerifiedState, ...] = Field(min_length=1)
    targets: tuple[NativeBinary, ...] = Field(min_length=1)
    state_evidence: RecoveryEvidence
    notes: str
    state_mode: StateMode = StateMode.REFERENCE

    @model_validator(mode="after")
    def _coverage(self) -> RecoveryRoute:
        if len({state.sha256 for state in self.states}) != len(self.states):
            raise ValueError("Duplicate recovery state hash")
        if any(not 0 <= number < self.program_count for state in self.states for number in state.program_numbers):
            raise ValueError("Verified program number is outside the native bank")
        if any(target.parameter_count not in (None, self.parameter_count) for target in self.targets):
            raise ValueError("Reference recovery cannot change the public parameter layout")
        if self.state_mode is StateMode.REFERENCE:
            if any(target.program_count not in (None, self.program_count) for target in self.targets):
                raise ValueError("A native program-count change requires an explicit capacity route")
        elif self.preset_type != "FBCh" or any(
            target.parameter_count != self.parameter_count
            or target.program_count is None
            or target.program_count <= self.program_count
            for target in self.targets
        ):
            raise ValueError(
                "Program-capacity recovery requires an opaque bank, matching controls and measured larger capacity"
            )
        return self


class TargetStatus(enum.StrEnum):
    AVAILABLE = "available"
    MISSING = "target_missing"
    UNVERIFIED = "target_binary_unverified"
    AMBIGUOUS = "target_ambiguous"


class RecoveryTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    route: RecoveryRoute
    status: TargetStatus
    path: pathlib.Path | None = None
    binary: NativeBinary | None = None
    candidates: tuple[pathlib.Path, ...] = ()
    reason: str


def load_recovery_routes() -> tuple[RecoveryRoute, ...]:
    """Read the bundled routes, without loading plugin code."""
    data = importlib.resources.files("abletoolz.plugins").joinpath("data/native_recovery.json").read_text("utf-8")
    routes = TypeAdapter(tuple[RecoveryRoute, ...]).validate_json(data)
    keys = [(name, route.unique_id) for route in routes for name in route.source_names]
    if len(keys) != len(set(keys)) or len({route.id for route in routes}) != len(routes):
        raise ValueError("Duplicate recovery route identity")
    return routes


def fingerprint_binary(path: pathlib.Path) -> str | None:
    """Hash an AMD64 PE32+ module; malformed or other-architecture files do not qualify."""
    data = path.read_bytes()
    if len(data) < 64 or data[:2] != b"MZ":
        return None
    offset = struct.unpack_from("<I", data, 0x3C)[0]
    if offset > len(data) - 26 or data[offset : offset + 4] != b"PE\0\0":
        return None
    if struct.unpack_from("<H", data, offset + 4)[0] != 0x8664:
        return None
    if struct.unpack_from("<H", data, offset + 24)[0] != 0x20B:
        return None
    return hashlib.sha256(data).hexdigest()


def resolve_recovery_targets(
    routes: Sequence[RecoveryRoute], plugin_dirs: Sequence[pathlib.Path]
) -> tuple[RecoveryTarget, ...]:
    """Resolve exact measured binaries under ``plugin_dirs``, every one of which has to be there."""
    for directory in plugin_dirs:
        if not directory.is_dir():
            raise NotADirectoryError(directory)
    folders = PluginFolders(plugin_dirs)
    names = {binary.file_name.casefold() for route in routes for binary in route.targets}
    found = {name: {candidate.resolve() for candidate in folders.named(name) if candidate.is_file()} for name in names}
    fingerprints: dict[pathlib.Path, str | None] = {}
    resolved: list[RecoveryTarget] = []
    for route in routes:
        candidates = sorted(
            {candidate for binary in route.targets for candidate in found[binary.file_name.casefold()]}, key=str
        )
        approved: list[tuple[pathlib.Path, NativeBinary]] = []
        for candidate in candidates:
            if candidate not in fingerprints:
                fingerprints[candidate] = fingerprint_binary(candidate)
            binary = next(
                (
                    target
                    for target in route.targets
                    if target.file_name.casefold() == candidate.name.casefold()
                    and target.sha256 == fingerprints[candidate]
                ),
                None,
            )
            if binary is not None:
                approved.append((candidate, binary))
        if len(approved) == 1:
            chosen, binary = approved[0]
            resolved.append(
                RecoveryTarget(
                    route=route,
                    status=TargetStatus.AVAILABLE,
                    path=chosen,
                    binary=binary,
                    candidates=tuple(candidates),
                    reason="Exact measured x64 native binary",
                )
            )
        else:
            status = (
                TargetStatus.AMBIGUOUS
                if approved
                else (TargetStatus.UNVERIFIED if candidates else TargetStatus.MISSING)
            )
            reason = {
                TargetStatus.AMBIGUOUS: "Multiple measured binaries found; narrow --plugin-dir to one installation",
                TargetStatus.UNVERIFIED: "Matching filenames have unmeasured bytes or architecture",
                TargetStatus.MISSING: "No measured native module found in the plugin search roots",
            }[status]
            resolved.append(RecoveryTarget(route=route, status=status, candidates=tuple(candidates), reason=reason))
    return tuple(resolved)
