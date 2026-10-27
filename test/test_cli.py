"""CLI argument validation and exit codes.

Calls ``cli.main()`` with an argument list, the way the console script does,
and asserts on the exit code it answers.
"""

from __future__ import annotations

import json
import pathlib
import pickle
import shutil
import subprocess
import sys

import pytest
from conftest import SKELETONS, run_cli

from abletoolz import cli
from abletoolz.plugins.config import AbletoolzConfig
from abletoolz.run import batch
from abletoolz.run.job import COMMAND_FLAGS, SetJob
from abletoolz.samples import database


def test_ctrl_c_cancels_batch(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """Ctrl+C during a batch must stop the run with 130, not drain the whole queue.

    Regression: all sets were submitted to the pool up front, and the executor's
    context-manager shutdown waited for every queued future after the interrupt.
    """
    shutil.copy(SKELETONS / "11.3.42.als", tmp_path)
    shutil.copy(SKELETONS / "12.2.6.als", tmp_path)

    def boom(_futures: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(batch, "as_completed", boom)
    assert run_cli(str(tmp_path)) == 130


def test_no_srcs_rejected() -> None:
    assert run_cli() == 2


def test_the_plugins_command_answers_its_exit_code_like_the_rest() -> None:
    assert run_cli("plugins", "bogus") == 2
    assert run_cli("plugins", "--help") == 0


def test_conflicting_flags_rejected(tmp_path: pathlib.Path) -> None:
    assert run_cli("--fold", "--unfold", str(tmp_path)) == 2
    assert run_cli("--fix-samples-absolute", "--fix-samples-collect", str(tmp_path)) == 2


def test_db_excludes_the_other_machine_commands() -> None:
    """The set commands are refused by the parametrized test below; --list-plugins is not one of them."""
    assert run_cli("--db", "--list-plugins") == 2


def test_sample_catalog_options_are_validated(tmp_path: pathlib.Path) -> None:
    assert run_cli("--search-samples", "kick", str(tmp_path)) == 2
    assert run_cli("--db", "--db-workers", "0", str(tmp_path)) == 2


def test_cli_builds_and_searches_sample_catalog(tmp_path: pathlib.Path, child_env: dict[str, str]) -> None:
    root = tmp_path / "samples"
    root.mkdir()
    sample = root / "kick_174.wav"
    sample.write_bytes(b"sample")
    catalog_path = tmp_path / "samples.sqlite3"
    subprocess.run(
        [sys.executable, "-m", "abletoolz", "--db", "--db-path", str(catalog_path), str(root)],
        capture_output=True,
        text=True,
        check=True,
        cwd=tmp_path,
        env=child_env,
    )
    result = subprocess.run(
        [sys.executable, "-m", "abletoolz", "--search-samples", "kick 17", "--db-path", str(catalog_path)],
        capture_output=True,
        text=True,
        check=True,
        cwd=tmp_path,
        env=child_env,
    )
    assert json.loads(result.stdout) == [str(sample)]


def test_list_plugins_exits_zero() -> None:
    assert run_cli("--list-plugins") == 0


def test_output_requires_save_or_apply_ops(tmp_path: pathlib.Path) -> None:
    """--output redirects writes, so a run that writes nothing rejects it."""
    assert run_cli("--check-plugins", "--output", str(tmp_path / "out"), str(tmp_path)) == 2


# --- what each set is asked ---------------------------------------------------

# The flags that take a value, and one they accept.
COMMAND_VALUES = {"describe": "structure", "master_out": "1", "cue_out": "1"}


@pytest.mark.parametrize("name", COMMAND_FLAGS)
def test_db_excludes_every_set_command(tmp_path: pathlib.Path, name: str) -> None:
    """The exclusivity check reads the set commands off SetJob, so a new one is covered by being added there."""
    value = COMMAND_VALUES.get(name, "1" if name.startswith("set_track_") else None)
    flag = ["--" + name.replace("_", "-"), *([] if value is None else [value])]
    # The flag itself parses, so the refusal below is the exclusivity check's.
    assert getattr(cli.parse_arguments([*flag, str(tmp_path)]), name)
    assert run_cli("--db", *flag, str(tmp_path)) == 2


def test_a_job_is_read_off_the_command_line_once_and_travels_to_workers(tmp_path: pathlib.Path) -> None:
    out = tmp_path / "out"
    argv = ["--fold", "--check-samples", "-s", "--output", str(out), "--no-meta", str(tmp_path)]
    job = SetJob.from_args(cli.parse_arguments(argv))
    assert job == SetJob(fold=True, check_samples=True, save=True, output=out, meta=False)
    assert job.editing and job.scans
    # Spawned workers get it pickled.
    assert pickle.loads(pickle.dumps(job)) == job


def test_a_job_refuses_what_the_command_line_refuses() -> None:
    with pytest.raises(ValueError, match="fold"):
        SetJob(fold=True, unfold=True)
    with pytest.raises(ValueError, match="relink"):
        SetJob(fix_samples_absolute=True, fix_samples_collect=True)


# --- machine-readable stdout ------------------------------------------------
#
# Run out of process on purpose: ``logging.basicConfig`` configures the root
# logger once per interpreter, so an in-process second call would keep the
# first call's stream and prove nothing.


def run_module(tmp_path: pathlib.Path, env: dict[str, str], *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "abletoolz", *argv],
        capture_output=True,
        text=True,
        check=True,
        cwd=tmp_path,
        env=env,
    )


def test_describe_stdout_is_json_and_nothing_else(tmp_path: pathlib.Path, child_env: dict[str, str]) -> None:
    """A consumer pipes stdout into a JSON parser; the banners go to stderr."""
    set_path = tmp_path / "described.als"
    shutil.copy(SKELETONS / "12.4.5b.als", set_path)
    result = run_module(tmp_path, child_env, "--describe", "patterns", str(set_path))

    document = json.loads(result.stdout)
    assert set(document) == {"set", "tracks", "patterns", "session", "arrangement"}
    assert "Parsing:" in result.stderr


def test_apply_ops_stdout_is_a_json_report(tmp_path: pathlib.Path, child_env: dict[str, str]) -> None:
    set_path = tmp_path / "input.als"
    shutil.copy(SKELETONS / "12.4.5b.als", set_path)
    ops_path = tmp_path / "ops.json"
    ops_path.write_text(
        json.dumps({"ops": [{"op": "set_notes", "track": {"name": "1-LOW"}, "clip": {"slot": 1}, "notes": []}]}),
        encoding="utf-8",
    )
    output = tmp_path / "output.als"
    result = run_module(tmp_path, child_env, "--apply-ops", str(ops_path), "--output", str(output), str(set_path))

    report = json.loads(result.stdout)
    assert report["applied"] == 1
    assert report["output"] == str(output)
    assert output.exists()


def test_db_writes_the_plugin_files_database_beside_the_sample_database(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    (root / "kick.wav").write_bytes(b"RIFF")
    (root / "Piano.nki").write_bytes(b"instrument")
    db_path = tmp_path / "dbs" / "samples.sqlite3"

    assert run_cli("--db", "--db-path", str(db_path), str(root)) == 0

    assert sorted(path.name for path in db_path.parent.glob("*.sqlite3")) == ["plugin_files.sqlite3", "samples.sqlite3"]


def test_db_without_folders_scans_the_ones_in_config(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    (root / "kick.wav").write_bytes(b"RIFF")
    db_path = tmp_path / "samples.sqlite3"

    assert run_cli("--db", "--db-path", str(db_path), config=AbletoolzConfig(sample_paths=[root])) == 0

    with database.load_db(db_path) as samples:
        assert list(samples.candidates("kick.wav")) == [str(root / "kick.wav")]


def test_db_prune_removes_files_that_no_longer_exist(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    (root / "kick.wav").write_bytes(b"RIFF")
    (root / "snare.wav").write_bytes(b"RIFF")
    (root / "Piano.nki").write_bytes(b"instrument")
    db_path = tmp_path / "samples.sqlite3"
    assert run_cli("--db", "--db-path", str(db_path), str(root)) == 0
    (root / "snare.wav").unlink()
    (root / "Piano.nki").unlink()

    assert run_cli("--db-prune", "--db-path", str(db_path)) == 0

    with database.load_db(db_path) as samples:
        assert list(samples.candidates("kick.wav")) == [str(root / "kick.wav")]
        assert not samples.candidates("snare.wav")
        assert samples.search("snare") == ()
    with database.load_plugin_files_db(database.plugin_files_db_path(db_path)) as plugin_files:
        assert not plugin_files.candidates("Piano.nki")


def test_collect_refuses_output_because_it_copies_into_the_original_project(tmp_path: pathlib.Path) -> None:
    assert run_cli("--fix-samples-collect", "-s", "--output", str(tmp_path / "out"), str(tmp_path)) == 2


def test_db_prune_takes_no_folders(tmp_path: pathlib.Path) -> None:
    assert run_cli("--db-prune", str(tmp_path)) == 2
    assert run_cli("--db-prune", "--db") == 2


def test_db_without_folders_or_config_paths_fails(tmp_path: pathlib.Path) -> None:
    assert run_cli("--db", "--db-path", str(tmp_path / "samples.sqlite3")) == 1


def apply_ops_files(tmp_path: pathlib.Path, *ops: dict[str, object]) -> tuple[pathlib.Path, pathlib.Path]:
    """An input set and an ops document in ``tmp_path``, for an --apply-ops run."""
    set_path = tmp_path / "input.als"
    shutil.copy(SKELETONS / "12.4.5b.als", set_path)
    ops_path = tmp_path / "ops.json"
    ops_path.write_text(json.dumps({"ops": list(ops)}), encoding="utf-8")
    return set_path, ops_path


def test_apply_ops_writes_the_output_and_nothing_else(tmp_path: pathlib.Path) -> None:
    set_path, ops_path = apply_ops_files(
        tmp_path, {"op": "set_notes", "track": {"name": "1-LOW"}, "clip": {"slot": 1}, "notes": []}
    )
    original = set_path.read_bytes()
    output = tmp_path / "output.als"
    assert run_cli("--apply-ops", str(ops_path), "--output", str(output), str(set_path)) == 0
    assert sorted(path.name for path in tmp_path.iterdir()) == ["input.als", "ops.json", "output.als"]
    assert set_path.read_bytes() == original


@pytest.mark.parametrize(
    "op",
    [
        {"op": "set_notes", "track": {"name": "nope"}, "clip": {"slot": 1}, "notes": []},
        {"op": "set_transport_setting", "setting": "LOOP_ON", "value": 1},
    ],
    ids=["an op the set refuses", "a value of the wrong kind"],
)
def test_a_failed_apply_ops_writes_nothing_so_it_can_be_retried(tmp_path: pathlib.Path, op: dict[str, object]) -> None:
    set_path, ops_path = apply_ops_files(tmp_path, op)
    output = tmp_path / "output.als"
    assert run_cli("--apply-ops", str(ops_path), "--output", str(output), str(set_path)) == 1
    assert sorted(path.name for path in tmp_path.iterdir()) == ["input.als", "ops.json"]


def test_apply_ops_on_a_set_that_cannot_be_read_exits_1(tmp_path: pathlib.Path) -> None:
    set_path, ops_path = apply_ops_files(tmp_path)
    set_path.write_bytes(b"not a set")
    output = tmp_path / "output.als"
    assert run_cli("--apply-ops", str(ops_path), "--output", str(output), str(set_path)) == 1
    assert not output.exists()
