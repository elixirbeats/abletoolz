"""Native recovery routes: what a device becomes through one, and everything a route refuses.

Each route here is synthetic but shaped like a measured one: a source bank and
program it was verified on, and the native binary, by hash, that accepts it.
The device is a Live 9 set built inline, so every field a route checks is in view.
"""

from __future__ import annotations

import copy
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from native_routes import element, native_route
from pydantic import ValidationError

from abletoolz.formats import hex_text, live_document, path_data
from abletoolz.plugins.container.device import PluginDevices
from abletoolz.plugins.conversion import Answer, Edit, write
from abletoolz.plugins.recovery import recover
from abletoolz.plugins.recovery_routes import (
    RecoveryRoute,
    RecoveryTarget,
    TargetStatus,
    load_recovery_routes,
    resolve_recovery_targets,
)


def planned(root: ET.Element, target: RecoveryTarget) -> Answer:
    """What the set's first plugin device becomes through ``target``'s route."""
    return recover(PluginDevices(root)[0], target)


def refused(root: ET.Element, target: RecoveryTarget) -> str:
    """Why the set's first plugin device cannot take ``target``'s route; it must not."""
    answer = planned(root, target)
    assert isinstance(answer, str), answer
    return answer


def recovered(root: ET.Element, answer: Answer) -> ET.Element:
    """A copy of ``root`` with ``answer``, a write and not a refusal, written to its first plugin device."""
    assert not isinstance(answer, str), answer
    repaired = copy.deepcopy(root)
    write(PluginDevices(repaired)[0], answer)
    return repaired


@pytest.mark.parametrize("old", [False, True])
def test_recovery_changes_only_reference_and_preserves_entire_control_graph(tmp_path: Path, old: bool) -> None:
    source, _route, target = native_route(tmp_path, old=old)
    original = source.read_bytes()
    root = live_document.parse(original)
    pristine = ET.tostring(root)
    answer = planned(root, target)
    assert isinstance(answer, Edit)
    repaired = recovered(root, answer)
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
    source, _route, target = native_route(tmp_path)
    root = live_document.read(source)
    element(element(root, ".//VstPluginInfo"), path).set("Value", value)
    before = ET.tostring(root)
    reason = refused(root, target)
    assert "differs" in reason or "outside the measured bank" in reason
    assert ET.tostring(root) == before


@pytest.mark.parametrize("kind", ["bank", "program", "control", "path", "preset"])
def test_unknown_state_program_and_ambiguous_device_are_refused(tmp_path: Path, kind: str) -> None:
    source, _route, target = native_route(tmp_path)
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
    unverified = "Saved bank and selected program have not been verified"
    assert (refused(root, target) == unverified) is (kind in ("bank", "program"))


@pytest.mark.parametrize("changed", ["binary", "gone"])
def test_a_native_binary_changed_since_planning_is_refused_before_any_write(tmp_path: Path, changed: str) -> None:
    """A set changed since planning is the plan's to refuse, by its hash; the binary is this module's."""
    source, _route, target = native_route(tmp_path)
    root = live_document.read(source)
    assert isinstance(planned(root, target), Edit)
    assert target.path is not None
    if changed == "binary":
        target.path.write_bytes(target.path.read_bytes() + b"changed")
        assert refused(root, target) == "Native plugin binary changed after it was verified"
    else:
        target.path.unlink()
        refused(root, target)


def test_resolver_distinguishes_missing_unverified_and_ambiguous(tmp_path: Path) -> None:
    _source, route, target = native_route(tmp_path)
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


def test_packaged_profiles_have_explicit_bank_program_and_binary_proof() -> None:
    routes = load_recovery_routes()
    assert len(routes) >= 9
    assert all(state.program_numbers for route in routes for state in route.states)
    assert all(binary.name == Path(binary.file_name).stem for route in routes for binary in route.targets)


@pytest.mark.parametrize("configured", [False, True])
def test_program_capacity_recovery_updates_only_measured_counts_and_reference(tmp_path: Path, configured: bool) -> None:
    source, route, native = native_route(tmp_path)
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
    if configured:
        assert "need a parameter map" in refused(root, target)
        return
    answer = planned(root, target)
    assert isinstance(answer, Edit) and answer.programs == 4
    pristine = ET.tostring(root)
    repaired = recovered(root, answer)
    expected = copy.deepcopy(root)
    info = element(expected, ".//VstPluginInfo")
    element(info, "PlugName").set("Value", "Native")
    element(info, "Path").set("Value", native.path.as_posix())
    element(info, "NumberOfPrograms").set("Value", "4")
    element(info, "Preset/VstPreset/ProgramCount").set("Value", "4")
    assert ET.tostring(repaired) == ET.tostring(expected)
    assert ET.tostring(root) == pristine


@pytest.mark.parametrize(
    "problem", ["implicit_capacity", "smaller_capacity", "parameter_count", "missing_count", "float_bank"]
)
def test_program_capacity_route_refuses_unmeasured_layout_changes(tmp_path: Path, problem: str) -> None:
    _source, route, _native = native_route(tmp_path)
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
