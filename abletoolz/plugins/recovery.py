"""Plan native reference repairs and apply prepared state conversions to set copies."""

from __future__ import annotations

import copy
import enum
import hashlib
import pathlib
from collections.abc import Sequence
from xml.etree import ElementTree as ET

from pydantic import BaseModel, ConfigDict, computed_field

from abletoolz.formats import live_document
from abletoolz.formats.errors import FormatError
from abletoolz.live_set.tracks import display_name
from abletoolz.plugins.container.device import PluginDevice, PluginDevices, PluginKind, ReferenceForm
from abletoolz.plugins.container.format_translation import Recipe, apply_recipe
from abletoolz.plugins.recovery_routes import (
    PreparedState,
    RecoveryTarget,
    StateEvidence,
    TargetFormat,
    TargetStatus,
    UidFields,
    fingerprint_binary,
)
from abletoolz.plugins.repair import needs_parameter_mapper
from abletoolz.plugins.state import ConstantControllerState, PreparedBytes
from abletoolz.schema import Attr


class RecoveryError(ValueError):
    """Recovery would change data outside the measured reference operation."""


class RecoveryStatus(enum.StrEnum):
    READY = "ready"
    NOT_APPLICABLE = "not_applicable"
    NO_ROUTE = "no_verified_route"
    INVALID_DEVICE = "invalid_device"
    UNVERIFIED_STATE = "state_or_program_unverified"
    TARGET_UNAVAILABLE = "target_unavailable"
    SET_TOO_OLD = "set_too_old_for_target"
    PARAMETER_TRANSLATION_REQUIRED = "parameter_translation_required"
    PREPARED_STATE_UNAVAILABLE = "prepared_state_unavailable"


class DeviceRecovery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    device: int
    source_name: str
    track: str | None = None
    plugin_device_id: str | None = None
    status: RecoveryStatus
    reason: str
    route_id: str | None = None
    source_unique_id: int | None = None
    state_sha256: str | None = None
    program_number: int | None = None
    target_path: pathlib.Path | None = None
    target_name: str | None = None
    target_sha256: str | None = None
    target_format: TargetFormat = "vst"
    target_uid_fields: UidFields | None = None
    program_capacity_update: int | None = None
    prepared: PreparedState | None = None
    info_sha256: str | None = None
    state_evidence: StateEvidence | None = None
    verification_notes: str | None = None
    audio_equivalence_verified: bool = False


class RecoveryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: pathlib.Path
    source_sha256: str
    document_sha256: str
    source_project: pathlib.Path | None
    project_relative_file_refs: int
    devices: tuple[DeviceRecovery, ...]
    operation: str = "verified_plugin_recovery"
    assets: str = "Stored media references are preserved; assets are not relocated"

    @computed_field  # type: ignore[prop-decorator]  # Pydantic supports stacked property decorators.
    @property
    def ready_count(self) -> int:
        return sum(device.status is RecoveryStatus.READY for device in self.devices)

    @computed_field  # type: ignore[prop-decorator]  # Pydantic supports stacked property decorators.
    @property
    def blocked_count(self) -> int:
        return sum(
            device.status not in (RecoveryStatus.READY, RecoveryStatus.NOT_APPLICABLE) for device in self.devices
        )


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _uid(value: int | None) -> int:
    """A stored unique id as the routes record it: unsigned, and refused outside 32 bits."""
    if value is None:
        raise RecoveryError("Missing plugin identity")
    if not -(1 << 31) <= value < (1 << 32):
        raise RecoveryError("Plugin identity is outside the 32-bit range")
    return value & 0xFFFFFFFF


def _infos(root: ET.Element) -> list[ET.Element]:
    return [device.info for device in PluginDevices(root)]


