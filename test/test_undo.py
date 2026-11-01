"""``abletoolz undo``: the last command's saves come back from their backups, after asking."""

from __future__ import annotations

import datetime
import os
import pathlib
import shutil

import pytest
from conftest import copy_set

from abletoolz import cli
from abletoolz.formats import live_document
from abletoolz.live_set.persistence import COMMAND_ENV, RUN_ENV, save_in_place, undo_journal
from abletoolz.live_set.undo import history, run_undo


def save_edited(path: pathlib.Path, run: str, monkeypatch: pytest.MonkeyPatch) -> bytes:
    """Save ``path`` in place as part of command ``run``, the way any abletoolz edit does; answers what was written."""
    monkeypatch.setenv(RUN_ENV, run)
    monkeypatch.setenv(COMMAND_ENV, f"abletoolz {run}")
    root = live_document.read(path)
    root.set("Creator", run)
    save_in_place(path, root)
    return path.read_bytes()


def yes(_: str) -> str:
    return "y"


def test_undo_puts_back_the_last_command_then_the_one_before(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_set, second_set = copy_set(tmp_path, name="a.als"), copy_set(tmp_path, name="b.als")
    original = first_set.read_bytes()
    after_first = save_edited(first_set, "1-first", monkeypatch)
    save_edited(first_set, "2-second", monkeypatch)
    save_edited(second_set, "2-second", monkeypatch)
    assert [run.command for run in history()] == ["abletoolz 2-second", "abletoolz 1-first"]

    assert run_undo([], ask=yes) == 0
    assert first_set.read_bytes() == after_first and second_set.read_bytes() == original
    assert run_undo([], ask=yes) == 0
    assert first_set.read_bytes() == original
    assert history() == []


def test_answering_no_restores_nothing(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = copy_set(tmp_path)
    written = save_edited(path, "1-run", monkeypatch)
    assert run_undo([], ask=lambda _: "") == 0
    assert path.read_bytes() == written and len(history()) == 1


def test_a_set_changed_since_is_pointed_out_and_its_version_kept(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = copy_set(tmp_path)
    original = path.read_bytes()
    save_edited(path, "1-run", monkeypatch)
    edited = path.read_bytes() + b"saved again in Live"
    path.write_bytes(edited)
    assert run_undo([], ask=yes) == 0
    assert "changed since abletoolz saved it" in capsys.readouterr().out
    assert path.read_bytes() == original
    assert edited in [backup.read_bytes() for backup in (tmp_path / "abletoolz_backup").iterdir()]


def test_list_shows_every_command_newest_first(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = copy_set(tmp_path)
    save_edited(path, "1-first", monkeypatch)
    save_edited(path, "2-second", monkeypatch)
    assert cli.main(["undo", "--list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "abletoolz 2-second" in lines[0] and "abletoolz 1-first" in lines[1]


def test_a_count_undoes_that_many_commands_with_one_question(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = copy_set(tmp_path)
    original = path.read_bytes()
    save_edited(path, "1-first", monkeypatch)
    save_edited(path, "2-second", monkeypatch)
    asked: list[str] = []
    assert run_undo(["2"], ask=lambda question: asked.append(question) or "y") == 0
    assert path.read_bytes() == original and history() == []
    assert asked == ["Restore 2 set(s) from 2 command(s)? [y/N] "]


def test_a_count_does_not_call_a_set_changed_that_a_newer_undo_puts_back_first(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = copy_set(tmp_path)
    save_edited(path, "1-first", monkeypatch)
    save_edited(path, "2-second", monkeypatch)
    assert run_undo(["2"], ask=lambda _: "") == 0
    assert "changed since" not in capsys.readouterr().out


def test_a_damaged_record_is_skipped(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = copy_set(tmp_path)
    original = path.read_bytes()
    save_edited(path, "1-run", monkeypatch)
    (undo_journal() / "0-garbled.json").write_text("{not json", encoding="utf-8")
    assert run_undo([], ask=yes) == 0
    assert path.read_bytes() == original
    assert "Skipped a damaged undo record" in capsys.readouterr().out


def test_a_cleared_backup_folder_leaves_those_sets_and_restores_the_rest(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kept, lost = tmp_path / "kept", tmp_path / "lost"
    kept.mkdir()
    lost.mkdir()
    kept_set, lost_set = copy_set(kept), copy_set(lost)
    original = kept_set.read_bytes()
    save_edited(kept_set, "1-run", monkeypatch)
    lost_written = save_edited(lost_set, "1-run", monkeypatch)
    shutil.rmtree(lost / "abletoolz_backup")
    assert run_undo([], ask=yes) == 0
    assert kept_set.read_bytes() == original and lost_set.read_bytes() == lost_written
    assert history() == []  # the record that could never come back is gone too


def test_a_set_deleted_since_is_put_back_when_asked(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = copy_set(tmp_path)
    original = path.read_bytes()
    save_edited(path, "1-run", monkeypatch)
    path.unlink()
    assert run_undo(["5"], ask=yes) == 0
    assert path.read_bytes() == original


def test_nothing_to_undo(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_undo([], ask=yes) == 0
    assert "Nothing to undo" in capsys.readouterr().out


def test_undo_moves_the_backup_into_place_and_keeps_the_version_it_replaces(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = copy_set(tmp_path)
    original = path.read_bytes()
    first = save_edited(path, "1-first", monkeypatch)
    second = save_edited(path, "2-second", monkeypatch)
    backups = tmp_path / "abletoolz_backup"
    assert sorted(item.read_bytes() for item in backups.iterdir()) == sorted([original, first])

    assert run_undo(["2"], ask=yes) == 0
    assert path.read_bytes() == original
    assert sorted(item.read_bytes() for item in backups.iterdir()) == sorted([first, second])


def test_the_listing_says_when_each_backup_was_last_saved(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = copy_set(tmp_path)
    os.utime(path, (0, datetime.datetime(2019, 5, 4, 12, 30).timestamp()))
    save_edited(path, "1-run", monkeypatch)
    assert run_undo([], ask=lambda _: "") == 0
    assert "last saved 2019-05-04 12:30" in capsys.readouterr().out


def test_trim_keeps_each_sets_newest_backups_after_asking(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = copy_set(tmp_path)
    backups = tmp_path / "abletoolz_backup"
    backups.mkdir()
    old_copy = backups / "set__7.als"  # made before the undo history existed, years ago
    old_copy.write_bytes(path.read_bytes())
    os.utime(old_copy, (0, datetime.datetime(2015, 1, 1).timestamp()))
    # Every save keeps the set's modified time, so all three backups carry the same one.
    written = [save_edited(path, f"{number}-run", monkeypatch) for number in (1, 2, 3)]
    assert len(list(backups.iterdir())) == 4

    assert run_undo(["--trim", "1"], ask=lambda _: "") == 0
    assert len(list(backups.iterdir())) == 4
    assert run_undo(["--trim", "1"], ask=yes) == 0
    (kept,) = backups.iterdir()
    assert kept.read_bytes() == written[1]  # what the third run saved over
    assert [run.command for run in history()] == ["abletoolz 3-run"]


def test_an_undo_reaching_a_trimmed_backup_skips_that_set(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()
    a, b = copy_set(a_dir), copy_set(b_dir)
    b_original = b.read_bytes()
    save_edited(a, "1-both", monkeypatch)
    save_edited(b, "1-both", monkeypatch)
    a_written = save_edited(a, "2-a-again", monkeypatch)
    (old_a,) = [
        save.backup
        for run in history()
        for save in run.saves
        if run.command == "abletoolz 1-both" and save.path == a.resolve()
    ]
    old_a.unlink()  # what a per-set trim does to a's oldest backup
    assert run_undo(["2"], ask=yes) == 0
    assert b.read_bytes() == b_original and a.read_bytes() != a_written
