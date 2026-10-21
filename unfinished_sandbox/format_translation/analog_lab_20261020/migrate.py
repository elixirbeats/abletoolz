"""Experimentally migrate a bare Lab 3/4 chunk through installed Analog Lab V.

Only the outer preset identity is edited before native conversion. A recovered
state is exported after successive fresh-host saves converge byte for byte.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arturia_archive import single_float_map
from arturia_preset_metadata import parse_root_metadata, retarget, split_payloads
from host_process import run_worker


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def numeric_fields(raw: bytes) -> dict[str, str]:
    return {
        f"{index}/{field.name}": field.value_text
        for index, body in enumerate(split_payloads(raw))
        for field in single_float_map(body).fields
    }


def text_inventory(path: Path) -> list[tuple[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("parameters"), list):
        raise ValueError("Missing native parameter inventory")
    result = []
    for row in data["parameters"]:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not isinstance(row.get("display"), str):
            raise ValueError("Invalid native parameter display record")
        result.append((row["name"], row["display"]))
    return result


def module_identity(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    modules = data.get("loaded_arturia_modules") if isinstance(data, dict) else None
    if not isinstance(modules, list) or not modules:
        raise ValueError("Missing loaded Arturia module inventory")
    if not all(isinstance(module, str) for module in modules):
        raise ValueError("Invalid loaded module path")
    return {module: digest(Path(module).read_bytes()) for module in modules}


def migrate(source: Path, plugin: Path, output: Path, python: str, max_cycles: int) -> dict[str, object]:
    original = source.read_bytes()
    roots = [parse_root_metadata(body) for body in split_payloads(original)]
    if any(root.instrument_id != 1 for root in roots):
        raise ValueError("This experiment requires observed Lab 3/4 outer preset ID 1")
    output.mkdir(parents=True, exist_ok=False)
    (output / "source.bare").write_bytes(original)
    candidate = retarget(original, 60, None)
    candidate_path = output / "identity_retargeted.bare"
    candidate_path.write_bytes(candidate)
    helper = Path(__file__).resolve().parent / "runtime" / "analog_runtime.py"
    report: dict[str, object] = {
        "status": "unverified",
        "source": str(source.resolve()),
        "source_sha256": digest(original),
        "plugin": str(plugin.resolve()),
        "plugin_sha256": digest(plugin.read_bytes()),
        "identity_edit": "Outer schema-7 preset ID 1 -> 60 only; original versions and all numeric fields retained",
        "target_id_authority": "corpus/instrument_id_table.json; measured Analog Lab V root ID",
        "source_preset_name": roots[1].name,
        "original_sound_equivalence": "unverified; no original-generation render",
        "source_set_modified": False,
    }
    cycles: list[dict[str, object]] = []
    report["cycles"] = cycles
    previous: bytes | None = None
    previous_path: Path | None = None
    dependencies: dict[str, str] | None = None
    try:
        for cycle in range(1, max_cycles + 1):
            name = f"cycle{cycle:02d}"
            path = output / f"{name}.json"
            command = [
                python,
                str(helper),
                "dawdreamer",
                "--plugin",
                str(plugin.resolve()),
                "--out",
                str(path),
                "--source",
                str(candidate_path),
                "--child",
                "--source-kind",
                "cross-generation" if cycle == 1 else "normalized-reload",
            ]
            run_worker(command, output, name, 30)
            current_path = output / f"{name}.readback.bare"
            current = current_path.read_bytes()
            current_roots = [parse_root_metadata(body) for body in split_payloads(current)]
            if any(root.instrument_id != 60 or not root.version for root in current_roots):
                raise ValueError("Native serializer did not retain a current outer identity and version")
            if current_roots[1].name != roots[1].name:
                raise ValueError("Native readback did not retain the source preset name")
            baseline = (output / f"{name}.baseline.state").read_bytes()
            if len(baseline) < 160 or baseline[8:12] != b"FBCh":
                raise ValueError("Expected captured VST2 FBCh template")
            template_roots = [parse_root_metadata(body) for body in split_payloads(baseline[160:])]
            if any(root.instrument_id != 60 for root in template_roots):
                raise ValueError("Target binary did not identify itself as the measured Analog Lab V family")
            if current == baseline[160:]:
                raise ValueError("Source was ignored: readback equals the fresh default")
            observed_dependencies = module_identity(path)
            if dependencies is not None and dependencies != observed_dependencies:
                raise ValueError("Loaded engine binaries changed during migration")
            dependencies = observed_dependencies
            report["runtime_dependencies"] = dependencies
            fields = numeric_fields(current)
            before = numeric_fields(previous) if previous is not None else {}
            changes = [key for key in fields.keys() | before.keys() if fields.get(key) != before.get(key)]
            cycle_report: dict[str, object] = {
                "cycle": cycle,
                "bare_sha256": digest(current),
                "length": len(current),
                "native_state": f"{name}.state",
                "matches_previous_raw": previous == current,
                "numeric_changes_from_previous": changes if previous is not None else None,
                "outer_versions": [root.version for root in current_roots],
            }
            cycles.append(cycle_report)
            if previous == current and previous_path is not None:
                if text_inventory(path) != text_inventory(previous_path.with_suffix(".json")):
                    raise ValueError("Identical saved states produced different native parameter displays")
                left = np.load(previous_path.with_suffix(".audio.npy"), allow_pickle=False)
                right = np.load(path.with_suffix(".audio.npy"), allow_pickle=False)
                if left.shape != right.shape or not np.isfinite(left).all() or not np.isfinite(right).all():
                    raise ValueError("Invalid native audio comparison")
                report["audio_peak_delta"] = float(np.max(np.abs(left - right)))
                report["audio_reference_peak"] = float(np.max(np.abs(left)))
                report["audio_scope"] = "Two fresh current hosts; C4 velocity 100 at 44.1 kHz, 1.2 seconds"
                verified = output / "verified.state"
                verified.write_bytes(path.with_suffix(".state").read_bytes())
                (output / "verified.bare").write_bytes(current)
                report.update(
                    {
                        "status": "verified_stable_current_state",
                        "verified_state": str(verified),
                        "verified_bare_sha256": digest(current),
                        "native_parameter_displays_equal": True,
                    }
                )
                return report
            previous, previous_path = current, path
            candidate_path = current_path
        raise ValueError(f"Native state did not converge within {max_cycles} fresh-host cycles")
    except (ValueError, RuntimeError, OSError) as error:
        report.update({"status": "failed", "error": str(error)})
        raise
    finally:
        (output / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="Exact bare two-part Lab 3/4 ALS buffer")
    parser.add_argument("--plugin", required=True, type=Path, help="Installed Analog Lab V VST2 DLL")
    parser.add_argument("--out", required=True, type=Path, help="Unused output directory")
    parser.add_argument("--python", default=sys.executable, help="Interpreter with DawDreamer and pedalboard")
    parser.add_argument("--max-cycles", type=int, choices=range(2, 6), default=4)
    args = parser.parse_args()
    result = migrate(args.source.resolve(), args.plugin.resolve(), args.out.resolve(), args.python, args.max_cycles)
    print(json.dumps({key: result[key] for key in ("status", "verified_state", "verified_bare_sha256")}, indent=2))


if __name__ == "__main__":
    main()
