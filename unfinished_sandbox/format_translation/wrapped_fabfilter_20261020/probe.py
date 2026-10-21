"""Bounded source and target probes for recovered FabFilter banks."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
from pydantic import BaseModel, Field

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))
sys.path.insert(0, str(LAB / "legacy_archive_20261020"))

from host_process import attach_worker, run_worker  # noqa: E402
from load_probe import isolate_desktop  # noqa: E402
from state_mapper import Host, Settings  # noqa: E402


class Change(BaseModel):
    index: int = Field(ge=0)
    value: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)


class Changes(BaseModel):
    controls: list[Change]


def worker(
    module: Path, output: Path, state: Path | None, scan: bool, selected: list[int], changes: Path | None
) -> None:
    attach_worker()
    isolate_desktop()
    host = Host(Settings(plugin=str(module), out=str(output), seconds=1.0))
    host.settle()
    baseline = host.capture()
    (output / "default.state").write_bytes(baseline)
    if state is not None:
        host.restore(state)
    if changes is not None:
        for change in Changes.model_validate_json(changes.read_text()).controls:
            if change.index >= host.plugin.get_plugin_parameter_size():
                raise ValueError("Change index exceeds the observed native inventory")
            host.set_control(change.index, change.value)
    restored = host.capture()
    (output / "restored.state").write_bytes(restored)
    parameters = host.parameters()
    (output / "inventory.json").write_text(
        json.dumps([row.model_dump() for row in parameters], indent=2), encoding="utf-8"
    )
    evidence: dict[str, object] = {
        "plugin": str(module),
        "plugin_sha256": hashlib.sha256(module.read_bytes()).hexdigest(),
        "host_rate": 44100,
        "source_set_engine_rate": None,
        "default_sha256": hashlib.sha256(baseline).hexdigest(),
        "restored_sha256": hashlib.sha256(restored).hexdigest(),
        "count": len(parameters),
    }
    if scan:
        rows = []
        indices = selected or [
            row.index for row in parameters if row.name.startswith("Band 1 ") or not row.name.startswith("Band ")
        ]
        restore_path = output / "restored.state"
        for index in indices:
            observations = []
            for step in range(17):
                host.restore(restore_path)
                value = step / 16
                host.set_control(index, value)
                captured = host.capture()
                path = output / f"parameter_{index:04d}_{step:02d}.state"
                path.write_bytes(captured)
                observations.append(
                    {"normalized": value, "text": host.plugin.get_parameter_text(index), "state": str(path)}
                )
            rows.append({"index": index, "name": parameters[index].name, "observations": observations})
        evidence["sweeps"] = rows
        host.restore(restore_path)
    noise = np.random.default_rng(4511).normal(0.0, 0.1, size=(2, 44100)).astype(np.float32)
    playback = host.engine.make_playback_processor("probe_input", noise)
    host.engine.load_graph([(playback, []), (host.plugin, ["probe_input"])])
    renders = []
    for take in range(2):
        host.restore(output / "restored.state")
        host.engine.render(1.0)
        audio = host.engine.get_audio()
        path = output / f"render{take}.npy"
        np.save(path, audio)
        renders.append({"path": str(path), "peak": float(np.max(np.abs(audio)))})
    evidence["renders"] = renders
    (output / "result.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps({"count": len(parameters), "done": str(output)}), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--scan", action="store_true")
    parser.add_argument("--indices", default="")
    parser.add_argument("--changes", type=Path)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    if args.worker:
        selected = [int(value) for value in args.indices.split(",") if value]
        worker(args.plugin, args.out, args.state, args.scan, selected, args.changes)
        return
    args.out.mkdir(parents=True, exist_ok=False)
    command = [
        args.python,
        str(Path(__file__).resolve()),
        "--worker",
        "--plugin",
        str(args.plugin),
        "--out",
        str(args.out),
    ]
    if args.state is not None:
        command.extend(["--state", str(args.state)])
    if args.scan:
        command.append("--scan")
    if args.indices:
        command.extend(["--indices", args.indices])
    if args.changes is not None:
        command.extend(["--changes", str(args.changes)])
    run_worker(command, args.out, "probe", 60)


if __name__ == "__main__":
    main()
