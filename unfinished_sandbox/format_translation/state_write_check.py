"""Test two measured float mappings at a position absent from their sweeps.

Run in the state-lab environment with --out pointing at a new directory.
The two probe result directories must be under --observations. A fresh plugin
instance reads each edited state; a separate instance supplies knob-set audio
and parameter readings. The parent limits the native worker to 60 seconds.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import struct
import subprocess
import sys
from pathlib import Path
from typing import cast

import numpy as np
from fabf_state import parse_fabf
from juce_container import PluginState, unwrap, wrap
from state_probe import BLOCK, SAMPLE_RATE, EngineFactory, capture, difference, fields, parts

from abletoolz.plugins.state.fabfilter import EditorState, FfbsState


def measure(observations: Path, out: Path) -> None:
    daw = importlib.import_module("dawdreamer")
    # DawDreamer's compiled constructor has no Python type stubs.
    factory = cast(EngineFactory, daw.RenderEngine)
    heldout = 0.37
    results: list[dict[str, object]] = []
    for folder in ("proc2_vst3_primed", "proq3_decoded"):
        source = observations / folder
        report = json.loads((source / "result.json").read_text())
        row = report["parameters"][0]
        baseline = (source / "baseline.state").read_bytes()
        split = parts(baseline)
        key = "processor" if "processor" in split else "chunk"
        varying = set().union(*(set(s["parts"][key]["decoded_changes"]) for s in row["samples"]))
        if len(varying) != 1:
            raise ValueError(f"Expected one measured float for {folder}: {varying}")
        field = next(iter(varying))
        slot = int(field.removeprefix("float[").removesuffix("]"))
        first = row["samples"][0]["parts"][key]["decoded_changes"][field][1]
        last = row["samples"][-1]["parts"][key]["decoded_changes"][field][1]
        predicted = float(first) + (float(last) - float(first)) * heldout
        if key == "processor":
            state = unwrap(baseline)
            component = bytearray(state.component)
            offset = 20 + len(parse_fabf(state.component).name.encode("latin-1")) + slot * 4
            struct.pack_into("<f", component, offset, predicted)
            patched = wrap(PluginState(bytes(component), state.controller))
        else:
            patched_bank = bytearray(baseline)
            struct.pack_into("<f", patched_bank, 160 + 12 + slot * 4, predicted)
            patched = bytes(patched_bank)
        path = out / f"{folder}_patched.state"
        path.write_bytes(patched)

        reference_engine = factory(SAMPLE_RATE, BLOCK)
        reference = reference_engine.make_plugin_processor("reference", report["plugin"])
        reference_engine.load_graph([(reference, [])])
        reference.load_state(str(source / "baseline.state"))
        reference.set_parameter(row["index"], heldout)
        reference_engine.render(BLOCK / SAMPLE_RATE)
        reference_state = capture(reference)

        target_engine = factory(SAMPLE_RATE, BLOCK)
        target = target_engine.make_plugin_processor("target", report["plugin"])
        target_engine.load_graph([(target, [])])
        target.load_state(str(path))
        target_engine.render(BLOCK / SAMPLE_RATE)
        actual_state = capture(target)
        (out / f"{folder}_reference.state").write_bytes(reference_state)
        (out / f"{folder}_loaded.state").write_bytes(actual_state)
        ref_parts, actual_parts = parts(reference_state), parts(actual_state)
        ref_fields, actual_fields = fields(ref_parts[key]), fields(actual_parts[key])
        result: dict[str, object] = {
            "case": folder,
            "position": heldout,
            "slot": slot,
            "predicted_stored_float": predicted,
            "reference_stored_float": ref_fields[field],
            "loaded_stored_float": actual_fields[field],
            "all_decoded_floats_match": ref_fields == actual_fields,
            "readback": target.get_parameter(row["index"]),
            "reference_text": reference.get_parameter_text(row["index"]),
            "loaded_text": target.get_parameter_text(row["index"]),
            "loaded_payload_echoes_patch": actual_parts[key] == parts(patched)[key],
            "patch_readback_difference": difference(parts(patched)[key], actual_parts[key]),
            "knob_vs_loaded_difference": difference(ref_parts[key], actual_parts[key]),
        }
        # A seeded signal supplies an observable beyond the state's own readback.
        noise = np.random.default_rng(20261020).uniform(-0.5, 0.5, (2, SAMPLE_RATE)).astype(np.float32)
        if key == "processor":
            pedalboard = importlib.import_module("pedalboard")
            ref_audio = pedalboard.load_plugin(report["plugin"])
            mapped_audio = pedalboard.load_plugin(report["plugin"])
            default_audio = pedalboard.load_plugin(report["plugin"])
            ref_audio.raw_state = reference_state
            mapped_audio.raw_state = actual_state
            default_audio.raw_state = baseline
            expected = ref_audio(noise, SAMPLE_RATE)
            audio_difference = expected - mapped_audio(noise, SAMPLE_RATE)
            result["audio_peak_delta"] = float(np.max(np.abs(audio_difference)))
            result["audio_reference_repeat_delta"] = float(np.max(np.abs(expected - ref_audio(noise, SAMPLE_RATE))))
            result["audio_default_peak_delta"] = float(np.max(np.abs(expected - default_audio(noise, SAMPLE_RATE))))
        else:
            result["audio_peak_delta"] = None
            result["audio_limit"] = "VST2 state needs reframing before loading into the VST3 audio host."
            editor_metadata = {}
            for label, blob in (
                ("patched", parts(patched)[key]),
                ("knob_set", ref_parts[key]),
                ("loaded", actual_parts[key]),
            ):
                editor = EditorState.parse(FfbsState.parse(blob).tail)
                editor_metadata[label] = {
                    "label": editor.label,
                    "carried_words": editor.carried,
                    "instance_index": editor.instance_index,
                }
            result["editor_metadata"] = editor_metadata
            result["editor_limit"] = (
                "Labels differ across simultaneous instances; the changed carried word is unresolved."
            )
        result["parameter_check_passed"] = (
            ref_fields == actual_fields
            and target.get_parameter_text(row["index"]) == reference.get_parameter_text(row["index"])
            and abs(target.get_parameter(row["index"]) - heldout) < 1e-6
        )
        results.append(result)
    (out / "result.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2), flush=True)
    if not all(row["parameter_check_passed"] for row in results):
        raise RuntimeError("A held-out parameter check failed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.child:
        args.out.mkdir(parents=True, exist_ok=False)
        try:
            completed = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:], "--child"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except subprocess.TimeoutExpired:
            print("State write check timed out", file=sys.stderr)
            return 2
        (args.out / "child_stderr.log").write_text(completed.stderr, encoding="utf-8")
        print(completed.stdout)
        if completed.returncode:
            print(completed.stderr, file=sys.stderr)
        return completed.returncode
    measure(args.observations.resolve(), args.out.resolve())
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
