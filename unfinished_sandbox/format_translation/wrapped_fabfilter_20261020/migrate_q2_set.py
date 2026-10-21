"""Exercise the registered Pro-Q 2 converter on a sandbox set copy."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np

from abletoolz.formats import hex_text, live_document
from abletoolz.live_set.document import AbletonSet
from abletoolz.plugins.container.format_translation import KNOWN_TRANSLATIONS, parse_config_targets
from abletoolz.plugins.repair import repair_set
from abletoolz.plugins.state.fabfilter import EditorState, FfbsState
from abletoolz.plugins.state.fxbk import LegacyBank

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host_process import run_worker
from juce_container import PluginState, unwrap, wrap


def required(parent: ET.Element, path: str) -> ET.Element:
    child = parent.find(path)
    if child is None:
        raise ValueError(f"Missing field: {path}")
    return child


def infos(root: ET.Element) -> list[ET.Element]:
    return [element for element in root.iter() if element.tag in ("VstPluginInfo", "Vst3PluginInfo")]


def without_device(root: ET.Element, ordinal: int) -> bytes:
    clone = copy.deepcopy(root)
    info = infos(clone)[ordinal]
    info.clear()
    info.tag = "SelectedPluginInfo"
    return ET.tostring(clone)


def run(source: Path, ordinal: int, plugin: Path, output: Path, python: str) -> dict[str, object]:
    original = source.read_bytes()
    source_hash = hashlib.sha256(original).hexdigest()
    output.mkdir(parents=True, exist_ok=False)
    preserved = output / "original.als"
    shutil.copy2(source, preserved)
    live_set = AbletonSet(preserved)
    before = copy.deepcopy(live_set.root)
    selected = infos(live_set.root)[ordinal]
    name = required(selected, "PlugName").get("Value", "")
    if name != "FabFilter Pro-Q 2.64":
        raise ValueError("This experiment requires the observed wrapped Pro-Q 2 identity")
    source_buffer = hex_text.decode(required(selected, "Preset/VstPreset/Buffer").text)
    source_preset_name = LegacyBank.parse(source_buffer).preset_name
    manifest = json.loads((Path(__file__).parent / "corpus/sources.json").read_text())
    source_record = next(
        record
        for record in manifest
        if record["set_sha256"] == source_hash
        and record["device"]["device_ordinal"] == ordinal
        and record["source_buffer_sha256"] == hashlib.sha256(source_buffer).hexdigest()
    )
    parents = {child: parent for parent in live_set.root.iter() for child in parent}
    owner = parents[parents[selected]]
    parameters = required(owner, "ParameterList")
    if any(required(parameter, "ParameterId").get("Value") != "-1" for parameter in parameters):
        raise ValueError("Configured parameters need an explicit automation translation")
    targets = {
        element.attrib["Id"]
        for element in parameters.iter()
        if element.tag in ("AutomationTarget", "ModulationTarget") and "Id" in element.attrib
    }
    if parameters.find(".//KeyMidi") is not None or any(
        element.tag == "PointeeId" and element.get("Value") in targets for element in live_set.root.iter()
    ):
        raise ValueError("Mapped controls need an explicit automation translation")
    target = KNOWN_TRANSLATIONS["FabFilter Pro-Q 3"]
    configured = parse_config_targets(
        {
            name: {
                "to": "vst3",
                "name": "Pro-Q 3",
                "uid": target.uid_fields,
                "state": "custom:fabfilter-q2-to-q3",
                "controller": "fabfilter-pro-q-3",
            }
        }
    )
    report = repair_set(live_set, targets=configured, loadable=lambda info: info is not selected)
    if report.fixed_count != 1:
        raise ValueError("Expected exactly one requested device replacement")
    destination = output / "Pro-Q 3 migrated.als"
    live_document.write(destination, live_document.encode(live_set.root))
    emitted = live_document.read(destination)
    if without_device(before, ordinal) != without_device(emitted, ordinal):
        raise ValueError("Content outside the requested plugin info changed")
    translated = infos(emitted)[ordinal]
    preset = required(translated, "Preset/Vst3Preset")
    component = hex_text.decode(required(preset, "ProcessorState").text)
    controller = hex_text.decode(required(preset, "ControllerState").text)
    expected = FfbsState.parse(component)
    if len(expected.parameters) != 358 or EditorState.parse(controller).preset_name != source_preset_name:
        raise ValueError("Unexpected converted preset structure or name")
    state = output / "emitted_pair.state"
    state.write_bytes(wrap(PluginState(component, controller)))
    native = output / "native_vst3"
    native.mkdir()
    probe = Path(__file__).with_name("probe.py")
    run_worker(
        [
            python,
            str(probe),
            "--worker",
            "--plugin",
            str(plugin),
            "--out",
            str(native),
            "--state",
            str(state),
        ],
        native,
        "probe",
        60,
    )
    restored = unwrap((native / "restored.state").read_bytes())
    actual = FfbsState.parse(restored.component)
    if len(actual.parameters) != len(expected.parameters):
        raise ValueError("Native VST3 changed the processor field count")
    deltas = [abs(a - b) for a, b in zip(actual.parameters, expected.parameters, strict=True)]
    if max(deltas) > 1e-5:
        raise ValueError("Native VST3 did not retain the converted processor values")
    if EditorState.parse(restored.controller).preset_name != EditorState.parse(controller).preset_name:
        raise ValueError("Native VST3 lost the converted preset name")
    if source.read_bytes() != original or preserved.read_bytes() != original:
        raise ValueError("Original set changed")
    source_audio = Path(__file__).parent / "q2_corpus" / source_record["slug"] / "render0.npy"
    left = np.load(source_audio, allow_pickle=False)
    right = np.load(native / "render0.npy", allow_pickle=False)
    second = np.load(native / "render1.npy", allow_pickle=False)
    if left.shape != right.shape or right.shape != second.shape:
        raise ValueError("Native audio shapes differ")
    if not all(np.isfinite(audio).all() for audio in (left, right, second)):
        raise ValueError("Nonfinite native audio")
    result: dict[str, object] = {
        "status": "sandbox_set_converted_vst3_state_verified",
        "source": str(source),
        "source_sha256": source_hash,
        "source_corpus_record": source_record["slug"],
        "source_native_render": str(source_audio),
        "source_unchanged": True,
        "device": ordinal,
        "source_name": name,
        "target_name": "Pro-Q 3",
        "target_class_id": target.uid_fields,
        "output": str(destination),
        "output_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        "target_plugin": str(plugin),
        "target_plugin_sha256": hashlib.sha256(plugin.read_bytes()).hexdigest(),
        "outside_selected_plugin_info_unchanged": True,
        "processor_fields_compared": len(deltas),
        "processor_max_delta": max(deltas),
        "preset_name_retained": True,
        "current_vst3_repeat_floor": float(np.max(np.abs(right - second))),
        "native_q2_to_q3_vst3_audio_peak_delta": float(np.max(np.abs(left - right))),
        "limits": [
            "Source rendering uses recovered native x64 Pro-Q 2, not the absent x86 binary or jBridge.",
            "No configured plugin parameters were present. Automation requires a separate translation.",
            "The emitted set was parsed and its state loaded natively; the full set has not been opened in Live.",
            "Audio evidence is one second of seeded stereo noise at 44100 Hz; source-set engine rate is unknown.",
        ],
    }
    (output / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--device", type=int, required=True)
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--python", required=True)
    args = parser.parse_args()
    result = run(args.source.resolve(), args.device, args.plugin.resolve(), args.out.resolve(), args.python)
    print(json.dumps({key: result[key] for key in ("status", "output", "source_unchanged")}, indent=2))


if __name__ == "__main__":
    main()
