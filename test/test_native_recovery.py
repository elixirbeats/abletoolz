from __future__ import annotations

import copy
import hashlib
import json
import struct
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from abletoolz.formats import hex_text, live_document, path_data
from abletoolz.plugins import commands
from abletoolz.plugins.recovery import (
    RecoveryError,
    RecoveryStatus,
    apply_recovery_plan,
    plan_recovery,
    recovered_document,
)
from abletoolz.plugins.recovery_routes import (
    NativeBinary,
    RecoveryRoute,
    RecoveryTarget,
    TargetStatus,
    VerifiedState,
    load_recovery_routes,
    resolve_recovery_targets,
)


def element(root: ET.Element, path: str) -> ET.Element:
    found = root.find(path)
    assert found is not None, path
    return found


def fixture(tmp_path: Path, *, old: bool = False) -> tuple[Path, RecoveryRoute, RecoveryTarget]:
    binary = tmp_path / "plugins" / "Native.dll"
    binary.parent.mkdir()
    data = bytearray(128)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 64)
    data[64:68] = b"PE\0\0"
    struct.pack_into("<H", data, 68, 0x8664)
    struct.pack_into("<H", data, 88, 0x20B)
    binary.write_bytes(data)
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


@pytest.mark.parametrize("old", [False, True])
def test_recovery_changes_only_reference_and_preserves_entire_control_graph(tmp_path: Path, old: bool) -> None:
    source, _route, target = fixture(tmp_path, old=old)
    original = source.read_bytes()
    root = live_document.parse(original)
    pristine = ET.tostring(root)
    plan = plan_recovery(source, (target,))
    assert plan.ready_count == 1
    assert plan.devices[0].track == "Synth"
    assert plan.devices[0].plugin_device_id == "9"
    assert plan.project_relative_file_refs == 1
    assert plan.source_project == source.parent
    repaired = apply_recovery_plan(root, plan)
    assert ET.tostring(root) == pristine
    expected = copy.deepcopy(root)
    info = element(expected, ".//VstPluginInfo")
    assert target.path is not None
    element(info, "PlugName").set("Value", "Native")
    if old:
        data = element(info, "Dir/Data")
        assert data.text is not None
        data.text = hex_text.encode(path_data.encode(str(target.path.parent)), hex_text.depth(data.text))
        element(info, "FileName").set("Value", "Native.dll")
    else:
        element(info, "Path").set("Value", target.path.as_posix())
    assert ET.tostring(repaired) == ET.tostring(expected)
    assert live_document.encode(repaired) == recovered_document(plan)
    assert source.read_bytes() == original


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("UniqueId", "999"),
        ("NumberOfParameters", "3"),
        ("NumberOfPrograms", "3"),
        ("Preset/VstPreset/UniqueId", "999"),
        ("Preset/VstPreset/ParameterCount", "3"),
        ("Preset/VstPreset/ProgramCount", "3"),
        ("Preset/VstPreset/Type", "1182286443"),
        ("Preset/VstPreset/ProgramNumber", "3"),
    ],
)
def test_mismatched_headers_are_refused_without_mutation(tmp_path: Path, path: str, value: str) -> None:
    source, _route, target = fixture(tmp_path)
    root = live_document.read(source)
    element(element(root, ".//VstPluginInfo"), path).set("Value", value)
    source.write_bytes(live_document.encode(root))
    before = source.read_bytes()
    plan = plan_recovery(source, (target,))
    assert plan.devices[0].status is RecoveryStatus.INVALID_DEVICE
    assert not plan.ready_count
    assert source.read_bytes() == before


@pytest.mark.parametrize("kind", ["bank", "program", "control", "path", "preset"])
def test_unknown_state_program_and_ambiguous_device_are_refused(tmp_path: Path, kind: str) -> None:
    source, _route, target = fixture(tmp_path)
    root = live_document.read(source)
    info = element(root, ".//VstPluginInfo")
    match kind:
        case "bank":
            element(info, "Preset/VstPreset/Buffer").text = "0001"
        case "program":
            element(info, "Preset/VstPreset/ProgramNumber").set("Value", "0")
        case "control":
            element(root, ".//ParameterId").set("Value", "2")
        case "path":
            ET.SubElement(info, "Dir")
        case "preset":
            element(info, "Preset").append(copy.deepcopy(element(info, "Preset/VstPreset")))
    source.write_bytes(live_document.encode(root))
    plan = plan_recovery(source, (target,))
    assert not plan.ready_count
    expected = RecoveryStatus.UNVERIFIED_STATE if kind in ("bank", "program") else RecoveryStatus.INVALID_DEVICE
    assert plan.devices[0].status is expected


@pytest.mark.parametrize("changed", ["source", "tree", "binary"])
def test_stale_plan_refuses_atomically(tmp_path: Path, changed: str) -> None:
    source, _route, target = fixture(tmp_path)
    plan = plan_recovery(source, (target,))
    root = live_document.read(source)
    if changed == "source":
        source.write_bytes(source.read_bytes() + b"changed")
    elif changed == "tree":
        root.set("Creator", "changed")
    else:
        assert target.path is not None
        target.path.write_bytes(target.path.read_bytes() + b"changed")
    pristine = ET.tostring(root)
    with pytest.raises(RecoveryError):
        if changed == "source":
            recovered_document(plan)
        else:
            apply_recovery_plan(root, plan)
    assert ET.tostring(root) == pristine


