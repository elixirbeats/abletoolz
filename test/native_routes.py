"""A synthetic native recovery route and the set it applies to, shared by the recovery and plan tests.

Synthetic but shaped like a measured route: a source bank and program it was
verified on, and the native binary, by hash, that accepts it. The device is a
Live 9 set built inline, so every field a route checks is in view.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from xml.etree import ElementTree as ET

from conftest import pe_module

from abletoolz.formats import hex_text, live_document, path_data
from abletoolz.plugins.recovery_routes import (
    NativeBinary,
    RecoveryRoute,
    RecoveryTarget,
    VerifiedState,
    resolve_recovery_targets,
)


def element(root: ET.Element, path: str) -> ET.Element:
    found = root.find(path)
    assert found is not None, path
    return found


def native_route(tmp_path: Path, *, old: bool = False) -> tuple[Path, RecoveryRoute, RecoveryTarget]:
    """A set holding one Legacy.64 device, a route measured on its bank, and the native binary it resolved to.

    ``old`` stores the device's file the pre-11 way, a folder blob and a file name.
    """
    binary = tmp_path / "plugins" / "Native.dll"
    binary.parent.mkdir()
    data = pe_module(binary).read_bytes()
    raw = b"measured source bank\0\xff"
    route = RecoveryRoute(
        id="test-native",
        source_names=("Legacy.64",),
        unique_id=123,
        parameter_count=2,
        program_count=2,
        preset_type="FBCh",
        states=(VerifiedState(sha256=hashlib.sha256(raw).hexdigest(), program_numbers=(1,)),),
        targets=(NativeBinary(file_name=binary.name, name="Native", sha256=hashlib.sha256(data).hexdigest()),),
        state_evidence="byte_exact_bank",
        notes="Synthetic identity and control-preservation fixture",
    )
    target = resolve_recovery_targets((route,), (binary.parent,))[0]
    source = tmp_path / "Project" / "song.als"
    source.parent.mkdir()
    (source.parent / "Ableton Project Info").mkdir()
    root = ET.fromstring(
        '<Ableton MajorVersion="5" MinorVersion="9.0_305" Creator="Ableton Live 9.0.1">'
        '<LiveSet><Tracks><MidiTrack Id="5"><Name><EffectiveName Value="Synth"/></Name>'
        '<DeviceChain><Devices><PluginDevice Id="9"><UserName Value="My patch"/><PluginDesc>'
        '<VstPluginInfo><PlugName Value="Legacy.64"/><UniqueId Value="123"/>'
        '<NumberOfParameters Value="2"/><NumberOfPrograms Value="2"/><Flags Value="67110148"/>'
        '<Preset><VstPreset Id="3"><Type Value="1178747752"/><UniqueId Value="123"/>'
        '<ParameterCount Value="2"/><ProgramCount Value="2"/><ProgramNumber Value="1"/>'
        '<ParameterSettings><Parameter Value="0.7"/></ParameterSettings><Buffer/>'
        "</VstPreset></Preset></VstPluginInfo></PluginDesc><ParameterList>"
        '<PluginFloatParameter Id="0"><ParameterId Value="1"/><ParameterName Value="Cutoff"/>'
        '<ParameterValue><Manual Value="0.7"/><AutomationTarget Id="77"/><KeyMidi Value="2"/>'
        "</ParameterValue></PluginFloatParameter></ParameterList></PluginDevice></Devices></DeviceChain>"
        '</MidiTrack></Tracks><PointeeId Value="77"/><FileRef><RelativePathType Value="3"/>'
        '<RelativePath><RelativePathElement Id="0" Dir="Samples"/></RelativePath></FileRef>'
        "</LiveSet></Ableton>"
    )
    info = element(root, ".//VstPluginInfo")
    element(info, "Preset/VstPreset/Buffer").text = hex_text.encode(raw, 5)
    if old:
        directory = ET.SubElement(info, "Dir")
        ET.SubElement(directory, "Type", Value="2")
        ET.SubElement(directory, "Data").text = hex_text.encode(path_data.encode("C:/Old"), 5)
        ET.SubElement(info, "FileName", Value="Legacy.64.dll")
    else:
        ET.SubElement(info, "Path", Value="C:/Old/Legacy.64.dll")
    source.write_bytes(live_document.encode(root))
    return source, route, target
