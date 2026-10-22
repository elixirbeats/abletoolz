"""Apply a prepared native VST2 chunk to the device it was derived from.

Vendor conversion and native reload checks happen before this operation. The
source hash binds that result to one saved patch; configured controls require
a separate parameter translation and are refused here.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import pathlib
from xml.etree import ElementTree as ET

from abletoolz.plugins.container.device import PluginDevice, PluginDevices, ReferenceForm


class MigrationError(ValueError):
    """A prepared state cannot be applied without losing device information."""


@dataclasses.dataclass(frozen=True)
class Vst2Target:
    """Installed target identity and the host inventory measured for it."""

    name: str
    path: pathlib.Path
    unique_id: int
    parameter_count: int
    flags: int


@dataclasses.dataclass(frozen=True)
class Vst2Migration:
    """A native converted chunk, bound to the original identity and state."""

    source_name: str
    source_unique_id: int
    source_sha256: str
    target: Vst2Target
    state: bytes


@dataclasses.dataclass(frozen=True)
class MigrationResult:
    device: int
    source: str
    target: str
    source_sha256: str
    target_sha256: str


def apply_vst2_migration(root: ET.Element, device: int, migration: Vst2Migration) -> MigrationResult:
    """Replace one VST2 chunk and identity atomically; retain the set's schema.

    ``device`` counts VST2/VST3 plugin info elements in document order, starting
    at zero. Unassigned Live parameter placeholders are retained. Configured
    parameters or references to placeholders require a parameter mapper first.
    """
    devices = PluginDevices(root)
    if not 0 <= device < len(devices) or devices[device].info.tag != "VstPluginInfo":
        raise MigrationError("Selected device is not a VST2 plugin")
    found = devices[device]
    info = found.info
    if found.owner is None:
        raise MigrationError("Expected a PluginDevice around the selected plugin")
    plugin = devices.parents[info]
    try:
        identity, reference, preset = found.identity, found.reference, found.saved_state
    except ValueError as error:
        raise MigrationError(str(error)) from error
    if identity.name != migration.source_name or identity.unique_id != migration.source_unique_id:
        raise MigrationError("Source plugin identity does not match the prepared migration")
    if identity.parameter_count is None or identity.flags is None:
        raise MigrationError("Missing device field")
    if reference.form is ReferenceForm.NONE:
        raise MigrationError("Unsupported or ambiguous VST2 path representation")
    if preset.type is None:
        raise MigrationError("Expected exactly one VST2 preset")
    if preset.type != int.from_bytes(b"FBCh", "big"):
        raise MigrationError("Expected an opaque FBCh preset, not a parameter bank")
    if preset.unique_id != migration.source_unique_id:
        raise MigrationError("Preset identity disagrees with its source plugin")
    if preset.parameter_count != identity.parameter_count:
        raise MigrationError("Preset parameter count disagrees with its source plugin")
    if preset.has_parameter_settings:
        raise MigrationError("Stored preset parameters require an explicit parameter translation")
    source_hash = hashlib.sha256(preset.state).hexdigest()
    if source_hash != migration.source_sha256:
        raise MigrationError("Source state changed after native conversion")
    target = migration.target
    if not target.name or not target.path.is_absolute() or not 0 < target.parameter_count <= 65536:
        raise MigrationError("Target requires a name, absolute path and measured parameter count")
    if not -(1 << 31) <= target.unique_id < (1 << 32) or target.flags < 0:
        raise MigrationError("Invalid target identity or flags")
    if not migration.state or len(migration.state) > 32 * 1024 * 1024:
        raise MigrationError("Expected a bounded, nonempty bare native chunk")

    controls = found.controls
    if controls.configured or controls.malformed:
        raise MigrationError("Configured plugin parameters require an explicit parameter translation")
    if controls.mapped:
        raise MigrationError("Mapped plugin controls require an explicit parameter translation")
    if controls.referenced:
        raise MigrationError("Referenced plugin parameters require an explicit parameter translation")

    # Staged on a copy, so a refusal anywhere above or below leaves the set untouched.
    replacement = copy.deepcopy(info)
    staged = PluginDevice(replacement)
    try:
        staged.set_reference(target.path)
        staged.set_name(target.name)
        staged.set_unique_id(target.unique_id)
        staged.set_layout(parameters=target.parameter_count, flags=target.flags)
        staged.set_saved_state_record(unique_id=target.unique_id, parameters=target.parameter_count)
        staged.set_state(migration.state)
    except ValueError as error:
        raise MigrationError(str(error)) from error
    result = MigrationResult(
        device, migration.source_name, target.name, source_hash, hashlib.sha256(migration.state).hexdigest()
    )
    index = list(plugin).index(info)
    plugin.remove(info)
    plugin.insert(index, replacement)
    return result
