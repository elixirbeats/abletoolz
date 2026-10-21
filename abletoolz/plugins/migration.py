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

from abletoolz.formats import hex_text, path_data


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


def _required(parent: ET.Element, path: str) -> ET.Element:
    element = parent.find(path)
    if element is None:
        raise MigrationError(f"Missing device field: {path}")
    return element


def apply_vst2_migration(root: ET.Element, device: int, migration: Vst2Migration) -> MigrationResult:
    """Replace one VST2 chunk and identity atomically; retain the set's schema.

    ``device`` counts VST2/VST3 plugin info elements in document order, starting
    at zero. Unassigned Live parameter placeholders are retained. Configured
    parameters or references to placeholders require a parameter mapper first.
    """
    infos = [element for element in root.iter() if element.tag in ("VstPluginInfo", "Vst3PluginInfo")]
    if not 0 <= device < len(infos) or infos[device].tag != "VstPluginInfo":
        raise MigrationError("Selected device is not a VST2 plugin")
    info = infos[device]
    parents = {child: parent for parent in root.iter() for child in parent}
    plugin = parents.get(info)
    if plugin is None or plugin.tag != "PluginDesc":
        raise MigrationError("Expected a PluginDesc around the selected plugin")
    owner = parents.get(plugin)
    if owner is None or owner.tag != "PluginDevice":
        raise MigrationError("Expected a PluginDevice around the selected plugin")
    name = _required(info, "PlugName").get("Value")
    if name != migration.source_name or _required(info, "UniqueId").get("Value") != str(migration.source_unique_id):
        raise MigrationError("Source plugin identity does not match the prepared migration")
    presets = info.findall("Preset/VstPreset")
    if len(presets) != 1:
        raise MigrationError("Expected exactly one VST2 preset")
    preset = presets[0]
    if _required(preset, "Type").get("Value") != str(int.from_bytes(b"FBCh", "big")):
        raise MigrationError("Expected an opaque FBCh preset, not a parameter bank")
    if _required(preset, "UniqueId").get("Value") != str(migration.source_unique_id):
        raise MigrationError("Preset identity disagrees with its source plugin")
    if _required(preset, "ParameterCount").get("Value") != _required(info, "NumberOfParameters").get("Value"):
        raise MigrationError("Preset parameter count disagrees with its source plugin")
    settings = preset.find("ParameterSettings")
    if settings is not None and len(settings):
        raise MigrationError("Stored preset parameters require an explicit parameter translation")
    source = _required(preset, "Buffer")
    source_hash = hashlib.sha256(hex_text.decode(source.text)).hexdigest()
    if source_hash != migration.source_sha256:
        raise MigrationError("Source state changed after native conversion")
    target = migration.target
    if not target.name or not target.path.is_absolute() or not 0 < target.parameter_count <= 65536:
        raise MigrationError("Target requires a name, absolute path and measured parameter count")
    if not -(1 << 31) <= target.unique_id < (1 << 32) or target.flags < 0:
        raise MigrationError("Invalid target identity or flags")
    if not migration.state or len(migration.state) > 32 * 1024 * 1024:
        raise MigrationError("Expected a bounded, nonempty bare native chunk")

    parameters = owner.find("ParameterList")
    targets: set[str] = set()
    if parameters is not None:
        for parameter in parameters:
            if _required(parameter, "ParameterId").get("Value") != "-1":
                raise MigrationError("Configured plugin parameters require an explicit parameter translation")
            if parameter.find(".//KeyMidi") is not None:
                raise MigrationError("Mapped plugin controls require an explicit parameter translation")
            targets.update(
                element.attrib["Id"]
                for element in parameter.iter()
                if element.tag in ("AutomationTarget", "ModulationTarget") and "Id" in element.attrib
            )
    if any(element.tag == "PointeeId" and element.get("Value") in targets for element in root.iter()):
        raise MigrationError("Referenced plugin parameters require an explicit parameter translation")

    replacement = copy.deepcopy(info)
    direct_path = replacement.find("Path")
    directory = replacement.find("Dir")
    if direct_path is not None and directory is None:
        direct_path.set("Value", target.path.as_posix())
    elif directory is not None and direct_path is None:
        data = _required(directory, "Data")
        depth = hex_text.depth(data.text) if data.text and data.text.strip() else 12
        data.text = hex_text.encode(path_data.encode(str(target.path.parent)), depth)
        _required(directory, "Type").set("Value", "2")
        _required(replacement, "FileName").set("Value", target.path.name)
    else:
        raise MigrationError("Unsupported or ambiguous VST2 path representation")
    for field, value in (
        ("PlugName", target.name),
        ("UniqueId", str(target.unique_id)),
        ("NumberOfParameters", str(target.parameter_count)),
        ("Flags", str(target.flags)),
    ):
        _required(replacement, field).set("Value", value)
    _required(replacement, "Preset/VstPreset/UniqueId").set("Value", str(target.unique_id))
    _required(replacement, "Preset/VstPreset/ParameterCount").set("Value", str(target.parameter_count))
    destination = _required(replacement, "Preset/VstPreset/Buffer")
    depth = hex_text.depth(destination.text) if destination.text and destination.text.strip() else 12
    destination.text = hex_text.encode(migration.state, depth)
    result = MigrationResult(
        device, migration.source_name, target.name, source_hash, hashlib.sha256(migration.state).hexdigest()
    )
    index = list(plugin).index(info)
    plugin.remove(info)
    plugin.insert(index, replacement)
    return result
