"""Measured recovery identities, native binaries and prepared conversion states."""

from __future__ import annotations

import enum
import hashlib
import importlib.resources
import pathlib
import struct
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from abletoolz.machine import default_vst_dirs

type StateEvidence = Literal[
    "byte_exact_bank",
    "program_float_preservation",
    "numeric_state_preservation",
    "legacy_reader_retention",
    "prepared_native_readback",
]
type TargetFormat = Literal["vst", "vst3"]
type UidFields = tuple[int, int, int, int]
MAX_STATE_BYTES = 32 * 1024 * 1024


class StateFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: pathlib.Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def read(self) -> bytes:
        """Read a bounded prepared blob and verify its recorded identity."""
        with self.path.open("rb") as stream:
            data = stream.read(MAX_STATE_BYTES + 1)
        if len(data) > MAX_STATE_BYTES:
            raise ValueError(f"Prepared state exceeds the size limit: {self.path}")
        if hashlib.sha256(data).hexdigest() != self.sha256:
            raise ValueError(f"Prepared state changed after native verification: {self.path}")
        return data


class PreparedState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    processor: StateFile
    controller: StateFile | None = None

    def read(self) -> tuple[bytes, bytes]:
        processor = self.processor.read()
        if not processor:
            raise ValueError("Prepared VST3 processor state is empty")
        return processor, self.controller.read() if self.controller is not None else b""


class VerifiedState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    program_numbers: tuple[int, ...] = Field(min_length=1)
    prepared: PreparedState | None = None


class NativeBinary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    file_name: str
    name: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    format: TargetFormat = "vst"
    uid_fields: UidFields | None = None
    parameter_count: int | None = Field(default=None, gt=0, le=65536)
    program_count: int | None = Field(default=None, gt=0, le=65536)

    @field_validator("file_name")
    @classmethod
    def _basename(cls, value: str) -> str:
        if pathlib.PureWindowsPath(value).name != value:
            raise ValueError("Native binary must name one file, without a directory")
        return value

    @model_validator(mode="after")
    def _format_identity(self) -> NativeBinary:
        extension = ".dll" if self.format == "vst" else ".vst3"
        if not self.file_name.casefold().endswith(extension):
            raise ValueError(f"Native {self.format} binary must end in {extension}")
        if self.format == "vst3":
            if self.uid_fields is None or any(not -(1 << 31) <= field < (1 << 31) for field in self.uid_fields):
                raise ValueError("Native VST3 target requires four signed class-ID fields")
        elif self.uid_fields is not None:
            raise ValueError("A VST2 reference route does not use VST3 class-ID fields")
        return self


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
    state_evidence: StateEvidence
    notes: str
    state_mode: Literal["reference", "reference_with_program_capacity", "prepared_vst3"] = "reference"

    @model_validator(mode="after")
    def _coverage(self) -> RecoveryRoute:
        if len({state.sha256 for state in self.states}) != len(self.states):
            raise ValueError("Duplicate recovery state hash")
        if any(not 0 <= number < self.program_count for state in self.states for number in state.program_numbers):
            raise ValueError("Verified program number is outside the native bank")
        if self.state_mode != "prepared_vst3":
            if any(target.format != "vst" for target in self.targets) or any(state.prepared for state in self.states):
                raise ValueError("Reference recovery requires VST2 targets and unchanged saved banks")
            if any(target.parameter_count not in (None, self.parameter_count) for target in self.targets):
                raise ValueError("Reference recovery cannot change the public parameter layout")
            if self.state_mode == "reference":
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
        else:
            if self.state_evidence != "prepared_native_readback":
                raise ValueError("Prepared conversion requires explicit native-readback evidence scope")
            if len(self.targets) != 1:
                raise ValueError("Prepared conversion binds to one exact native binary")
            if any(target.format != "vst3" for target in self.targets) or any(
                state.prepared is None for state in self.states
            ):
                raise ValueError("Prepared conversion requires VST3 targets and a prepared state for every source")
            if self.program_count != 1 or any(state.program_numbers != (0,) for state in self.states):
                raise ValueError("Prepared VST3 conversion requires single-program source states")
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


def load_recovery_routes(prepared_paths: Sequence[pathlib.Path] = ()) -> tuple[RecoveryRoute, ...]:
    """Read bundled routes and explicit prepared manifests, without loading plugin code."""
    data = importlib.resources.files("abletoolz.plugins").joinpath("data/native_recovery.json").read_text("utf-8")
    routes = TypeAdapter(tuple[RecoveryRoute, ...]).validate_json(data)
    for manifest in prepared_paths:
        manifest = manifest.resolve()
        additions = TypeAdapter(tuple[RecoveryRoute, ...]).validate_json(manifest.read_text(encoding="utf-8"))
        for route in additions:
            if route.state_mode != "prepared_vst3":
                raise ValueError("--prepared accepts only prepared VST3 conversion routes")
            states: list[VerifiedState] = []
            for state in route.states:
                prepared = state.prepared
                if prepared is None:
                    raise ValueError("Prepared conversion is missing its native state files")
                processor = prepared.processor.model_copy(
                    update={"path": (manifest.parent / prepared.processor.path).resolve()}
                )
                controller = (
                    prepared.controller.model_copy(
                        update={"path": (manifest.parent / prepared.controller.path).resolve()}
                    )
                    if prepared.controller is not None
                    else None
                )
                states.append(
                    state.model_copy(update={"prepared": PreparedState(processor=processor, controller=controller)})
                )
            routes += (route.model_copy(update={"states": tuple(states)}),)
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
    routes: Sequence[RecoveryRoute], plugin_dirs: Sequence[pathlib.Path] | None = None
) -> tuple[RecoveryTarget, ...]:
    """Resolve exact measured binaries; explicit search roots replace the installed defaults."""
    directories = default_vst_dirs() if plugin_dirs is None else list(plugin_dirs)
    names = {binary.file_name.casefold() for route in routes for binary in route.targets}
    found: dict[str, set[pathlib.Path]] = {name: set() for name in names}
    for directory in directories:
        if not directory.is_dir():
            raise NotADirectoryError(directory)
        for candidate in directory.rglob("*"):
            key = candidate.name.casefold()
            if key in found and candidate.is_file():
                found[key].add(candidate.resolve())
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
