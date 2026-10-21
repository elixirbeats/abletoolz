"""Convert legacy Analog Lab VST2 devices in a new sandbox set copy."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

from abletoolz.formats import hex_text, live_document
from abletoolz.live_set.document import AbletonSet
from abletoolz.plugins.migration import Vst2Migration, Vst2Target

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host_process import run_worker
from migrate import migrate


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def required(parent: ET.Element, path: str) -> ET.Element:
    element = parent.find(path)
    if element is None:
        raise ValueError(f"Missing field: {path}")
    return element


def infos(root: ET.Element) -> list[ET.Element]:
    return [element for element in root.iter() if element.tag in ("VstPluginInfo", "Vst3PluginInfo")]


def without_selected(root: ET.Element, selected: set[int]) -> bytes:
    clone = copy.deepcopy(root)
    parents = {child: parent for parent in clone.iter() for child in parent}
    for index, info in enumerate(infos(clone)):
        if index in selected:
            replacement = ET.Element("MigratedPlugin", Ordinal=str(index))
            replacement.tail = info.tail
            parent = parents[info]
            position = list(parent).index(info)
            parent.remove(info)
            parent.insert(position, replacement)
    return ET.tostring(clone)


def migrate_set(source: Path, plugin: Path, reference: Path, output: Path, python: str) -> dict[str, object]:
    original = source.read_bytes()
    source_hash = digest(original)
    reference_bytes = reference.read_bytes()
    target_info = next(
        info
        for info in infos(live_document.parse(reference_bytes))
        if info.tag == "VstPluginInfo" and required(info, "PlugName").get("Value") == "Analog Lab V"
    )
    output.mkdir(parents=True, exist_ok=False)
    original_copy = output / "original.als"
    shutil.copy2(source, original_copy)
    live_set = AbletonSet(original_copy)
    original_tree = copy.deepcopy(live_set.root)
    selected = {
        index
        for index, info in enumerate(infos(live_set.root))
        if info.tag == "VstPluginInfo" and required(info, "PlugName").get("Value") in ("Analog Lab 3", "Analog Lab 4")
    }
    if not selected:
        raise ValueError("No supported legacy Analog Lab VST2 devices")
    inputs = output / "inputs"
    inputs.mkdir()
    actions: list[dict[str, object]] = []
    for index, info in enumerate(infos(live_set.root)):
        if index not in selected:
            continue
        name = required(info, "PlugName").get("Value", "")
        source_id = int(required(info, "UniqueId").get("Value", ""))
        bare = hex_text.decode(required(info, "Preset/VstPreset/Buffer").text)
        source_path = inputs / f"device{index:03d}.bare"
        source_path.write_bytes(bare)
        directory = output / f"device{index:03d}"
        native = migrate(source_path, plugin, directory, python, 4)
        native_inventory = json.loads((directory / "cycle01.json").read_text())
        full_state = (directory / "verified.state").read_bytes()
        if full_state[:4] != b"CcnK" or full_state[8:12] != b"FBCh":
            raise ValueError("Expected native FBCh target state")
        target_id = int.from_bytes(full_state[16:20], "big")
        if target_id != int(required(target_info, "UniqueId").get("Value", "")):
            raise ValueError("Native target ID disagrees with the real-set target identity")
        target = Vst2Target(
            "Analog Lab V",
            plugin,
            target_id,
            native_inventory["count"],
            int(required(target_info, "Flags").get("Value", "")),
        )
        converted = (directory / "verified.bare").read_bytes()
        migration = Vst2Migration(name, source_id, digest(bare), target, converted)
        applied = live_set.plugins.migrate_vst2(index, migration)
        actions.append(
            {
                "device": index,
                "source": applied.source,
                "target": applied.target,
                "source_state_sha256": applied.source_sha256,
                "target_state_sha256": applied.target_sha256,
                "native_result": str(directory / "result.json"),
                "native_status": native["status"],
                "target_parameter_count": target.parameter_count,
                "target_unique_id": target_id,
            }
        )
    destination = output / "Analog Lab V migrated.als"
    live_document.write(destination, live_document.encode(live_set.root))
    readback = live_document.read(destination)
    if without_selected(original_tree, selected) != without_selected(readback, selected):
        raise ValueError("Content outside selected plugin info elements changed")
    for action in actions:
        ordinal = action["device"]
        if not isinstance(ordinal, int):
            raise TypeError("Invalid device ordinal")
        index = ordinal
        read_info = infos(readback)[index]
        if required(read_info, "Preset/VstPreset/UniqueId").get("Value") != str(action["target_unique_id"]):
            raise ValueError("Serialized preset retained the old plugin identity")
        if required(read_info, "Preset/VstPreset/ParameterCount").get("Value") != str(action["target_parameter_count"]):
            raise ValueError("Serialized preset retained the old parameter count")
        bare = hex_text.decode(required(read_info, "Preset/VstPreset/Buffer").text)
        if digest(bare) != action["target_state_sha256"]:
            raise ValueError("Serialized set did not retain the prepared state")
        recheck = output / f"device{index:03d}" / "set_readback"
        source_path = recheck.with_suffix(".bare")
        source_path.write_bytes(bare)
        helper = Path(__file__).parent / "runtime" / "analog_runtime.py"
        run_worker(
            [
                python,
                str(helper),
                "dawdreamer",
                "--plugin",
                str(plugin),
                "--out",
                str(recheck.with_suffix(".json")),
                "--source",
                str(source_path),
                "--child",
                "--source-kind",
                "normalized-reload",
            ],
            recheck.parent,
            "set_readback",
            30,
        )
        if recheck.with_suffix(".readback.bare").read_bytes() != bare:
            raise ValueError("Native target did not retain the state extracted from the emitted set")
        action["emitted_set_native_state_echo"] = True
        action["preset_header_identity_and_count_verified"] = True
    if source.read_bytes() != original or original_copy.read_bytes() != original:
        raise ValueError("Original source or preserved sandbox copy changed")
    result: dict[str, object] = {
        "status": "migrated_copy_native_states_verified",
        "source": str(source),
        "source_sha256": source_hash,
        "preserved_original": str(original_copy),
        "output": str(destination),
        "output_sha256": digest(destination.read_bytes()),
        "plugin": str(plugin),
        "plugin_sha256": digest(plugin.read_bytes()),
        "target_identity_reference": str(reference),
        "target_identity_reference_sha256": digest(reference_bytes),
        "actions": actions,
        "outside_selected_plugin_info_unchanged": True,
        "source_unchanged": True,
        "limits": [
            "No configured plugin parameters were present; the library refuses them without an explicit mapper.",
            "Validation reopens the ALS and reloads each patch in a fresh native host. "
            "The complete set has not been opened in Live.",
            "Original-generation audio equivalence is unverified. "
            "Existing sample and unrelated plugin references are unchanged.",
        ],
    }
    (output / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--plugin", required=True, type=Path)
    parser.add_argument("--target-reference", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--python", required=True)
    args = parser.parse_args()
    result = migrate_set(
        args.source.resolve(), args.plugin.resolve(), args.target_reference.resolve(), args.out.resolve(), args.python
    )
    print(json.dumps({key: result[key] for key in ("status", "output", "source_unchanged")}, indent=2))


if __name__ == "__main__":
    main()
