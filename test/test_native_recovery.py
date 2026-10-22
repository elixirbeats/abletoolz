from __future__ import annotations

import copy
import hashlib
import json
import struct
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from pydantic import ValidationError

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
    first = live_document.encode(ET.Element("Ableton", Test="first"))
    second = live_document.encode(ET.Element("Ableton", Test="second"))
    live_document.write_new(target, first)
    with pytest.raises(FileExistsError):
        live_document.write_new(target, second)
    assert target.read_bytes() == first
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


def prepared_fixture(tmp_path: Path) -> tuple[Path, Path, Path, RecoveryTarget]:
    source, route, native = fixture(tmp_path)
    assert native.path is not None
    binary = native.path.with_suffix(".vst3")
    native.path.rename(binary)
    root = live_document.read(source)
    root.set("Creator", "Ableton Live 11.3.42")
    root.set("MinorVersion", "11.0_11300")
    info = element(root, ".//VstPluginInfo")
    ET.SubElement(info, "Category", Value="2")
    element(info, "NumberOfPrograms").set("Value", "1")
    preset = element(info, "Preset/VstPreset")
    element(preset, "ProgramCount").set("Value", "1")
    element(preset, "ProgramNumber").set("Value", "0")
    element(preset, "ParameterSettings").clear()
    element(root, ".//ParameterList").clear()
    source.write_bytes(live_document.encode(root))
    processor = tmp_path / "prepared.processor"
    processor.write_bytes(b"native converted processor\0with private patch bytes")
    record = route.model_dump(mode="json")
    record.update(
        {
            "state_mode": "prepared_vst3",
            "program_count": 1,
            "state_evidence": "prepared_native_readback",
            "states": [
                {
                    "sha256": route.states[0].sha256,
                    "program_numbers": [0],
                    "prepared": {
                        "processor": {
                            "path": processor.name,
                            "sha256": hashlib.sha256(processor.read_bytes()).hexdigest(),
                        }
                    },
                }
            ],
            "targets": [
                {
                    "file_name": binary.name,
                    "name": "Native",
                    "sha256": route.targets[0].sha256,
                    "format": "vst3",
                    "uid_fields": [-1, 2, 3, 4],
                }
            ],
        }
    )
    manifest = tmp_path / "prepared.json"
    manifest.write_text(json.dumps([record]), encoding="utf-8")
    loaded = load_recovery_routes((manifest,))
    target = next(
        target for target in resolve_recovery_targets(loaded, (binary.parent,)) if target.route.id == route.id
    )
    return source, manifest, processor, target


def test_prepared_vst3_conversion_preserves_everything_outside_selected_info(tmp_path: Path) -> None:
    source, _manifest, processor, target = prepared_fixture(tmp_path)
    original = source.read_bytes()
    root = live_document.parse(original)
    plan = plan_recovery(source, (target,))
    assert plan.ready_count == 1
    assert plan.devices[0].target_format == "vst3"
    assert plan.devices[0].prepared is not None
    repaired = apply_recovery_plan(root, plan)
    info = element(repaired, ".//Vst3PluginInfo")
    preset = element(info, "Preset/Vst3Preset")
    assert hex_text.decode(element(preset, "ProcessorState").text) == processor.read_bytes()
    assert not hex_text.decode(element(preset, "ControllerState").text)
    assert element(info, "Name").get("Value") == "Native"
    assert [field.get("Value") for field in element(info, "Uid")] == ["-1", "2", "3", "4"]
    parents = {child: parent for parent in repaired.iter() for child in parent}
    owner = parents[info]
    owner.remove(info)
    owner.append(copy.deepcopy(element(root, ".//VstPluginInfo")))
    assert ET.tostring(repaired) == ET.tostring(root)
    assert source.read_bytes() == original


@pytest.mark.parametrize(
    "problem",
    [
        "old_schema",
        "misleading_creator",
        "unknown_schema",
        "configured",
        "referenced",
        "missing_state",
        "changed_state",
    ],
)
def test_prepared_conversion_refuses_unmapped_controls_old_documents_and_bad_state(
    tmp_path: Path, problem: str
) -> None:
    source, _manifest, processor, target = prepared_fixture(tmp_path)
    root = live_document.read(source)
    if problem in ("old_schema", "misleading_creator", "unknown_schema"):
        if problem == "old_schema":
            root.set("Creator", "Ableton Live 10.0.6")
        root.set("MinorVersion", "unknown" if problem == "unknown_schema" else "10.0_370")
        expected = RecoveryStatus.SET_TOO_OLD
    elif problem in ("configured", "referenced"):
        parameter = ET.SubElement(element(root, ".//ParameterList"), "PluginFloatParameter")
        ET.SubElement(parameter, "ParameterId", Value="0" if problem == "configured" else "-1")
        ET.SubElement(parameter, "AutomationTarget", Id="77")
        expected = RecoveryStatus.PARAMETER_TRANSLATION_REQUIRED
    else:
        if problem == "missing_state":
            processor.unlink()
        else:
            processor.write_bytes(b"changed")
        expected = RecoveryStatus.PREPARED_STATE_UNAVAILABLE
    source.write_bytes(live_document.encode(root))
    original = source.read_bytes()
    plan = plan_recovery(source, (target,))
    assert plan.devices[0].status is expected
    assert plan.ready_count == 0
    assert source.read_bytes() == original


