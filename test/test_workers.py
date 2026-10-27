"""CPU availability, worker overrides and CLI auto selection."""

from __future__ import annotations

import pathlib

import pytest

from abletoolz import cli, workers


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ((3, 16, 32), 3),  # the process's own limit first (Python 3.13+)
        ((None, 2, 32), 2),  # then its affinity, where the OS has one
        ((None, None, 8), 8),  # then the system's
        ((None, 0, None), 1),  # never fewer than one
        ((0, None, 0), 1),
    ],
)
def test_the_most_specific_known_cpu_count_wins(counts: tuple[int | None, ...], expected: int) -> None:
    assert workers.first_known_count(*counts) == expected


def test_this_computer_has_at_least_one_cpu() -> None:
    assert workers.available_cpu_count() >= 1


def test_auto_is_bounded_by_tasks_and_executor_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workers, "available_cpu_count", lambda: 128)
    assert workers.worker_count(None) == 128
    assert workers.worker_count(None, tasks=2) == 2
    assert workers.worker_count(None, tasks=0) == 1
    assert workers.worker_count(None, limit=61) == 61
    assert workers.worker_count(16, tasks=40) == 16
    assert workers.worker_count(128, tasks=2, limit=61) == 2
    with pytest.raises(ValueError, match="at most 61"):
        workers.worker_count(128, tasks=128, limit=61)


@pytest.mark.parametrize("requested", [0, -1])
def test_nonpositive_overrides_are_rejected(requested: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        workers.worker_count(requested)


@pytest.mark.parametrize(("option", "value"), [("-j", "16"), ("--jobs", "16"), ("--jobs", "auto")])
def test_cli_job_options(tmp_path: pathlib.Path, option: str, value: str) -> None:
    args = cli.parse_arguments([option, value, str(tmp_path)])
    assert args.jobs == (None if value == "auto" else 16)
    assert args.db_workers is None


def test_cli_automatic_default_and_db_override(tmp_path: pathlib.Path) -> None:
    args = cli.parse_arguments(["--db-workers", "4", str(tmp_path)])
    assert args.jobs is None and args.db_workers == 4


def test_cli_compact_job_override(tmp_path: pathlib.Path) -> None:
    assert cli.parse_arguments(["-j16", str(tmp_path)]).jobs == 16


@pytest.mark.parametrize("option", ["-j", "--db-workers"])
@pytest.mark.parametrize("value", ["0", "-2", "invalid"])
def test_invalid_cli_worker_values_are_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, option: str, value: str
) -> None:
    with pytest.raises(SystemExit) as error:
        cli.parse_arguments([option, value, str(tmp_path)])
    assert error.value.code == 2