def test_resolver_distinguishes_missing_unverified_and_ambiguous(tmp_path: Path) -> None:
    _source, route, target = fixture(tmp_path)
    missing = tmp_path / "missing"
    missing.mkdir()
    assert resolve_recovery_targets((route,), (missing,))[0].status is TargetStatus.MISSING
    assert target.path is not None
    extra = tmp_path / "second"
    extra.mkdir()
    other = extra / target.path.name
    other.write_bytes(target.path.read_bytes())
    assert resolve_recovery_targets((route,), (target.path.parent, extra))[0].status is TargetStatus.AMBIGUOUS
    other.write_bytes(b"not a native plugin")
    assert resolve_recovery_targets((route,), (extra,))[0].status is TargetStatus.UNVERIFIED


def test_cli_plan_is_read_only_and_repair_uses_explicit_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, route, target = fixture(tmp_path)
    monkeypatch.setattr(commands, "load_recovery_routes", lambda: (route,))
    assert target.path is not None
    before = {file: file.read_bytes() for file in source.parent.rglob("*") if file.is_file()}
    common = [str(source), "--plugin-dir", str(target.path.parent), "--json"]
    assert commands.run_plugin_commands(["plan", *common]) == 0
    assert before == {file: file.read_bytes() for file in source.parent.rglob("*") if file.is_file()}
    output = tmp_path / "out" / source.name
    assert commands.run_plugin_commands(["repair", *common, "--output", str(output)]) == 0
    assert source.read_bytes() == before[source]
    report = output.with_name(output.name + ".recovery.json")
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["plan"]["ready_count"] == 1
    assert result["written_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert commands.run_plugin_commands(["repair", *common, "--output", str(output)]) == 1
    assert commands.run_plugin_commands(["repair", *common, "--output", str(source)]) == 1


def test_batch_preserves_duplicate_basenames_and_deduplicates_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, route, target = fixture(tmp_path)
    monkeypatch.setattr(commands, "load_recovery_routes", lambda: (route,))
    second = tmp_path / "other" / source.name
    second.parent.mkdir()
    second.write_bytes(source.read_bytes())
    assert target.path is not None
    output = tmp_path / "copies"
    assert (
        commands.run_plugin_commands(
            [
                "repair",
                str(source),
                str(second),
                str(source),
                "--output-dir",
                str(output),
                "--plugin-dir",
                str(target.path.parent),
            ]
        )
        == 0
    )
    assert (output / "Project" / "song.als").is_file()
    assert (output / "other" / "song.als").is_file()
    assert len(list(output.rglob("*.als"))) == 2


def test_atomic_new_output_preserves_existing_file_and_unrelated_temp(tmp_path: Path) -> None:
    target = tmp_path / "set.als"
    legacy_temp = tmp_path / "set.als.tmp"
    legacy_temp.write_bytes(b"unrelated")
    live_document.write_new(target, b"first")
    with pytest.raises(FileExistsError):
        live_document.write_new(target, b"second")
    assert target.read_bytes() == b"first"
    assert legacy_temp.read_bytes() == b"unrelated"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["set.als", "set.als.tmp"]


def test_directory_batch_retains_input_root_with_one_nested_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, route, target = fixture(tmp_path)
    monkeypatch.setattr(commands, "load_recovery_routes", lambda: (route,))
    nested = source.parent / "versions" / source.name
    nested.parent.mkdir()
    source.rename(nested)
    original = nested.read_bytes()
    assert target.path is not None
    output = source.parent / "recovered"
    assert (
        commands.run_plugin_commands(
            [
                "repair",
                str(source.parent),
                "--output-dir",
                str(output),
                "--plugin-dir",
                str(target.path.parent),
            ]
        )
        == 0
    )
    assert (output / "versions" / source.name).is_file()
    assert not (output / source.name).exists()
    assert nested.read_bytes() == original


def test_cli_truncated_gzip_returns_json_error_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source, route, target = fixture(tmp_path)
    monkeypatch.setattr(commands, "load_recovery_routes", lambda: (route,))
    source.write_bytes(b"\x1f\x8b\x08\0")
    assert target.path is not None
    output = tmp_path / "out.als"
    assert (
        commands.run_plugin_commands(
            [
                "repair",
                str(source),
                "--output",
                str(output),
                "--plugin-dir",
                str(target.path.parent),
                "--json",
            ]
        )
        == 1
    )
    result = json.loads(capsys.readouterr().out)
    assert "corrupt or truncated gzip" in result["error"]
    assert source.read_bytes() == b"\x1f\x8b\x08\0"
    assert not output.exists()


def test_packaged_profiles_have_explicit_bank_program_and_binary_proof() -> None:
    routes = load_recovery_routes()
    assert len(routes) >= 9
    assert all(state.program_numbers for route in routes for state in route.states)
    assert all(binary.name == Path(binary.file_name).stem for route in routes for binary in route.targets)
