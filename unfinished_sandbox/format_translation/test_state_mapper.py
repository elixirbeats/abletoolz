from __future__ import annotations

import json
from pathlib import Path

from state_mapper import Parameter, Row, Scan, fit_candidate, plugin_identity, predict, summarize


def test_measured_internal_quantization_predicts_unsampled_values() -> None:
    positions = tuple(i / 16 for i in range(17))
    # Values captured from CHOWTapeModel's Drive save field, including rounded .125 and .375.
    values = (0.0, 0.06, 0.13, 0.19, 0.25, 0.31, 0.38, 0.44, 0.5, 0.56, 0.63, 0.69, 0.75, 0.81, 0.88, 0.94, 1.0)
    candidate = fit_candidate("drive", positions, values)
    assert candidate.model["kind"] == "quantized-linear"
    assert predict(candidate, 0.173) == 0.17
    assert predict(candidate, 0.371) == 0.37
    assert predict(candidate, 0.683) == 0.68


def test_an_unexplained_curve_stays_a_measured_table() -> None:
    candidate = fit_candidate("curve", (0.0, 0.25, 0.5, 0.75, 1.0), (0.0, 0.1, 0.4, 0.85, 1.0))
    assert candidate.model["kind"] == "table"
    assert candidate.residual == 0.0
    assert predict(candidate, 0.5) == 0.4


def test_inverting_numeric_curve_keeps_its_observed_endpoints() -> None:
    candidate = fit_candidate("inverted", (0.0, 0.25, 0.5, 0.75, 1.0), (12.0, 6.0, 0.0, -6.0, -12.0))
    assert candidate.model["kind"] == "linear"
    assert predict(candidate, 0.0) == 12.0
    assert predict(candidate, 1.0) == -12.0


def test_mapping_success_does_not_claim_an_ignored_source_was_recovered(tmp_path: Path) -> None:
    mapping = Scan(
        plugin="plugin.dll",
        plugin_sha256="example",
        dawdreamer_version="test",
        sample_rate=44100,
        block=512,
        layers=["XML"],
        inventory=[],
        baseline_file="baseline.state",
        baseline_sha256="example",
        memory_matches_file=True,
        volatile_fields=[],
        raw_baseline_stable=True,
        stable_fields={"gain": 0.0},
        contested_fields={},
        unclaimed_varying_fields=[],
        source_baseline_state="old.state",
        baseline_import_raw_echo=False,
        baseline_import_field_mismatches=["gain"],
        parameters=[Row(parameter=Parameter(index=0, name="Gain", value=0.0, text="0"), status="candidate")],
    )
    (tmp_path / "mapping.json").write_text(mapping.model_dump_json())
    (tmp_path / "verification.json").write_text(
        json.dumps(
            [
                {
                    "index": 0,
                    "parameter_check_passed": True,
                    "field_check_passed": True,
                    "audio_matches_reference": True,
                    "audio_changed_from_default": True,
                }
            ]
        )
    )
    summary = summarize(tmp_path)
    assert summary["recovery_status"] == "source_import_field_mismatch"
    assert summary["statuses"] == {"verified_state_and_audio": 1}


def test_informative_audio_failure_has_its_own_status(tmp_path: Path) -> None:
    test_mapping_success_does_not_claim_an_ignored_source_was_recovered(tmp_path)
    (tmp_path / "verification.json").write_text(
        json.dumps(
            [
                {
                    "index": 0,
                    "parameter_check_passed": True,
                    "field_check_passed": True,
                    "audio_matches_reference": False,
                    "audio_changed_from_default": True,
                }
            ]
        )
    )
    assert summarize(tmp_path)["statuses"] == {"verified_state_audio_failed": 1}


def test_poor_render_repeatability_cannot_establish_audio_agreement(tmp_path: Path) -> None:
    test_mapping_success_does_not_claim_an_ignored_source_was_recovered(tmp_path)
    checks = [
        {
            "index": 0,
            "parameter_check_passed": True,
            "field_check_passed": True,
            "audio_matches_reference": True,
            "audio_changed_from_default": True,
            "audio_reproducible": False,
        }
    ]
    (tmp_path / "verification.json").write_text(json.dumps(checks))
    assert summarize(tmp_path)["statuses"] == {"verified_state_only": 1}


def test_identical_bridge_stubs_do_not_hide_a_changed_original_dll(tmp_path: Path) -> None:
    stub = tmp_path / "Effect.64.dll"
    original = tmp_path / "Effect.dll"
    stub.write_bytes(b"identical bridge code")
    original.write_bytes(b"original version one")
    stub.with_suffix(".txt").write_text(f'"{original}" 1234 flags', encoding="utf-8")
    before = plugin_identity(stub)
    original.write_bytes(b"original version two")
    after = plugin_identity(stub)
    assert before["module_sha256"] == after["module_sha256"]
    assert before["jbridge_target_sha256"] != after["jbridge_target_sha256"]
