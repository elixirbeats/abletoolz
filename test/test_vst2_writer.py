"""Writing a device as a VST2: another VST2 plugin in place, or a VST3 rebuilt in its set's own layout."""

from __future__ import annotations

import dataclasses
import pathlib
from xml.etree import ElementTree as ET

import pytest
from conftest import SKELETONS, skeleton

from abletoolz.plugins.container.device import PluginDevice, PluginDevices
from abletoolz.plugins.identity.ids import PluginKind, Vst2Record
from abletoolz.plugins.identity.vst2_records import live_flags

KEYS = sorted(path.stem for path in SKELETONS.glob("*.als"))
RECORD = Vst2Record(
    path=pathlib.Path("C:/VstPlugins/Target/Target x64.dll"),
    unique_id=1416126324,
    parameters=12,
    programs=1,
    flags=live_flags(0x31),
    version=1300,
    vst_version=2400,
    category=1,
    chunks=True,
)
# What a rebuild never writes: the preset folder Live 9 to 11.0 kept beside the host record.
_FOLDERS = {"PresetDirectory", "LastPresetFolder"}


def written(key: str, kind: PluginKind) -> tuple[ET.Element, PluginDevice]:
    """The first complete ``kind`` device of a fixture, rewritten as RECORD's VST2, and its document."""
    root = skeleton(key)
    devices = [device for device in PluginDevices(root) if device.kind is kind and device.complete]
    if not devices:
        pytest.skip(f"{key} has no {kind.value} device")
    device = devices[0]
    identity = dataclasses.replace(device.identity, kind=PluginKind.VST, name="Target", vst2=RECORD)
    preset = dataclasses.replace(device.saved_state, state=b"patch", program_number=None)
    device.rewrite(PluginKind.VST, identity, preset)
    return root, device


@pytest.mark.parametrize("key", KEYS)
def test_a_vst3_rebuilt_as_a_vst2_takes_the_layout_live_wrote_beside_it(key: str) -> None:
    root, device = written(key, PluginKind.VST3)
    live = [
        info for info in root.iter("VstPluginInfo") if info is not device.info and info.find("Category") is not None
    ]
    if not live:
        pytest.skip(f"{key} has no VST2 device Live wrote to compare with")
    assert [child.tag for child in device.info] == [child.tag for child in live[0] if child.tag not in _FOLDERS]
    live_preset = live[0].find("Preset/VstPreset")
    preset = device.info.find("Preset/VstPreset")
    assert live_preset is not None and preset is not None
    assert [child.tag for child in preset] == [child.tag for child in live_preset]


@pytest.mark.parametrize("key", ["10.0.1", "11.3.42"])
def test_a_vst2_written_as_another_vst2_keeps_its_layout_and_reads_back_as_the_new_plugin(key: str) -> None:
    root, device = written(key, PluginKind.VST)
    before = [child.tag for child in device.info]
    (again,) = [found for found in PluginDevices(root) if found.info is device.info]
    assert [child.tag for child in again.info] == before
    assert (again.kind, again.identity.name, again.identity.unique_id) == (PluginKind.VST, "Target", RECORD.unique_id)
    assert again.reference.file_name == "Target x64.dll"
    saved = again.saved_state
    assert (saved.state, saved.type, saved.unique_id, saved.parameter_count) == (b"patch", 1178747752, 1416126324, 12)
