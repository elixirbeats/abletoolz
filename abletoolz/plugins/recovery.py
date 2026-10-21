"""Plan and apply measured VST2 reference recovery without changing saved controls."""

from __future__ import annotations

import copy
import enum
import hashlib
import pathlib
from collections.abc import Mapping, Sequence
from xml.etree import ElementTree as ET

from pydantic import BaseModel, ConfigDict, computed_field

from abletoolz.formats import hex_text, live_document, path_data
from abletoolz.plugins.recovery_routes import RecoveryTarget, StateEvidence, TargetStatus, fingerprint_binary


class RecoveryError(ValueError):
    """Recovery would change data outside the measured reference operation."""


class RecoveryStatus(enum.StrEnum):
    READY = "ready"
    NOT_APPLICABLE = "not_applicable"
    NO_ROUTE = "no_verified_route"
    INVALID_DEVICE = "invalid_device"
    UNVERIFIED_STATE = "state_or_program_unverified"
    TARGET_UNAVAILABLE = "target_unavailable"


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
    operation: str = "native_vst2_reference_recovery"
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


def _required(parent: ET.Element, path: str) -> ET.Element:
    element = parent.find(path)
    if element is None:
        raise RecoveryError(f"Missing device field: {path}")
    return element


def _integer(parent: ET.Element, path: str) -> int:
    value = _required(parent, path).get("Value")
    if value is None:
        raise RecoveryError(f"Missing integer value: {path}")
    try:
        return int(value)
    except ValueError as error:
        raise RecoveryError(f"Invalid integer value: {path}") from error


def _uid(parent: ET.Element, path: str) -> int:
    value = _integer(parent, path)
    if not -(1 << 31) <= value < (1 << 32):
        raise RecoveryError("Plugin identity is outside the 32-bit range")
    return value & 0xFFFFFFFF


def _infos(root: ET.Element) -> list[ET.Element]:
    return [element for element in root.iter() if element.tag in ("VstPluginInfo", "Vst3PluginInfo")]


def _check_path(info: ET.Element) -> None:
    direct = info.find("Path")
    directory = info.find("Dir")
    if direct is not None and directory is None:
        return
    if directory is not None and direct is None:
        _required(directory, "Data")
        _required(directory, "Type")
        _required(info, "FileName")
        return
    raise RecoveryError("Expected one VST2 Path or Dir/FileName representation")


def _validate_device(
    info: ET.Element, target: RecoveryTarget, parents: Mapping[ET.Element, ET.Element]
) -> tuple[str, int]:
    route = target.route
    if _uid(info, "UniqueId") != route.unique_id:
        raise RecoveryError("Source identity differs from the measured recovery route")
    if _integer(info, "NumberOfParameters") != route.parameter_count:
        raise RecoveryError("Source public parameter count differs from the measured native layout")
    if _integer(info, "NumberOfPrograms") != route.program_count:
        raise RecoveryError("Source program count differs from the measured native bank")
    _required(info, "Flags")
    _check_path(info)
    presets = info.findall("Preset/VstPreset")
    if len(presets) != 1:
        raise RecoveryError("Expected exactly one saved VST2 preset")
    preset = presets[0]
    if _integer(preset, "Type") != int.from_bytes(route.preset_type.encode("ascii"), "big"):
        raise RecoveryError("Preset container differs from the measured route")
    if _uid(preset, "UniqueId") != route.unique_id:
        raise RecoveryError("Saved preset identity differs from the device")
    if _integer(preset, "ParameterCount") != route.parameter_count:
        raise RecoveryError("Saved preset public parameter count differs from the device")
    if _integer(preset, "ProgramCount") != route.program_count:
        raise RecoveryError("Saved preset program count differs from the device")
    program = _integer(preset, "ProgramNumber")
    if not 0 <= program < route.program_count:
        raise RecoveryError("Selected program is outside the measured bank")
    try:
        raw = hex_text.decode(_required(preset, "Buffer").text)
    except ValueError as error:
        raise RecoveryError("Malformed saved plugin buffer") from error
    if not raw:
        raise RecoveryError("Saved plugin buffer is empty")
    description = parents.get(info)
    owner = parents.get(description) if description is not None else None
    if description is None or description.tag != "PluginDesc" or owner is None or owner.tag != "PluginDevice":
        raise RecoveryError("Expected a PluginDevice around the selected VST2 reference")
    parameters = owner.find("ParameterList")
    if parameters is not None:
        for parameter in parameters:
            index = _integer(parameter, "ParameterId")
            if index != -1 and not 0 <= index < route.parameter_count:
                raise RecoveryError("Configured control is outside the measured public parameter layout")
    return _digest(raw), program


