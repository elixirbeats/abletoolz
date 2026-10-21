"""Observe saved plugin state while moving named controls through DawDreamer.

Run with --plugin PATH --parameter NAME [--parameter NAME] --out DIRECTORY.
Each run owns a disposable host process with a 60-second timeout. Snapshots are
serialized state buffers, not process heap dumps. Byte ranges are candidates;
compressed or variable-length data needs decoding before field attribution.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Protocol, cast

from fabf_state import parse_fabf
from juce_container import unwrap

from abletoolz.plugins.state.fabfilter import FfbsState
from abletoolz.plugins.state.families import vstw_chunk

SAMPLE_RATE = 44100
BLOCK = 512
POSITIONS = (0.0, 0.25, 0.5, 0.75, 1.0)


class Plugin(Protocol):
    def __getstate__(self) -> dict[str, object]: ...
    def get_plugin_parameter_size(self) -> int: ...
    def get_parameter_name(self, index: int) -> str: ...
    def get_parameter(self, index: int) -> float: ...
    def get_parameter_text(self, index: int) -> str: ...
    def set_parameter(self, index: int, value: float) -> bool: ...
    def save_state(self, filepath: str) -> None: ...
    def load_state(self, filepath: str) -> None: ...


class Engine(Protocol):
    def make_plugin_processor(self, name: str, path: str) -> Plugin: ...
    def load_graph(self, graph: list[tuple[Plugin, list[str]]]) -> bool: ...
    def render(self, seconds: float) -> bool: ...


class EngineFactory(Protocol):
    def __call__(self, sample_rate: int, block: int) -> Engine: ...


def capture(plugin: Plugin) -> bytes:
    state = plugin.__getstate__()["plugin_state"]
    if not isinstance(state, bytes):
        raise TypeError("DawDreamer did not return a serialized bytes buffer")
    return state


def parts(raw: bytes) -> dict[str, bytes]:
    if raw.startswith(b"VC2!") and b"VST3PluginState" in raw[:150]:
        state = unwrap(raw)
        return {"processor": state.component, "controller": state.controller}
    if raw[:4] == b"CcnK" and raw[8:12] == b"FBCh":
        size = struct.unpack_from(">I", raw, 156)[0]
        if 160 + size != len(raw):
            raise ValueError("Unexpected VST2 chunk length")
        return {"chunk": raw[160:]}
    return {"host_state": raw}


def fields(blob: bytes) -> dict[str, float | str]:
    if blob.startswith(b"VstW"):
        chunk = vstw_chunk(blob)
        if chunk is None:
            raise ValueError("Malformed VstW envelope")
        return fields(chunk)
    if blob.startswith(b"FFBS"):
        return {f"float[{i}]": v for i, v in enumerate(FfbsState.parse(blob).parameters)}
    if blob.startswith(b"FabF"):
        return {f"float[{i}]": v for i, v in enumerate(parse_fabf(blob).values)}
    if blob.startswith(b"VC2!"):
        size = struct.unpack_from("<I", blob, 4)[0]
        blob = blob[8 : 8 + size]
    try:
        root = ET.fromstring(blob.rstrip(b"\x00"))
    except (ET.ParseError, ValueError):
        return {}
    result: dict[str, float | str] = {}

    def visit(element: ET.Element, path: str) -> None:
        for key, value in element.attrib.items():
            result[f"{path}/@{key}"] = value
        if element.text and element.text.strip():
            result[f"{path}/text()"] = element.text.strip()
        for index, child in enumerate(element):
            visit(child, f"{path}/{child.tag}[{index}]")

    visit(root, f"/{root.tag}")
    return result


def difference(before: bytes, after: bytes) -> dict[str, object]:
    offsets = [i for i in range(min(len(before), len(after))) if before[i] != after[i]]
    offsets.extend(range(min(len(before), len(after)), max(len(before), len(after))))
    ranges: list[list[int]] = []
    for offset in offsets:
        if ranges and ranges[-1][1] == offset:
            ranges[-1][1] = offset + 1
        else:
            ranges.append([offset, offset + 1])
    old_fields, new_fields = fields(before), fields(after)
    changed_fields = {
        key: [old_fields.get(key), new_fields.get(key)]
        for key in old_fields.keys() | new_fields.keys()
        if old_fields.get(key) != new_fields.get(key)
    }
    return {
        "before_bytes": len(before),
        "after_bytes": len(after),
        "changed_bytes": len(offsets),
        "ranges_half_open": ranges,
        "decoded_changes": changed_fields,
    }


def measure(module: Path, wanted: list[str], out: Path) -> dict[str, object]:
    daw = importlib.import_module("dawdreamer")
    # The native extension exposes this documented constructor without Python stubs.
    factory = cast(EngineFactory, daw.RenderEngine)
    engine = factory(SAMPLE_RATE, BLOCK)
    plugin = engine.make_plugin_processor("state_probe", str(module))
    engine.load_graph([(plugin, [])])

    def settle() -> None:
        engine.render(BLOCK / SAMPLE_RATE)

    settle()
    inventory = [
        {
            "index": i,
            "name": plugin.get_parameter_name(i),
            "value": plugin.get_parameter(i),
            "text": plugin.get_parameter_text(i),
        }
        for i in range(plugin.get_plugin_parameter_size())
    ]
    chosen: list[tuple[int, str]] = []
    for name in wanted:
        matches = [
            (i, plugin.get_parameter_name(i))
            for i in range(len(inventory))
            if plugin.get_parameter_name(i).casefold() == name.casefold()
        ]
        if len(matches) != 1:
            raise ValueError(f"{name!r} matched {len(matches)} controls; see inventory: {inventory}")
        chosen.extend(matches)

    initial = capture(plugin)
    initial_path = out / "initial.state"
    initial_path.write_bytes(initial)
    # load_state initializes the editor; record that change before measuring knobs.
    plugin.load_state(str(initial_path))
    settle()
    baseline = capture(plugin)
    baseline_path = out / "baseline.state"
    baseline_path.write_bytes(baseline)
    plugin.save_state(str(out / "baseline_file.state"))
    memory_matches_file = baseline == (out / "baseline_file.state").read_bytes()
    baseline_parts = parts(baseline)
    repeat_diffs = []
    for _ in range(3):
        settle()
        repeat_diffs.append(difference(baseline, capture(plugin)))
    rows: list[dict[str, object]] = []
    for index, name in chosen:
        samples: list[dict[str, object]] = []
        for step, position in enumerate(POSITIONS):
            plugin.load_state(str(baseline_path))
            settle()
            pretrial = capture(plugin)
            plugin.set_parameter(index, position)
            immediate = capture(plugin)
            settle()
            saved = capture(plugin)
            stem = f"parameter_{index:04d}_step_{step}"
            (out / f"{stem}.state").write_bytes(saved)
            saved_parts = parts(saved)
            for part, data in saved_parts.items():
                (out / f"{stem}.{part}.bin").write_bytes(data)
            samples.append(
                {
                    "requested": position,
                    "readback": plugin.get_parameter(index),
                    "text": plugin.get_parameter_text(index),
                    "snapshot": f"{stem}.state",
                    "baseline_restored_before_trial": pretrial == baseline,
                    "immediate_matches_settled": immediate == saved,
                    "parts": {key: difference(baseline_parts[key], data) for key, data in saved_parts.items()},
                }
            )
        plugin.load_state(str(baseline_path))
        settle()
        rows.append(
            {"index": index, "name": name, "samples": samples, "restored_after_sweep": capture(plugin) == baseline}
        )
    return {
        "plugin": str(module),
        "plugin_sha256": hashlib.sha256(module.read_bytes()).hexdigest(),
        "dawdreamer_version": importlib.metadata.version("dawdreamer"),
        "sample_rate": SAMPLE_RATE,
        "block_size": BLOCK,
        "positions": POSITIONS,
        "inventory": inventory,
        "memory_matches_save_file": memory_matches_file,
        "baseline_sha256": hashlib.sha256(baseline).hexdigest(),
        "load_initialization_change": difference(initial, baseline),
        "baseline_parts": {
            key: {"bytes": len(data), "prefix_hex": data[:16].hex()} for key, data in baseline_parts.items()
        },
        "baseline_repeat_diffs": repeat_diffs,
        "parameters": rows,
        "limits": [
            "Serialized state only; no heap-layout claims.",
            "Byte ranges are observations, not proven field boundaries.",
            "No audio equivalence or state-write validation in this probe.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--parameter", action="append", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    out = args.out.resolve()
    if not args.child:
        out.mkdir(parents=True, exist_ok=False)
        try:
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:], "--child"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except subprocess.TimeoutExpired:
            print(f"Plugin probe timed out; partial snapshots: {out}", file=sys.stderr)
            return 2
        (out / "child_stdout.log").write_text(result.stdout, encoding="utf-8")
        (out / "child_stderr.log").write_text(result.stderr, encoding="utf-8")
        print(result.stdout)
        if result.returncode:
            print(result.stderr, file=sys.stderr)
        return result.returncode
    report = measure(args.plugin.resolve(), args.parameter, out)
    (out / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote {out / 'result.json'}", flush=True)
    # Some plugins hang during native teardown after the measurements are saved.
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
