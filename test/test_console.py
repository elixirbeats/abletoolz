"""What a person reads: one block per set from its run record, and the run in one line."""

from __future__ import annotations

import datetime
import io
import pathlib

import pytest

from abletoolz.run import console
from abletoolz.run.report import CopyRecord, RefusalRecord, RelinkRecord, SetRecord

BROKEN = SetRecord(
    path="D:/sets/hazed.als",
    written="D:/sets/hazed.als",
    changed=True,
    live_version="Ableton Live 9.0.1",
    bpm=166.0,
    bars=64,
    seconds=92.5,
    backup="D:/sets/abletoolz_backup/hazed__1.als",
    relinks=[RelinkRecord(old="C:/old/kick.wav", new="D:/lib/kick.wav")],
    refused=[RefusalRecord(what=f"sample s{n}.wav", why="not found in the database") for n in range(8)],
    collected=[CopyRecord(source="D:/lib/kick.wav", destination="D:/sets/Samples/Imported/kick.wav")],
    samples_missing=27,
    samples_missing_by_name={"kick.wav": 20, **{f"s{n}.wav": 1 for n in range(7)}},
    plugins_missing={"Serum": 2},
    plugins_loaded={"Pro-Q 3": 4},
    edits=["folded every track"],
    notices=["saved by a beta of Live; some commands might not work properly"],
)


def printed(capsys: pytest.CaptureFixture[str], record: SetRecord, *, verbose: bool = False) -> list[str]:
    console.setup(verbose=verbose)
    console.render_set(record)
    return capsys.readouterr().out.splitlines()


def test_a_set_is_a_header_then_counts_with_its_problems_by_name(capsys: pytest.CaptureFixture[str]) -> None:
    lines = printed(capsys, BROKEN)
    assert lines[0] == "hazed.als  Live 9.0.1, 166 BPM, 64 bars, 1:32"
    assert lines[1] == (
        "  samples      27 missing: kick.wav x20, s0.wav, s1.wav, s2.wav, s3.wav, s4.wav, and 2 more (-v lists them)"
        " | 1 relinked | 1 collected into the project"
    )
    assert lines[2] == "  plugins      2 missing: Serum x2 | 4 load"
    assert lines[3] == "  left as is   8 file(s) left as they were"
    assert lines[4:10] == [f"      sample s{n}.wav: not found in the database" for n in range(6)]
    assert lines[10] == "      and 2 more (-v lists them)"
    assert lines[11:] == [
        "  edits        folded every track",
        "  note         saved by a beta of Live; some commands might not work properly",
        "  saved        to D:/sets/hazed.als, backup D:/sets/abletoolz_backup/hazed__1.als",
    ]


def test_v_adds_every_item_behind_the_counts(capsys: pytest.CaptureFixture[str]) -> None:
    lines = printed(capsys, BROKEN, verbose=True)
    assert "      C:/old/kick.wav -> D:/lib/kick.wav" in lines
    assert "      collected D:/lib/kick.wav -> D:/sets/Samples/Imported/kick.wav" in lines
    assert "      sample s7.wav: not found in the database" in lines
    assert not any("more (-v lists them)" in line for line in lines)


def test_a_cached_set_says_so_and_a_failed_one_says_why(capsys: pytest.CaptureFixture[str]) -> None:
    cached = SetRecord(
        path="a.als", cached=True, live_version="Ableton Live 11.3.42", bpm=120, bars=8, plugins_missing={}
    )
    assert printed(capsys, cached) == ["a.als  Live 11.3.42, 120 BPM, 8 bars (cached)", "  plugins      all load"]
    failed = SetRecord(path="b.als", error="SetError: an empty gzip stream")
    assert printed(capsys, failed) == ["b.als  failed: SetError: an empty gzip stream"]


def test_an_unsaved_change_says_how_to_save_it(capsys: pytest.CaptureFixture[str]) -> None:
    assert printed(capsys, SetRecord(path="c.als", changed=True))[-1] == "  not saved    add -s to write these changes"


def test_the_run_is_one_line_then_its_report_and_failures(capsys: pytest.CaptureFixture[str]) -> None:
    console.setup()
    failed = SetRecord(path="b.als", error="SetError: an empty gzip stream")
    console.render_run([BROKEN, failed], elapsed=datetime.timedelta(seconds=25.4), report=pathlib.Path("report.json"))
    assert capsys.readouterr().out.splitlines() == [
        "",
        "2 set(s) in 0:00:25 | 1 saved | 1 failed | 27 missing file(s) | 1 relinked | 8 left as they were"
        " | 2 missing plugin device(s)",
        "run report: report.json",
        "  failed b.als: SetError: an empty gzip stream",
    ]


def test_no_colour_when_the_output_is_no_terminal_or_no_color_is_set(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "\x1b" not in "".join(printed(capsys, BROKEN))
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    assert "\x1b" not in "".join(printed(capsys, BROKEN))


def test_a_name_the_output_cannot_encode_shows_as_a_stand_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run piped on Windows writes in its code page; one odd sample name must not end the run."""
    out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr("sys.stdout", out)
    console.setup()
    console.render_set(SetRecord(path="d.als", samples_missing=1, samples_missing_by_name={"kick 大.wav": 1}))
    out.flush()
    assert "kick ?.wav" in out.buffer.getvalue().decode("cp1252")  # type: ignore[attr-defined]