def test_prepared_file_is_rechecked_before_any_conversion(tmp_path: Path) -> None:
    source, _manifest, processor, target = prepared_fixture(tmp_path)
    plan = plan_recovery(source, (target,))
    root = live_document.read(source)
    original = ET.tostring(root)
    processor.write_bytes(b"changed after planning")
    with pytest.raises(RecoveryError, match="changed after native verification"):
        apply_recovery_plan(root, plan)
    assert ET.tostring(root) == original


def test_cli_accepts_explicit_prepared_manifest_without_writing_beside_source(tmp_path: Path) -> None:
    source, manifest, _processor, target = prepared_fixture(tmp_path)
    assert target.path is not None
    original = {path: path.read_bytes() for path in source.parent.rglob("*") if path.is_file()}
    common = [str(source), "--prepared", str(manifest), "--plugin-dir", str(target.path.parent), "--json"]
    assert commands.run_plugin_commands(["plan", *common]) == 0
    assert {path: path.read_bytes() for path in source.parent.rglob("*") if path.is_file()} == original
    output = tmp_path / "copies" / "converted.als"
    assert commands.run_plugin_commands(["repair", *common, "--output", str(output)]) == 0
    assert live_document.read(output).find(".//Vst3PluginInfo") is not None
    assert source.read_bytes() == original[source]


def test_prepared_route_refuses_ambiguous_identity_and_bank_selection(tmp_path: Path) -> None:
    _source, manifest, _processor, _target = prepared_fixture(tmp_path)
    record = json.loads(manifest.read_text(encoding="utf-8"))[0]
    record["targets"][0]["uid_fields"] = None
    with pytest.raises(ValidationError, match="class-ID fields"):
        RecoveryRoute.model_validate(record)
    record["targets"][0]["uid_fields"] = [-1, 2, 3, 4]
    record["program_count"] = 2
    with pytest.raises(ValidationError, match="single-program"):
        RecoveryRoute.model_validate(record)


@pytest.mark.parametrize("configured", [False, True])
def test_program_capacity_recovery_updates_only_measured_counts_and_reference(tmp_path: Path, configured: bool) -> None:
    source, route, native = fixture(tmp_path)
    target_binary = route.targets[0].model_copy(update={"parameter_count": 2, "program_count": 4})
    measured = RecoveryRoute.model_validate(
        route.model_dump()
        | {
            "state_mode": "reference_with_program_capacity",
            "targets": (target_binary,),
            "state_evidence": "legacy_reader_retention",
        }
    )
    assert native.path is not None
    target = resolve_recovery_targets((measured,), (native.path.parent,))[0]
    root = live_document.read(source)
    if not configured:
        element(root, ".//ParameterSettings").clear()
        element(root, ".//ParameterId").set("Value", "-1")
        element(root, ".//PointeeId").set("Value", "unrelated")
        value = element(root, ".//ParameterValue")
        value.remove(element(value, "KeyMidi"))
        source.write_bytes(live_document.encode(root))
    plan = plan_recovery(source, (target,))
    if configured:
        assert plan.devices[0].status is RecoveryStatus.PARAMETER_TRANSLATION_REQUIRED
        assert not plan.ready_count
        assert ET.tostring(apply_recovery_plan(root, plan)) == ET.tostring(root)
        return
    assert plan.ready_count == 1
    assert plan.devices[0].program_capacity_update == 4
    repaired = apply_recovery_plan(root, plan)
    expected = copy.deepcopy(root)
    info = element(expected, ".//VstPluginInfo")
    element(info, "PlugName").set("Value", "Native")
    element(info, "Path").set("Value", native.path.as_posix())
    element(info, "NumberOfPrograms").set("Value", "4")
    element(info, "Preset/VstPreset/ProgramCount").set("Value", "4")
    assert ET.tostring(repaired) == ET.tostring(expected)
    assert source.read_bytes() == live_document.encode(root)


@pytest.mark.parametrize(
    "problem", ["implicit_capacity", "smaller_capacity", "parameter_count", "missing_count", "float_bank"]
)
def test_program_capacity_route_refuses_unmeasured_layout_changes(tmp_path: Path, problem: str) -> None:
    _source, route, _native = fixture(tmp_path)
    record = route.model_dump(mode="json")
    record["state_mode"] = "reference_with_program_capacity"
    record["targets"][0]["parameter_count"] = 2
    record["targets"][0]["program_count"] = 4
    if problem == "implicit_capacity":
        record["state_mode"] = "reference"
    elif problem == "smaller_capacity":
        record["targets"][0]["program_count"] = 1
    elif problem == "parameter_count":
        record["targets"][0]["parameter_count"] = 3
    elif problem == "missing_count":
        record["targets"][0]["parameter_count"] = None
    else:
        record["preset_type"] = "FxBk"
    with pytest.raises(ValidationError):
        RecoveryRoute.model_validate(record)