def _location(info: ET.Element, parents: Mapping[ET.Element, ET.Element]) -> tuple[str | None, str | None]:
    track = None
    device = None
    current = parents.get(info)
    while current is not None:
        if current.tag == "PluginDevice" and device is None:
            device = current.get("Id")
        if current.tag in ("AudioTrack", "MidiTrack", "GroupTrack", "ReturnTrack", "MasterTrack"):
            name = current.find("Name/EffectiveName")
            track = name.get("Value") if name is not None else current.tag
            break
        current = parents.get(current)
    return track, device


def _device_plan(
    info: ET.Element, ordinal: int, targets: Sequence[RecoveryTarget], parents: Mapping[ET.Element, ET.Element]
) -> DeviceRecovery:
    name = ""
    element = info.find("PlugName" if info.tag == "VstPluginInfo" else "Name")
    if element is not None:
        name = element.get("Value", "")
    track, device = _location(info, parents)
    base = DeviceRecovery(
        device=ordinal,
        source_name=name,
        track=track,
        plugin_device_id=device,
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
        uid = _uid(info, "UniqueId")
        matching = [target for target in matches if target.route.unique_id == uid]
        if len(matching) != 1:
            raise RecoveryError("Wrapper name and plugin identity do not select one measured route")
        target = matching[0]
        digest, program = _validate_device(info, target, parents)
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
    if target.status is not TargetStatus.AVAILABLE or target.path is None or target.binary is None:
        return base.model_copy(update=updates | {"status": RecoveryStatus.TARGET_UNAVAILABLE, "reason": target.reason})
    return base.model_copy(
        update=updates
        | {
            "status": RecoveryStatus.READY,
            "reason": "Measured native identity; saved bank and all control bindings retained",
            "target_path": target.path,
            "target_name": target.binary.name,
            "target_sha256": target.binary.sha256,
        }
    )


def plan_recovery(source: pathlib.Path, targets: Sequence[RecoveryTarget]) -> RecoveryPlan:
    """Inspect a set without executing plugins or writing beside the input."""
    source = source.resolve()
    data = source.read_bytes()
    root = live_document.parse(data)
    parents = {child: parent for parent in root.iter() for child in parent}
    project = next((folder for folder in source.parents if (folder / "Ableton Project Info").is_dir()), None)
    relative = sum(element.find("RelativePathType[@Value='3']") is not None for element in root.iter("FileRef"))
    return RecoveryPlan(
        source=source,
        source_sha256=_digest(data),
        document_sha256=_digest(ET.tostring(root)),
        source_project=project,
        project_relative_file_refs=relative,
        devices=tuple(_device_plan(info, ordinal, targets, parents) for ordinal, info in enumerate(_infos(root))),
    )


def apply_recovery_plan(root: ET.Element, plan: RecoveryPlan) -> ET.Element:
    """Return a repaired tree after checking every planned reference; the input stays unchanged."""
    if _digest(ET.tostring(root)) != plan.document_sha256:
        raise RecoveryError("Document changed after recovery was planned")
    checks: dict[pathlib.Path, str | None] = {}
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
    repaired = copy.deepcopy(root)
    infos = _infos(repaired)
    for action in plan.devices:
        if action.status is not RecoveryStatus.READY:
            continue
        if action.target_path is None or action.target_name is None:
            raise RecoveryError("Missing planned target")
        info = infos[action.device]
        direct = info.find("Path")
        if direct is not None:
            direct.set("Value", action.target_path.as_posix())
        else:
            directory = _required(info, "Dir")
            data = _required(directory, "Data")
            depth = hex_text.depth(data.text) if data.text and data.text.strip() else 12
            data.text = hex_text.encode(path_data.encode(str(action.target_path.parent)), depth)
            _required(directory, "Type").set("Value", "2")
            _required(info, "FileName").set("Value", action.target_path.name)
        _required(info, "PlugName").set("Value", action.target_name)
    return repaired


def recovered_document(plan: RecoveryPlan) -> bytes:
    """Revalidate the original file and native binaries before encoding an output copy."""
    source = plan.source.read_bytes()
    if _digest(source) != plan.source_sha256:
        raise RecoveryError("Source set changed after recovery was planned")
    return live_document.encode(apply_recovery_plan(live_document.parse(source), plan))
