from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from abletoolz.formats import hex_text, path_data
from abletoolz.live_set.document import AbletonSet
from abletoolz.plugins.migration import MigrationError, Vst2Migration, Vst2Target, apply_vst2_migration


def element(parent: ET.Element, path: str) -> ET.Element:
    found = parent.find(path)
    assert found is not None, path
    return found


def fixture(tmp_path: Path, *, old_path: bool = False) -> tuple[ET.Element, Vst2Migration]:
    root = ET.fromstring(
        '<Ableton Creator="Ableton Live 10.1.14"><LiveSet><PluginDevice Id="9">'
        '<UserName Value="My sound"/><PluginDesc><VstPluginInfo><PlugName Value="Old Lab"/>'
        '<UniqueId Value="123"/><NumberOfParameters Value="2880"/><Flags Value="67110149"/>'
        '<Preset><VstPreset><Type Value="1178747752"/><UniqueId Value="123"/>'
        '<ParameterCount Value="2880"/><ParameterSettings/><Buffer/></VstPreset></Preset>'
        "</VstPluginInfo></PluginDesc>"
        '<ParameterList><PluginFloatParameter Id="0"><ParameterId Value="-1"/>'
        '<ParameterValue><Manual Value="0.1234567687"/><AutomationTarget Id="77"/>'
        "</ParameterValue></PluginFloatParameter></ParameterList></PluginDevice>"
        '<AudioClip Id="99"/></LiveSet></Ableton>'
    )
    info = element(root, ".//VstPluginInfo")
    assert info is not None
    if old_path:
        directory = ET.SubElement(info, "Dir")
        ET.SubElement(directory, "Type", Value="2")
        ET.SubElement(directory, "Data").text = hex_text.encode(path_data.encode("C:/old"), 5)
        ET.SubElement(info, "FileName", Value="Old Lab.dll")
    else:
        ET.SubElement(info, "Path", Value="C:/old/Old Lab.dll")
    buffer = element(info, "Preset/VstPreset/Buffer")
    assert buffer is not None
    buffer.text = hex_text.encode(b"original\0\xff", 6)
    target = Vst2Target("New Lab", tmp_path / "New Lab.dll", 456, 2575, 1285)
    return root, Vst2Migration(
        "Old Lab", 123, hashlib.sha256(b"original\0\xff").hexdigest(), target, b"converted\xff\0"
    )


@pytest.mark.parametrize("old_path", [False, True])
def test_native_chunk_migration_preserves_schema_and_device_controls(tmp_path: Path, old_path: bool) -> None:
    root, migration = fixture(tmp_path, old_path=old_path)
    before = ET.tostring(element(root, ".//ParameterList"))
    result = apply_vst2_migration(root, 0, migration)
    info = element(root, ".//VstPluginInfo")
    assert info is not None
    assert root.attrib == {"Creator": "Ableton Live 10.1.14"}
    assert ET.tostring(element(root, ".//ParameterList")) == before
    assert element(root, ".//UserName").get("Value") == "My sound"
    assert element(root, ".//AudioClip").attrib == {"Id": "99"}
    assert element(info, "PlugName").get("Value") == "New Lab"
    assert element(info, "UniqueId").get("Value") == "456"
    assert element(info, "NumberOfParameters").get("Value") == "2575"
    assert element(info, "Flags").get("Value") == "1285"
    assert element(info, "Preset/VstPreset/UniqueId").get("Value") == "456"
    assert element(info, "Preset/VstPreset/ParameterCount").get("Value") == "2575"
    assert hex_text.decode(element(info, "Preset/VstPreset/Buffer").text) == migration.state
    if old_path:
        assert info.find("Path") is None
        assert element(info, "FileName").get("Value") == "New Lab.dll"
        assert path_data.decode(hex_text.decode(element(info, "Dir/Data").text)).path == str(
            migration.target.path.parent
        )
    else:
        assert element(info, "Path").get("Value") == migration.target.path.as_posix()
        assert info.find("Dir") is None
    assert result.source_sha256 == migration.source_sha256
    assert result.target_sha256 == hashlib.sha256(migration.state).hexdigest()


@pytest.mark.parametrize(
    "failure",
    [
        "state",
        "identity",
        "configured",
        "reference",
        "binding",
        "path",
        "missing",
        "index",
        "preset_id",
        "preset_count",
        "preset_type",
        "preset_settings",
    ],
)
def test_failed_migration_is_atomic(tmp_path: Path, failure: str) -> None:
    root, migration = fixture(tmp_path)
    device = 0
    match failure:
        case "state":
            migration = replace(migration, source_sha256="0" * 64)
        case "identity":
            migration = replace(migration, source_unique_id=999)
        case "configured":
            element(root, ".//ParameterId").set("Value", "5")
        case "reference":
            ET.SubElement(root, "PointeeId", Value="77")
        case "binding":
            ET.SubElement(element(root, ".//ParameterValue"), "KeyMidi")
        case "path":
            migration = replace(migration, target=replace(migration.target, path=Path("relative.dll")))
        case "missing":
            info = element(root, ".//VstPluginInfo")
            info.remove(element(info, "Flags"))
        case "index":
            device = 1
        case "preset_id":
            element(root, ".//VstPreset/UniqueId").set("Value", "999")
        case "preset_count":
            element(root, ".//VstPreset/ParameterCount").set("Value", "1")
        case "preset_type":
            element(root, ".//VstPreset/Type").set("Value", str(int.from_bytes(b"FxBk", "big")))
        case "preset_settings":
            ET.SubElement(element(root, ".//ParameterSettings"), "Parameter")
    before = ET.tostring(root)
    with pytest.raises(MigrationError):
        apply_vst2_migration(root, device, migration)
    assert ET.tostring(root) == before


def test_plugins_accessor_applies_the_same_migration(tmp_path: Path) -> None:
    root, migration = fixture(tmp_path)
    live_set = AbletonSet(tmp_path / "copy.als")
    live_set._root = root
    result = live_set.plugins.migrate_vst2(0, migration)
    assert result.target == "New Lab"
    assert element(live_set.root, ".//UniqueId").get("Value") == "456"


def test_plugin_ordinal_includes_vst3_devices(tmp_path: Path) -> None:
    root, migration = fixture(tmp_path)
    root.insert(0, ET.Element("Vst3PluginInfo"))
    with pytest.raises(MigrationError, match="not a VST2"):
        apply_vst2_migration(root, 0, migration)
    assert apply_vst2_migration(root, 1, migration).device == 1
