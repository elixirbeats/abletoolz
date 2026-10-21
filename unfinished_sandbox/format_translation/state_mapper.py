"""Map exposed controls to decoded save fields and test predicted state writes.

Invoked by derive_rig.py map. The coordinator never loads a DLL: scanning and
verification each run in a disposable process with a bounded timeout. Unknown
formats produce observations for investigation rather than invented mappings.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib
import importlib.metadata
import json
import math
import os
import re
import sys
import time
import traceback
from collections import Counter
from ctypes import wintypes
from functools import reduce
from pathlib import Path
from typing import Literal, Protocol, cast

import numpy as np
import transfer
from host_process import attach_worker, run_worker
from numpy.typing import NDArray
from pydantic import BaseModel, Field
from state_codec import Scalar, View, decode, equivalent, number


class Settings(BaseModel):
    mode: Literal["scan", "verify"] = "scan"
    plugin: str
    out: str
    names: list[str] = Field(default_factory=list)
    indices: list[int] = Field(default_factory=list)
    points: int = Field(default=65, ge=3, le=257)
    sample_rate: int = Field(default=44100, ge=8000, le=192000)
    block: int = 512
    holdouts: list[float] = Field(default_factory=lambda: [0.173, 0.371, 0.683])
    verify_limit: int = Field(default=0, ge=0)
    seconds: float = Field(default=0.25, gt=0.0, le=2.0)
    settle_ms: int = Field(default=0, ge=0, le=1000)
    baseline_state: str | None = None
    experimental_arturia: bool = False
    midi_note: int | None = Field(default=None, ge=0, le=127)


class Parameter(BaseModel):
    index: int
    name: str
    value: float
    text: str


class Candidate(BaseModel):
    field: str
    model: dict[str, object]
    residual: float
    positions: list[float]
    values: list[float]


class Row(BaseModel):
    parameter: Parameter
    status: str
    candidates: list[Candidate] = Field(default_factory=list)
    side_effect_fields: list[str] = Field(default_factory=list)
    restoration_failures: int = 0
    queued_changes: int = 0
    snapshot_files: list[str] = Field(default_factory=list)
    observed_texts: list[str] = Field(default_factory=list)
    changed_byte_counts: list[int] = Field(default_factory=list)


class Scan(BaseModel):
    plugin: str
    plugin_sha256: str
    dawdreamer_version: str
    sample_rate: int
    block: int
    layers: list[str]
    inventory: list[Parameter]
    baseline_file: str
    baseline_sha256: str
    memory_matches_file: bool
    volatile_fields: list[str]
    raw_baseline_stable: bool
    stable_fields: dict[str, Scalar]
    parameters: list[Row]
    contested_fields: dict[str, list[int]]
    unclaimed_varying_fields: list[str]
    source_baseline_state: str | None = None
    parameter_value_source: str = "DawDreamer host automation cache; native display text is read separately"
    baseline_import_raw_echo: bool | None = None
    baseline_import_field_mismatches: list[str] = Field(default_factory=list)
    source_baseline_field_count: int = 0
    plugin_identity: dict[str, str] = Field(default_factory=dict)
    runtime_dependencies: dict[str, str] = Field(default_factory=dict)


class HostedPlugin(Protocol):
    def __getstate__(self) -> dict[str, object]: ...
    def get_plugin_parameter_size(self) -> int: ...
    def get_parameter_name(self, index: int) -> str: ...
    def get_parameter(self, index: int) -> float: ...
    def get_parameter_text(self, index: int) -> str: ...
    def set_parameter(self, index: int, value: float) -> bool: ...
    def save_state(self, filepath: str) -> None: ...
    def load_state(self, filepath: str) -> None: ...
    def add_midi_note(self, note: int, velocity: int, start_time: float, duration: float) -> bool: ...


class Engine(Protocol):
    def make_plugin_processor(self, name: str, path: str) -> HostedPlugin: ...
    def make_playback_processor(self, name: str, data: NDArray[np.float32]) -> object: ...
    def load_graph(self, graph: list[tuple[object, list[str]]]) -> bool: ...
    def render(self, seconds: float) -> bool: ...
    def get_audio(self) -> NDArray[np.float32]: ...


class EngineFactory(Protocol):
    def __call__(self, sample_rate: int, block: int) -> Engine: ...


class Host:
    def __init__(self, settings: Settings) -> None:
        daw = importlib.import_module("dawdreamer")
        # The native constructor lacks stubs; the protocol covers the API used here.
        factory = cast(EngineFactory, daw.RenderEngine)
        self.engine = factory(settings.sample_rate, settings.block)
        self.plugin = self.engine.make_plugin_processor("mapped_plugin", str(Path(settings.plugin)))
        if self.plugin.get_plugin_parameter_size() < 1:
            raise RuntimeError("Host returned an unloaded or parameterless plugin")
        self.settings = settings
        self.engine.load_graph([(self.plugin, [])])

    def settle(self) -> None:
        self.engine.render(self.settings.block / self.settings.sample_rate)
        if self.settings.settle_ms:
            if sys.platform != "win32":
                raise RuntimeError("Message pumping is currently implemented for Windows only")
            message = wintypes.MSG()
            deadline = time.monotonic() + self.settings.settle_ms / 1000
            while time.monotonic() < deadline:
                while time.monotonic() < deadline and ctypes.windll.user32.PeekMessageW(
                    ctypes.byref(message), None, 0, 0, 1
                ):
                    ctypes.windll.user32.TranslateMessage(ctypes.byref(message))
                    ctypes.windll.user32.DispatchMessageW(ctypes.byref(message))
                time.sleep(0.001)

    def capture(self) -> bytes:
        value = self.plugin.__getstate__()["plugin_state"]
        if not isinstance(value, bytes):
            raise TypeError("Host returned no serialized state bytes")
        return value

    def restore(self, path: Path) -> bytes:
        self.plugin.load_state(str(path))
        self.settle()
        return self.capture()

    def set_control(self, index: int, value: float) -> tuple[bytes, bytes]:
        before = self.capture()
        before_text = self.plugin.get_parameter_text(index)
        self.plugin.set_parameter(index, value)
        immediate = self.capture()
        self.settle()
        settled = self.capture()
        # JUCE's ValueTree timer can back off to 500 ms while idle.
        deadline = time.monotonic() + 1.0
        while (
            self.settings.settle_ms
            and settled == before
            and self.plugin.get_parameter_text(index) != before_text
            and time.monotonic() < deadline
        ):
            self.settle()
            settled = self.capture()
        return immediate, settled

    def parameters(self) -> list[Parameter]:
        return [
            Parameter(
                index=i,
                name=self.plugin.get_parameter_name(i),
                value=self.plugin.get_parameter(i),
                text=self.plugin.get_parameter_text(i),
            )
            for i in range(self.plugin.get_plugin_parameter_size())
        ]


def _same_fields(expected: dict[str, Scalar], actual: View) -> bool:
    return all(key in actual.fields and equivalent(value, actual.fields[key]) for key, value in expected.items())


def plugin_identity(module: Path) -> dict[str, str]:
    identity = {"module_path": str(module.resolve()), "module_sha256": hashlib.sha256(module.read_bytes()).hexdigest()}
    if module.name.lower().endswith(".64.dll"):
        config = module.with_suffix(".txt")
        raw = config.read_bytes()
        quoted = re.search(rb'"([^"\r\n]+\.dll)"', raw, flags=re.IGNORECASE)
        if quoted is None:
            raise ValueError("jBridge configuration lacks a quoted target DLL")
        target = Path(quoted[1].decode("utf-8"))
        if not target.is_absolute() or not target.is_file():
            raise ValueError("jBridge target must resolve to an existing absolute DLL path")
        identity.update(
            {
                "jbridge_config_path": str(config.resolve()),
                "jbridge_config_sha256": hashlib.sha256(raw).hexdigest(),
                "jbridge_target_path": str(target.resolve()),
                "jbridge_target_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            }
        )
    return identity


def arturia_engine_identity() -> dict[str, str]:
    if sys.platform != "win32":
        raise RuntimeError("Arturia runtime module inventory is implemented for Windows only")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32]
    psapi.EnumProcessModules.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    modules = (ctypes.c_void_p * 2048)()
    needed = ctypes.c_uint32()
    if not psapi.EnumProcessModules(kernel.GetCurrentProcess(), modules, ctypes.sizeof(modules), ctypes.byref(needed)):
        raise ctypes.WinError(ctypes.get_last_error())
    if needed.value > ctypes.sizeof(modules):
        raise ValueError("Runtime module inventory exceeded its buffer")
    dependencies: dict[str, str] = {}
    for index in range(needed.value // ctypes.sizeof(ctypes.c_void_p)):
        path = ctypes.create_unicode_buffer(32768)
        length = kernel.GetModuleFileNameW(modules[index], path, len(path))
        if not length or length >= len(path):
            raise RuntimeError("Could not read an untruncated runtime module path")
        if "arturia" in path.value.casefold():
            module = Path(path.value)
            dependencies[str(module.resolve())] = hashlib.sha256(module.read_bytes()).hexdigest()
    if not dependencies:
        raise ValueError("No Arturia runtime modules observed for the experimental decoder")
    return dependencies


def fit_candidate(field: str, positions: tuple[float, ...], values: tuple[float, ...]) -> Candidate:
    declared_steps = len(set(values)) if len(set(values)) < min(32, len(positions)) else None
    fit = transfer.fit(positions, values, declared_steps)
    model = transfer.to_json(fit.transfer)
    residual = fit.residual
    if isinstance(fit.transfer, transfer.TableTransfer):
        distinct = sorted({round(value * 1_000_000) for value in values})
        quantum_int = reduce(math.gcd, (b - a for a, b in zip(distinct, distinct[1:], strict=False)), 0)
        quantum = quantum_int / 1_000_000
        span = values[-1] - values[0]
        if quantum > 0 and 1 < abs(span) / quantum <= 4096:
            errors = [
                abs(values[0] + quantum * math.floor((span * position) / quantum + 0.5) - value)
                for position, value in zip(positions, values, strict=True)
            ]
            if max(errors) <= 1e-6 * max(abs(span), 1.0):
                model = {
                    "kind": "quantized-linear",
                    "intercept": values[0],
                    "slope": span,
                    "quantum": quantum,
                    "rounding": "half-up",
                }
                residual = max(errors)
    return Candidate(field=field, model=model, residual=residual, positions=list(positions), values=list(values))


def predict(candidate: Candidate, position: float) -> float:
    if candidate.model["kind"] != "quantized-linear":
        return transfer.from_json(candidate.model).apply(position)

    def coefficient(name: str) -> float:
        value = candidate.model[name]
        if not isinstance(value, (int, float)):
            raise ValueError("Invalid quantized-linear coefficient")
        return float(value)

    intercept, slope, quantum = coefficient("intercept"), coefficient("slope"), coefficient("quantum")
    return intercept + quantum * math.floor(slope * position / quantum + 0.5)


def scan(settings: Settings) -> Scan:
    out = Path(settings.out)
    identity = plugin_identity(Path(settings.plugin))
    host = Host(settings)
    host.settle()
    initial_path = out / "initial.state"
    initial_path.write_bytes(Path(settings.baseline_state).read_bytes() if settings.baseline_state else host.capture())
    baseline = host.restore(initial_path)

    def view_of(data: bytes) -> View:
        return decode(data, experimental_arturia=settings.experimental_arturia)

    imported = view_of(initial_path.read_bytes())
    imported_back = view_of(baseline)
    import_mismatches = [
        key
        for key, value in imported.fields.items()
        if key not in imported_back.fields or not equivalent(value, imported_back.fields[key])
    ]
    baseline_path = out / "baseline.state"
    baseline_path.write_bytes(baseline)
    host.plugin.save_state(str(out / "baseline_file.state"))
    view = view_of(baseline)
    repeats: list[bytes] = []
    for _ in range(3):
        host.settle()
        repeats.append(host.capture())
    repeated_views = [view_of(raw) for raw in repeats]
    volatile = {
        key
        for repeated in repeated_views
        for key, value in view.fields.items()
        if key not in repeated.fields or not equivalent(value, repeated.fields[key])
    }
    stable = {key: value for key, value in view.fields.items() if key not in volatile}
    inventory = host.parameters()
    selected = set(settings.indices)
    for name in settings.names:
        matches = [p.index for p in inventory if p.name.casefold() == name.casefold()]
        if len(matches) != 1:
            raise ValueError(f"{name!r} matches {len(matches)} controls; select duplicate names by --index")
        selected.update(matches)
    if not selected:
        selected.update(p.index for p in inventory)
    if not selected.issubset({p.index for p in inventory}):
        raise ValueError("Parameter index outside the plugin's inventory")
    positions = tuple(i / (settings.points - 1) for i in range(settings.points))
    rows: list[Row] = []
    claimed: dict[str, list[int]] = {}
    varying: set[str] = set()
    for parameter in inventory:
        if parameter.index not in selected:
            continue
        row = Row(parameter=parameter, status="observed")
        observations: list[View] = []
        for step, position in enumerate(positions):
            restored = host.restore(baseline_path)
            if not _same_fields(stable, view_of(restored)) or (not stable and restored != baseline):
                row.restoration_failures += 1
            immediate, raw = host.set_control(parameter.index, position)
            row.queued_changes += immediate != raw
            path = out / f"p{parameter.index:04d}_{step:03d}.state"
            path.write_bytes(raw)
            row.snapshot_files.append(path.name)
            row.observed_texts.append(host.plugin.get_parameter_text(parameter.index))
            row.changed_byte_counts.append(
                sum(a != b for a, b in zip(raw, baseline, strict=False)) + abs(len(raw) - len(baseline))
            )
            observations.append(view_of(raw))
        for key, original in stable.items():
            if any(key not in observation.fields for observation in observations):
                row.side_effect_fields.append(key)
                continue
            values = [observation.fields[key] for observation in observations]
            if all(equivalent(values[0], value) for value in values[1:]):
                if not equivalent(original, values[0]):
                    row.side_effect_fields.append(key)
                continue
            varying.add(key)
            numeric = [float(value) if isinstance(value, bool) else number(value) for value in values]
            if any(value is None for value in numeric):
                row.side_effect_fields.append(key)
                continue
            numbers = tuple(value for value in numeric if value is not None)
            row.candidates.append(fit_candidate(key, positions, numbers))
            claimed.setdefault(key, []).append(parameter.index)
        if row.restoration_failures:
            row.status = "baseline_not_restored"
        elif not stable and not all(raw == baseline for raw in repeats):
            row.status = "unstable_opaque_state"
        elif not row.candidates:
            row.status = "no_decoded_change" if view.fields else "opaque_requires_analysis"
        else:
            row.status = "candidate" if len(row.candidates) == 1 else "coupled_candidate"
        rows.append(row)
        print(f"{parameter.index}: {parameter.name}: {row.status}, {len(row.candidates)} fields", flush=True)
    result = Scan(
        plugin=settings.plugin,
        plugin_sha256=hashlib.sha256(Path(settings.plugin).read_bytes()).hexdigest(),
        dawdreamer_version=importlib.metadata.version("dawdreamer"),
        sample_rate=settings.sample_rate,
        block=settings.block,
        layers=list(view.layers),
        inventory=inventory,
        baseline_file=baseline_path.name,
        baseline_sha256=hashlib.sha256(baseline).hexdigest(),
        memory_matches_file=baseline == (out / "baseline_file.state").read_bytes(),
        volatile_fields=sorted(volatile),
        raw_baseline_stable=all(raw == baseline for raw in repeats),
        stable_fields=stable,
        parameters=rows,
        contested_fields={key: owners for key, owners in claimed.items() if len(owners) > 1},
        unclaimed_varying_fields=sorted(varying - claimed.keys()),
        source_baseline_state=settings.baseline_state,
        baseline_import_raw_echo=initial_path.read_bytes() == baseline if settings.baseline_state else None,
        baseline_import_field_mismatches=import_mismatches if settings.baseline_state else [],
        source_baseline_field_count=len(imported.fields) if settings.baseline_state else 0,
        plugin_identity=identity,
        runtime_dependencies=arturia_engine_identity() if settings.experimental_arturia else {},
    )
    (out / "mapping.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return result


def verify(settings: Settings) -> list[dict[str, object]]:
    out = Path(settings.out)
    mapping = Scan.model_validate_json((out / "mapping.json").read_text())
    if hashlib.sha256(Path(settings.plugin).read_bytes()).hexdigest() != mapping.plugin_sha256:
        raise ValueError("Plugin binary changed since the mapping was measured")
    if mapping.plugin_identity and plugin_identity(Path(settings.plugin)) != mapping.plugin_identity:
        raise ValueError("Plugin module, jBridge configuration or original target changed since the scan")
    baseline_path = out / mapping.baseline_file
    baseline = baseline_path.read_bytes()

    def view_of(data: bytes) -> View:
        return decode(data, experimental_arturia=settings.experimental_arturia)

    view = view_of(baseline)
    if mapping.runtime_dependencies:
        current_files = {
            path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in mapping.runtime_dependencies
        }
        if current_files != mapping.runtime_dependencies:
            raise ValueError("Arturia engine files changed since the scan")
    host = Host(settings)
    host.restore(baseline_path)
    if mapping.runtime_dependencies:
        observed = arturia_engine_identity()
        mismatches = [path for path, digest in observed.items() if mapping.runtime_dependencies.get(path) != digest]
        (out / "dependency_check.json").write_text(
            json.dumps(
                {
                    "observed": observed,
                    "mismatches": mismatches,
                    "recorded_files_not_yet_loaded": sorted(mapping.runtime_dependencies.keys() - observed.keys()),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        if mismatches:
            raise ValueError("Loaded Arturia engine modules differ from the scan")
    candidates = [row for row in mapping.parameters if row.status in ("candidate", "coupled_candidate")]
    if settings.verify_limit:
        candidates = candidates[: settings.verify_limit]
    audio_probe: dict[str, object] = {"seconds": settings.seconds, "sample_rate": settings.sample_rate}
    if settings.midi_note is None:
        noise = (
            np.random.default_rng(20261020)
            .uniform(-0.25, 0.25, (2, int(settings.sample_rate * settings.seconds)))
            .astype(np.float32)
        )
        source = host.engine.make_playback_processor("probe_audio", noise)
        host.engine.load_graph([(source, []), (host.plugin, ["probe_audio"])])
        audio_probe.update({"kind": "seeded stereo noise", "seed": 20261020})
    else:
        start, duration = settings.seconds / 12, settings.seconds / 2
        host.plugin.add_midi_note(settings.midi_note, 100, start, duration)
        host.engine.load_graph([(host.plugin, [])])
        audio_probe.update(
            {"kind": "MIDI note", "note": settings.midi_note, "velocity": 100, "start": start, "duration": duration}
        )

    def render(state: Path) -> NDArray[np.float32]:
        host.restore(state)
        host.engine.render(settings.seconds)
        return host.engine.get_audio().copy()

    default_audio = render(baseline_path)
    default_floor = float(np.max(np.abs(default_audio - render(baseline_path))))
    results: list[dict[str, object]] = []
    for row in candidates:
        for position in settings.holdouts:
            if any(position in candidate.positions for candidate in row.candidates):
                raise ValueError("Verification position overlaps sweep observations")
            edits: dict[str, Scalar] = {}
            for candidate in row.candidates:
                prediction = predict(candidate, position)
                original = view.fields[candidate.field]
                if isinstance(original, bool):
                    edits[candidate.field] = prediction >= 0.5
                elif isinstance(original, str):
                    edits[candidate.field] = str(prediction)
                else:
                    edits[candidate.field] = prediction
            patched = out / f"verify_p{row.parameter.index:04d}_{position:.6f}.state"
            patched.write_bytes(view.encode(edits))
            host.restore(baseline_path)
            host.set_control(row.parameter.index, position)
            reference_path = out / f"reference_p{row.parameter.index:04d}_{position:.6f}.state"
            reference_path.write_bytes(host.capture())
            # Reload both states to read actual quantized plugin values rather than host automation inputs.
            reference_audio = render(reference_path)
            reference_parameters = host.parameters()
            reference_view = view_of(host.capture())
            reference_repeat = render(reference_path)
            host.set_control(row.parameter.index, 1.0 if position < 0.5 else 0.0)
            actual_audio = render(patched)
            actual_parameters = host.parameters()
            actual_raw = host.capture()
            readback_path = out / f"readback_p{row.parameter.index:04d}_{position:.6f}.state"
            readback_path.write_bytes(actual_raw)
            actual_view = view_of(actual_raw)
            mismatched_parameters = [
                a.index for a, b in zip(reference_parameters, actual_parameters, strict=True) if a.text != b.text
            ]
            mismatched_fields = [
                key
                for key in mapping.stable_fields
                if key not in actual_view.fields
                or key not in reference_view.fields
                or not equivalent(reference_view.fields[key], actual_view.fields[key])
            ]
            peak = float(np.max(np.abs(reference_audio - actual_audio)))
            repeat_floor = float(np.max(np.abs(reference_audio - reference_repeat)))
            default_delta = float(np.max(np.abs(reference_audio - default_audio)))
            floor = max(default_floor, repeat_floor)
            reference_peak = float(np.max(np.abs(reference_audio)))
            reproducible = floor <= max(1e-6, reference_peak * 0.01)
            mismatch_path = out / f"mismatches_p{row.parameter.index:04d}_{position:.6f}.json"
            mismatch_path.write_text(
                json.dumps(
                    {
                        "parameters": [
                            {
                                "index": a.index,
                                "expected_value": a.value,
                                "actual_value": b.value,
                                "expected_text": a.text,
                                "actual_text": b.text,
                            }
                            for a, b in zip(reference_parameters, actual_parameters, strict=True)
                            if a.index in mismatched_parameters
                        ],
                        "fields": {
                            key: {"expected": reference_view.fields.get(key), "actual": actual_view.fields.get(key)}
                            for key in mismatched_fields
                        },
                        "raw_expected_bytes": len(patched.read_bytes()),
                        "raw_readback_bytes": len(actual_raw),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            result: dict[str, object] = {
                "index": row.parameter.index,
                "name": row.parameter.name,
                "position": position,
                "parameter_mismatches": mismatched_parameters,
                "decoded_field_mismatches": mismatched_fields,
                "parameter_check_passed": not mismatched_parameters,
                "parameter_reading_basis": "native display text; normalized getter is a host automation cache",
                "host_automation_value_mismatches": [
                    a.index
                    for a, b in zip(reference_parameters, actual_parameters, strict=True)
                    if not math_close(a.value, b.value)
                ],
                "field_check_passed": not mismatched_fields,
                "raw_echo": actual_raw == patched.read_bytes(),
                "audio_peak_delta": peak,
                "audio_probe": audio_probe,
                "audio_repeat_floor": floor,
                "audio_reference_peak": reference_peak,
                "audio_reproducible": reproducible,
                "audio_reproducibility_limit": "repeat peak delta <= max(1e-6, 1% of reference peak)",
                "audio_default_delta": default_delta,
                "audio_matches_reference": peak <= floor + 1e-6,
                "audio_changed_from_default": reproducible and default_delta > floor + 1e-6,
                "predicted_state": patched.name,
                "reference_state": reference_path.name,
                "readback_state": readback_path.name,
                "mismatch_details": mismatch_path.name,
            }
            results.append(result)
            (out / "verification.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
            print(
                f"verify {row.parameter.index} @ {position}: parameters={not mismatched_parameters}, "
                f"fields={not mismatched_fields}, audio_delta={peak:.3g}",
                flush=True,
            )
    return results


def math_close(left: float, right: float) -> bool:
    return abs(left - right) <= 1e-5


def summarize(out: Path) -> dict[str, object]:
    mapping = Scan.model_validate_json((out / "mapping.json").read_text())
    checks = json.loads((out / "verification.json").read_text()) if (out / "verification.json").exists() else []
    for row in mapping.parameters:
        trials = [check for check in checks if check["index"] == row.parameter.index]
        if not trials:
            continue
        if not all(check["parameter_check_passed"] and check["field_check_passed"] for check in trials):
            row.status = "verification_failed"
        elif any(check.get("audio_reproducible", True) and not check["audio_matches_reference"] for check in trials):
            row.status = "verified_state_audio_failed"
        elif any(check.get("audio_reproducible", True) and check["audio_changed_from_default"] for check in trials):
            row.status = "verified_state_and_audio"
        else:
            row.status = "verified_state_only"
    (out / "mapping.json").write_text(mapping.model_dump_json(indent=2), encoding="utf-8")
    if mapping.source_baseline_state is None:
        recovery_status = "source_not_requested"
    elif mapping.baseline_import_field_mismatches:
        recovery_status = "source_import_field_mismatch"
    elif mapping.baseline_import_raw_echo:
        recovery_status = "source_state_read_back_exactly"
    elif mapping.source_baseline_field_count:
        recovery_status = "source_decoded_fields_agree_raw_changed"
    else:
        recovery_status = "source_import_opaque_unverified"
    summary: dict[str, object] = {
        "recovery_status": recovery_status,
        "source_import": {
            "path": mapping.source_baseline_state,
            "raw_echo": mapping.baseline_import_raw_echo,
            "decoded_field_mismatches": mapping.baseline_import_field_mismatches,
            "decoded_field_count": mapping.source_baseline_field_count,
        },
        "mapping_scope": (
            "Verification describes predicted edits to the state the plugin returned, "
            "independently of source import acceptance."
        ),
        "statuses": dict(Counter(row.status for row in mapping.parameters)),
        "verification_trials": len(checks),
        "trials_with_parameter_and_field_agreement": sum(
            bool(c["parameter_check_passed"] and c["field_check_passed"]) for c in checks
        ),
        "contested_fields": mapping.contested_fields,
        "limits": [
            "Evidence covers the recorded baseline, binary, sample rate and sampled/held-out positions.",
            "Audio evidence covers the recorded probe signal or MIDI note; other internal modes remain untested.",
            "State-only verification does not establish audible equivalence.",
        ],
        "out": str(out),
    }
    if any("experimental Arturia" in layer for layer in mapping.layers):
        limits = summary["limits"]
        if isinstance(limits, list):
            limits.append(
                "Experimental Arturia decoding covers counted numeric maps; other archive contents remain opaque."
            )
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def _run_worker(settings: Settings, python: str, timeout: int) -> None:
    out = Path(settings.out)
    job = out / f"{settings.mode}.job.json"
    job.write_text(settings.model_dump_json(indent=2), encoding="utf-8")
    run_worker([python, str(Path(__file__).resolve()), "--worker", str(job)], out, settings.mode, timeout)


def main(arguments: list[str] | None = None) -> int:
    argv = sys.argv[1:] if arguments is None else arguments
    if argv and argv[0] == "--worker":
        settings = Settings.model_validate_json(Path(argv[1]).read_text())
        try:
            attach_worker()
            if sys.platform == "win32":
                user = ctypes.windll.user32
                user.CreateDesktopW.restype = ctypes.c_void_p
                user.CreateDesktopW.argtypes = [
                    ctypes.c_wchar_p,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                    ctypes.c_uint,
                    ctypes.c_uint,
                    ctypes.c_void_p,
                ]
                user.SetThreadDesktop.argtypes = [ctypes.c_void_p]
                desktop = user.CreateDesktopW(f"AbletoolzMapper{os.getpid()}", None, None, 0, 0x01FF, None)
                if not desktop or not user.SetThreadDesktop(desktop):
                    raise OSError("Cannot isolate plugin UI on a hidden desktop")
                ctypes.windll.kernel32.SetErrorMode(0x8003)
            if settings.mode == "scan":
                scan(settings)
            else:
                verify(settings)
        except (ValueError, RuntimeError, OSError, AttributeError, ImportError, TypeError, KeyError):
            traceback.print_exc()
            sys.stderr.flush()
            os._exit(1)
        sys.stdout.flush()
        os._exit(0)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--parameter", action="append", default=[])
    parser.add_argument("--index", type=int, action="append", default=[])
    parser.add_argument("--points", type=int, default=65)
    parser.add_argument("--sample-rate", type=int, default=44100)
    parser.add_argument("--seconds", type=float, default=0.25, help="Audio probe duration, up to 2 seconds")
    parser.add_argument("--midi-note", type=int, help="Probe an instrument with this MIDI note at velocity 100")
    parser.add_argument(
        "--experimental-arturia",
        action="store_true",
        help="Explicitly enable the partial counted-float-map decoder for an established Arturia Boost archive",
    )
    parser.add_argument(
        "--baseline-state", type=Path, help="Explicit host state from a corpus adapter or prior capture"
    )
    parser.add_argument("--settle-ms", type=int, default=0, help="Pump Windows messages for asynchronous state updates")
    parser.add_argument("--verify-limit", type=int, default=0, help="0 verifies all candidate controls")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--python", default=os.environ.get("DERIVE_PYTHON", sys.executable))
    args = parser.parse_args(argv)
    settings = Settings(
        plugin=str(args.plugin.resolve()),
        out=str(args.out.resolve()),
        names=args.parameter,
        indices=args.index,
        points=args.points,
        sample_rate=args.sample_rate,
        verify_limit=args.verify_limit,
        settle_ms=args.settle_ms,
        baseline_state=str(args.baseline_state.resolve()) if args.baseline_state else None,
        experimental_arturia=args.experimental_arturia,
        seconds=args.seconds,
        midi_note=args.midi_note,
    )
    if not Path(settings.plugin).is_file():
        parser.error("--plugin must identify the actual native DLL/module file")
    Path(settings.out).mkdir(parents=True, exist_ok=False)
    _run_worker(settings, args.python, args.timeout)
    _run_worker(settings.model_copy(update={"mode": "verify"}), args.python, args.timeout)
    print(json.dumps(summarize(Path(settings.out)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