def _validate_device(device: PluginDevice, target: RecoveryTarget) -> tuple[str, int]:
    route = target.route
    identity, reference = device.identity, device.reference
    try:
        preset = device.preset
    except (FormatError, ValueError) as error:
        raise RecoveryError("Malformed saved plugin buffer") from error
    if _uid(identity.unique_id) != route.unique_id:
        raise RecoveryError("Source identity differs from the measured recovery route")
    if identity.parameter_count != route.parameter_count:
        raise RecoveryError("Source public parameter count differs from the measured source layout")
    if identity.program_count != route.program_count:
        raise RecoveryError("Source program count differs from the measured native bank")
    if identity.flags is None:
        raise RecoveryError("Missing device field: Flags")
    if reference.form is ReferenceForm.NONE or (reference.form is ReferenceForm.DIR and reference.file_name is None):
        raise RecoveryError("Expected one VST2 Path or Dir/FileName representation")
    if preset.type is None:
        raise RecoveryError("Expected exactly one saved VST2 preset")
    if preset.type != int.from_bytes(route.preset_type.encode("ascii"), "big"):
        raise RecoveryError("Preset container differs from the measured route")
    if _uid(preset.unique_id) != route.unique_id:
        raise RecoveryError("Saved preset identity differs from the device")
    if preset.parameter_count != route.parameter_count:
        raise RecoveryError("Saved preset public parameter count differs from the device")
    if preset.program_count != route.program_count:
        raise RecoveryError("Saved preset program count differs from the device")
    program = preset.program_number
    if program is None or not 0 <= program < route.program_count:
        raise RecoveryError("Selected program is outside the measured bank")
    raw = preset.state
    if not raw:
        raise RecoveryError("Saved plugin buffer is empty")
    if device.owner is None:
        raise RecoveryError("Expected a PluginDevice around the selected VST2 reference")
    controls = device.controls
    if controls.malformed:
        raise RecoveryError("Configured control has no readable parameter id")
    if any(not 0 <= index < route.parameter_count for index in controls.configured):
        raise RecoveryError("Configured control is outside the measured public parameter layout")
    return _digest(raw), program


def _device_plan(device: PluginDevice, targets: Sequence[RecoveryTarget]) -> DeviceRecovery:
    info = device.info
    assert device.document is not None and device.ordinal is not None  # read through PluginDevices
    name = device.identity.name or ""
    track = device.track
    owner = device.owner
    base = DeviceRecovery(
        device=device.ordinal,
        source_name=name,
        track=None if track is None else display_name(track, device.document.version) or track.tag,
        plugin_device_id=None if owner is None else owner.get(Attr.ID),
        status=RecoveryStatus.NOT_APPLICABLE,
        reason="No wrapped VST2 reference to recover",
    )
    if info.tag != "VstPluginInfo":
        return base
    matches = [target for target in targets if name in target.route.source_names]
    if not matches:
        if name.endswith(".64"):
            return base.model_copy(
                update={"status": RecoveryStatus.NO_ROUTE, "reason": "No measured native recovery route"}
            )
        return base
    try:
        uid = _uid(device.identity.unique_id)
        matching = [target for target in matches if target.route.unique_id == uid]
        if len(matching) != 1:
            raise RecoveryError("Wrapper name and plugin identity do not select one measured route")
        target = matching[0]
        digest, program = _validate_device(device, target)
    except RecoveryError as error:
        return base.model_copy(update={"status": RecoveryStatus.INVALID_DEVICE, "reason": str(error)})
    updates: dict[str, object] = {
        "route_id": target.route.id,
        "source_unique_id": uid,
        "state_sha256": digest,
        "program_number": program,
        "info_sha256": _digest(ET.tostring(info)),
        "state_evidence": target.route.state_evidence,
        "verification_notes": target.route.notes,
        "target_format": target.route.targets[0].format,
    }
    state = next((state for state in target.route.states if state.sha256 == digest), None)
    if state is None or program not in state.program_numbers:
        return base.model_copy(
            update=updates
            | {
                "status": RecoveryStatus.UNVERIFIED_STATE,
                "reason": "Saved bank and selected program have not been verified",
            }
        )
    if target.route.state_mode == "prepared_vst3":
        try:
            supported = device.document.supports(PluginKind.VST3)
        except ValueError as error:
            return base.model_copy(update=updates | {"status": RecoveryStatus.INVALID_DEVICE, "reason": str(error)})
        if not supported:
            return base.model_copy(
                update=updates
                | {"status": RecoveryStatus.SET_TOO_OLD, "reason": "Document version cannot hold VST3 devices"}
            )
    if target.route.state_mode != "reference":
        if needs_parameter_mapper(device):
            return base.model_copy(
                update=updates
                | {
                    "status": RecoveryStatus.PARAMETER_TRANSLATION_REQUIRED,
                    "reason": "Configured, mapped or referenced controls require a parameter-index mapper",
                }
            )
    if target.route.state_mode == "prepared_vst3":
        if not PluginDevice(info).complete:
            return base.model_copy(
                update=updates
                | {
                    "status": RecoveryStatus.INVALID_DEVICE,
                    "reason": "Source device is too incomplete for VST3 conversion",
                }
            )
        if state.prepared is None:
            raise RecoveryError("Prepared conversion route is missing its native state")
        try:
            state.prepared.read()
        except (OSError, ValueError) as error:
            return base.model_copy(
                update=updates | {"status": RecoveryStatus.PREPARED_STATE_UNAVAILABLE, "reason": str(error)}
            )
        updates["prepared"] = state.prepared
    if target.status is not TargetStatus.AVAILABLE or target.path is None or target.binary is None:
        return base.model_copy(update=updates | {"status": RecoveryStatus.TARGET_UNAVAILABLE, "reason": target.reason})
    reason = "Measured native identity; saved bank and all control bindings retained"
    if target.route.state_mode == "prepared_vst3":
        reason = "Prepared native VST3 state; unassigned control placeholders retained"
    elif target.route.state_mode == "reference_with_program_capacity":
        reason = "Measured native reader; saved bank retained and program-capacity metadata updated"
    return base.model_copy(
        update=updates
        | {
            "status": RecoveryStatus.READY,
            "reason": reason,
            "target_path": target.path,
            "target_name": target.binary.name,
            "target_sha256": target.binary.sha256,
            "target_format": target.binary.format,
            "target_uid_fields": target.binary.uid_fields,
            "program_capacity_update": (
                target.binary.program_count if target.route.state_mode == "reference_with_program_capacity" else None
            ),
        }
    )


