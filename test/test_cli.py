"""CLI argument validation and exit codes.

Drives ``cli.main()`` through monkeypatched ``sys.argv`` — the same path the
console script takes — and asserts on the ``SystemExit`` code.
"""

from __future__ import annotations

import pathlib
import shutil

import pytest

from abletoolz import cli

SKELETONS = pathlib.Path(__file__).parent / "version_fixtures" / "skeletons"


def run_cli(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr("sys.argv", ["abletoolz", *argv])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    code = excinfo.value.code
    assert isinstance(code, int)
    return code


def test_failed_set_exits_nonzero(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    corrupt = tmp_path / "corrupt.als"
    corrupt.write_bytes(b"not a gzip file")
    assert run_cli(monkeypatch, str(corrupt)) == 1


def test_good_set_exits_zero(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    shutil.copy(SKELETONS / "11.3.42.als", tmp_path)
    assert run_cli(monkeypatch, str(tmp_path)) == 0


def test_failure_infects_batch_exit_code(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    shutil.copy(SKELETONS / "11.3.42.als", tmp_path)
    (tmp_path / "corrupt.als").write_bytes(b"not a gzip file")
    assert run_cli(monkeypatch, str(tmp_path)) == 1


def test_ctrl_c_cancels_batch(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """Ctrl+C during a batch must stop the run with 130, not drain the whole queue.

    Regression: all sets were submitted to the pool up front, and the executor's
    context-manager shutdown waited for every queued future after the interrupt.
    """
    shutil.copy(SKELETONS / "11.3.42.als", tmp_path)
    shutil.copy(SKELETONS / "12.2.6.als", tmp_path)

    def boom(_futures: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "as_completed", boom)
    assert run_cli(monkeypatch, str(tmp_path)) == 130


def test_no_srcs_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    assert run_cli(monkeypatch) == 2


def test_conflicting_flags_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    assert run_cli(monkeypatch, "--fold", "--unfold", str(tmp_path)) == 2
    assert run_cli(monkeypatch, "--fix-samples-absolute", "--fix-samples-collect", str(tmp_path)) == 2


def test_db_excludes_edit_flags(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    assert run_cli(monkeypatch, "--db", "--fix-plugins", str(tmp_path)) == 2
    assert run_cli(monkeypatch, "--db", "--analyze-plugins", str(tmp_path)) == 2
    assert run_cli(monkeypatch, "--db", "--list-parsers") == 2


def test_list_parsers_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    assert run_cli(monkeypatch, "--list-parsers") == 0