def plan_recovery(source: pathlib.Path, targets: Sequence[RecoveryTarget]) -> RecoveryPlan:
    """Inspect a set without executing plugins or writing beside the input."""
    source = source.resolve()
    data = source.read_bytes()
    root = live_document.parse(data)
    project = next((folder for folder in source.parents if (folder / "Ableton Project Info").is_dir()), None)
    relative = sum(element.find("RelativePathType[@Value='3']") is not None for element in root.iter("FileRef"))
    return RecoveryPlan(
        source=source,
        source_sha256=_digest(data),
        document_sha256=_digest(ET.tostring(root)),
        source_project=project,
        project_relative_file_refs=relative,
        devices=tuple(_device_plan(device, targets) for device in PluginDevices(root)),
    )


def apply_recovery_plan(root: ET.Element, plan: RecoveryPlan) -> ET.Element:
    """Return a repaired tree after checking every planned reference; the input stays unchanged."""
    if _digest(ET.tostring(root)) != plan.document_sha256:
        raise RecoveryError("Document changed after recovery was planned")
    checks: dict[pathlib.Path, str | None] = {}
    prepared: dict[int, tuple[bytes, bytes]] = {}
    original = _infos(root)
    for action in plan.devices:
        if action.status is not RecoveryStatus.READY:
            continue
        if action.target_path is None or action.target_name is None or action.target_sha256 is None:
            raise RecoveryError("Ready recovery action is missing its native binary identity")
        if action.target_path not in checks:
            checks[action.target_path] = fingerprint_binary(action.target_path)
        if checks[action.target_path] != action.target_sha256:
            raise RecoveryError("Native plugin binary changed after recovery was planned")
        if (
            not 0 <= action.device < len(original)
            or _digest(ET.tostring(original[action.device])) != action.info_sha256
        ):
            raise RecoveryError("Selected plugin changed after recovery was planned")
        if action.target_format == "vst3":
            if action.prepared is None or action.target_uid_fields is None:
                raise RecoveryError("Prepared VST3 action is missing its state or class ID")
            try:
                prepared[action.device] = action.prepared.read()
            except (OSError, ValueError) as error:
                raise RecoveryError(str(error)) from error
    repaired = copy.deepcopy(root)
    devices = PluginDevices(repaired)
    for action in plan.devices:
        if action.status is not RecoveryStatus.READY:
            continue
        if action.target_path is None or action.target_name is None:
            raise RecoveryError("Missing planned target")
        device = devices[action.device]
        try:
            if action.target_format == "vst3":
                if action.target_uid_fields is None:
                    raise RecoveryError("Missing planned VST3 class ID")
                processor, controller = prepared[action.device]
                apply_recipe(
                    device,
                    Recipe(
                        PluginKind.VST3,
                        action.target_name,
                        action.target_uid_fields,
                        state_transform=PreparedBytes(processor),
                        controller_state=ConstantControllerState(controller),
                    ),
                )
                continue
            if action.program_capacity_update is not None:
                device.set_layout(programs=action.program_capacity_update)
                device.set_preset_record(programs=action.program_capacity_update)
            device.set_reference(action.target_path)
            device.set_name(action.target_name)
        except ValueError as error:
            raise RecoveryError(str(error)) from error
    return repaired


def recovered_document(plan: RecoveryPlan) -> bytes:
    """Revalidate the original file and native binaries before encoding an output copy."""
    source = plan.source.read_bytes()
    if _digest(source) != plan.source_sha256:
        raise RecoveryError("Source set changed after recovery was planned")
    return live_document.encode(apply_recovery_plan(live_document.parse(source), plan))
