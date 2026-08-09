"""Tests for abletoolz.dj_crates.

No test here depends on ffmpeg/ffprobe being installed: WAV sources exercise the
"copy unchanged" cache path, and the MP3/ffmpeg transcode path is exercised with
:func:`abletoolz.dj_crates._ffmpeg_transcode` monkeypatched out.
"""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import pytest

from abletoolz import dj_crates
from abletoolz.alc import AlcClip
from abletoolz.dj_crates import (
    PicksRow,
    audio_dest_extension,
    build_plan,
    compute_anchor,
    derive_clip_name,
    ensure_audio_cached,
    filter_rows,
    mp3_decoder_offset_seconds,
    parse_picks_row,
    read_duration_seconds,
    read_picks_tsv,
    relative_to_user_library,
    resolve_link_placement,
    run_crate_generation,
    sanitize_filename,
)

TSV_HEADER = (
    "path\tfilename\ttitle_key\tduration_s\tbpm\tbeat_offset_s\tstart_s\tstart_beat\tstart_bar\t"
    "drop_s\tdrop_beat\tdrop_bar\tbars_start_to_drop\tconfidence\tneeds_review\tverdict\thuman_reviewed\t"
    "mp3_tag\tmp3_lame_ext\tmp3_encoder_delay\tsamplerate"
)

TSV_HEADER_V1 = (
    "path\tfilename\ttitle_key\tduration_s\tbpm\tbeat_offset_s\tstart_s\tstart_beat\tstart_bar\t"
    "drop_s\tdrop_beat\tdrop_bar\tbars_start_to_drop\tconfidence\tneeds_review\tverdict\thuman_reviewed"
)


def _write_test_wav(path: Path, *, duration_s: float, rate: int = 8000) -> None:
    """Synthesize a tiny silent mono 16-bit WAV of an exact duration (stdlib only)."""
    nframes = round(duration_s * rate)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(rate)
        wav_file.writeframes(b"\x00\x00" * nframes)


def _make_row(**overrides: object) -> PicksRow:
    """A PicksRow with sane defaults, overridable per test."""
    defaults: dict[str, object] = {
        "path": Path("track.wav"),
        "filename": "track.wav",
        "title_key": "track",
        "duration_s": 10.0,
        "bpm": 120.0,
        "beat_offset_s": 0.0,
        "start_s": 0.0,
        "start_beat": 0.0,
        "start_bar": 0.0,
        "drop_s": None,
        "drop_beat": None,
        "drop_bar": None,
        "bars_start_to_drop": None,
        "confidence": 1.0,
        "needs_review": False,
        "verdict": "accepted",
        "human_reviewed": True,
        "mp3_tag": "",
        "mp3_lame_ext": False,
        "mp3_encoder_delay": None,
        "samplerate": None,
    }
    defaults.update(overrides)
    return PicksRow(**defaults)  # type: ignore[arg-type]  # kwargs are dynamically typed by design


def _mp3_row_fields(*, tag: str = "Info", lame_ext: bool = True, delay: int | None = 576) -> dict[str, object]:
    """v2 header-column overrides for an MP3 row (default: the Info+gapless class)."""
    return {"mp3_tag": tag, "mp3_lame_ext": lame_ext, "mp3_encoder_delay": delay, "samplerate": 44100}


# ── TSV parsing ──────────────────────────────────────────────────────────────


def test_parse_picks_row_full_fields() -> None:
    """A fully populated TSV record round-trips through parse_picks_row."""
    record = {
        "path": "tunes/tune.mp3",
        "filename": "tune - 9A - Energy 7.mp3",
        "title_key": "tune",
        "duration_s": "266.25",
        "bpm": "160.0",
        "beat_offset_s": "0.005",
        "start_s": "0.38",
        "start_beat": "1.0",
        "start_bar": "0.25",
        "drop_s": "24.38",
        "drop_beat": "65.0",
        "drop_bar": "16.25",
        "bars_start_to_drop": "16.0",
        "confidence": "0.3",
        "needs_review": "True",
        "verdict": "corrected",
        "human_reviewed": "True",
        "mp3_tag": "Info",
        "mp3_lame_ext": "True",
        "mp3_encoder_delay": "576",
        "samplerate": "44100",
    }
    row = parse_picks_row(record)
    assert row.path == Path("tunes/tune.mp3")
    assert row.bpm == 160.0
    assert row.drop_s == 24.38
    assert row.needs_review is True
    assert row.human_reviewed is True
    assert row.verdict == "corrected"
    assert row.mp3_tag == "Info"
    assert row.mp3_lame_ext is True
    assert row.mp3_encoder_delay == 576
    assert row.samplerate == 44100


def test_parse_picks_row_empty_drop_is_none() -> None:
    """Empty drop_s/drop_beat/drop_bar/bars_start_to_drop fields parse to None."""
    record = {
        "path": "track.mp3",
        "filename": "track.mp3",
        "title_key": "track",
        "duration_s": "100.0",
        "bpm": "174.0",
        "beat_offset_s": "0.1",
        "start_s": "0.1",
        "start_beat": "0.0",
        "start_bar": "0.0",
        "drop_s": "",
        "drop_beat": "",
        "drop_bar": "",
        "bars_start_to_drop": "",
        "confidence": "1.0",
        "needs_review": "False",
        "verdict": "",
        "human_reviewed": "False",
        "mp3_tag": "",
        "mp3_lame_ext": "",
        "mp3_encoder_delay": "",
        "samplerate": "",
    }
    row = parse_picks_row(record)
    assert row.drop_s is None
    assert row.drop_beat is None
    assert row.drop_bar is None
    assert row.bars_start_to_drop is None
    assert row.needs_review is False
    assert row.human_reviewed is False
    assert row.verdict == ""
    assert row.mp3_lame_ext is False
    assert row.mp3_encoder_delay is None
    assert row.samplerate is None


def test_parse_picks_row_rejects_bad_bool() -> None:
    """A boolean field that isn't exactly "True"/"False" is a hard parse error."""
    record = {
        "path": "t.mp3",
        "filename": "t.mp3",
        "title_key": "t",
        "duration_s": "1.0",
        "bpm": "120.0",
        "beat_offset_s": "0.0",
        "start_s": "0.0",
        "start_beat": "0.0",
        "start_bar": "0.0",
        "drop_s": "",
        "drop_beat": "",
        "drop_bar": "",
        "bars_start_to_drop": "",
        "confidence": "1.0",
        "needs_review": "yes",  # invalid
        "verdict": "",
        "human_reviewed": "False",
    }
    with pytest.raises(ValueError, match=r"True.*False"):
        parse_picks_row(record)


def test_read_picks_tsv(tmp_path: Path) -> None:
    """A real TSV file (header + two rows) parses into a list of PicksRow."""
    tsv_path = tmp_path / "picks.tsv"
    tsv_path.write_text(
        TSV_HEADER + "\n"
        "a.mp3" + "\ta.mp3\ta\t100.0\t174.0\t0.1\t0.1\t0.0\t0.0\t\t\t\t\t1.0\tFalse\t\tFalse"
        "\tInfo\tTrue\t576\t44100\n"
        "b.mp3"
        + "\tb.mp3\tb\t200.0\t170.0\t0.2\t0.2\t0.0\t0.0\t50.0\t144.0\t36.0\t35.9\t0.9\tTrue\tcorrected\tTrue"
        "\tXing\tFalse\t\t44100\n",
        encoding="utf-8",
    )
    rows = read_picks_tsv(tsv_path)
    assert len(rows) == 2
    assert rows[0].drop_s is None
    assert rows[0].mp3_tag == "Info"
    assert rows[1].drop_s == 50.0
    assert rows[1].human_reviewed is True
    assert rows[1].mp3_lame_ext is False


def test_read_picks_tsv_rejects_v1_schema(tmp_path: Path) -> None:
    """A pre-v2 export (no header-class columns) must fail loudly, not run with guesses."""
    tsv_path = tmp_path / "old.tsv"
    tsv_path.write_text(
        TSV_HEADER_V1 + "\n"
        "a.mp3" + "\ta.mp3\ta\t100.0\t174.0\t0.1\t0.1\t0.0\t0.0\t\t\t\t\t1.0\tFalse\t\tFalse\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="pre-v2"):
        read_picks_tsv(tsv_path)


# ── filter_rows ──────────────────────────────────────────────────────────────


def test_filter_rows_include_is_case_insensitive_and_ored() -> None:
    """Multiple --include substrings are OR'd together, case-insensitively."""
    rows = [
        _make_row(path=Path(r"\\nas\Dnb Picks\a.mp3")),
        _make_row(path=Path(r"\\nas\House Picks\b.mp3")),
        _make_row(path=Path(r"\\nas\Techno Picks\c.mp3")),
    ]
    filtered = filter_rows(rows, includes=["dnb picks", "TECHNO"])
    assert {str(r.path) for r in filtered} == {r"\\nas\Dnb Picks\a.mp3", r"\\nas\Techno Picks\c.mp3"}


def test_filter_rows_reviewed_only_and_limit() -> None:
    """--reviewed-only and --limit compose: filter first, then cap the count."""
    rows = [
        _make_row(title_key="a", human_reviewed=True),
        _make_row(title_key="b", human_reviewed=False),
        _make_row(title_key="c", human_reviewed=True),
    ]
    reviewed = filter_rows(rows, reviewed_only=True)
    assert [r.title_key for r in reviewed] == ["a", "c"]
    limited = filter_rows(rows, limit=1)
    assert [r.title_key for r in limited] == ["a"]


# ── compute_anchor (bar-fold rule) ───────────────────────────────────────────


def test_compute_anchor_no_drop_uses_beat_offset() -> None:
    """With no drop cue, the anchor is simply the tagged first beat."""
    result = compute_anchor(bpm=174.0, beat_offset_s=0.5, drop_s=None)
    assert result.anchor_s == 0.5
    assert result.n_bars is None
    assert result.bar_phase_warning is False


def test_compute_anchor_gravity_lands_on_beat_offset() -> None:
    """Real cue_finder numbers (Agbo - Gravity): in-phase drop, anchor == beat_offset."""
    bpm = 174.0
    beat_offset_s = 6.89655172365633e-05
    drop_s = 44.138
    result = compute_anchor(bpm=bpm, beat_offset_s=beat_offset_s, drop_s=drop_s)
    assert result.anchor_s == pytest.approx(beat_offset_s)
    assert result.n_bars == 32
    assert result.n_bars * 4 == 128  # matches cue_finder's tagged drop_beat
    assert result.bar_phase_warning is False
    assert result.residual_bars == pytest.approx(0.0, abs=1e-9)


def test_compute_anchor_caligo_flags_bar_phase_mismatch() -> None:
    """Real cue_finder numbers (Alix Perez & Monty - Caligo): 47.75-bar fold, flagged."""
    bpm = 174.0
    beat_offset_s = 0.3119310344827608
    drop_s = 66.174
    result = compute_anchor(bpm=bpm, beat_offset_s=beat_offset_s, drop_s=drop_s, bar_tolerance=0.02)
    assert result.n_bars == 47
    assert result.anchor_s == pytest.approx(1.3464137931034514)
    assert result.anchor_s >= 0.0
    assert result.residual_bars == pytest.approx(-0.25, abs=1e-6)
    assert result.bar_phase_warning is True


def test_compute_anchor_respects_bar_tolerance() -> None:
    """A generous --bar-tolerance suppresses the warning for the same off-phase drop."""
    # Same Caligo numbers, but a generous tolerance should not flag it.
    result = compute_anchor(bpm=174.0, beat_offset_s=0.3119310344827608, drop_s=66.174, bar_tolerance=0.5)
    assert result.bar_phase_warning is False


# ── Clip naming ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("6blocc - Control The Floor - 9A - Energy 7.mp3", "6blocc - Control The Floor"),
        ("Alix Perez & Monty - Caligo - 9A - Energy 6.mp3", "Alix Perez & Monty - Caligo"),
        ("Some Tune - 12B.wav", "Some Tune"),
        ("bass.wav", "bass"),
        ("Track With No Suffix.mp3", "Track With No Suffix"),
    ],
)
def test_derive_clip_name(filename: str, expected: str) -> None:
    """Trailing camelot-key(+energy) suffixes are stripped when trivially matchable."""
    assert derive_clip_name(filename) == expected


def test_sanitize_filename_strips_unsafe_chars() -> None:
    """Filesystem-unsafe characters are replaced with underscores."""
    assert sanitize_filename('Tune / Remix: "Edit"') == "Tune _ Remix_ _Edit_"


# ── Audio cache: extension + idempotent transcode/copy ───────────────────────


def test_audio_dest_extension_lossless_kept_regardless_of_format() -> None:
    """WAV/AIFF sources keep their own extension no matter what --audio-format is."""
    assert audio_dest_extension(Path("a.wav"), "wav") == ".wav"
    assert audio_dest_extension(Path("a.wav"), "flac") == ".wav"
    assert audio_dest_extension(Path("a.aiff"), "wav") == ".aiff"


def test_audio_dest_extension_compressed_uses_audio_format() -> None:
    """Compressed sources (MP3) get the --audio-format's extension."""
    assert audio_dest_extension(Path("a.mp3"), "wav") == ".wav"
    assert audio_dest_extension(Path("a.mp3"), "flac") == ".flac"


def test_ensure_audio_cached_copies_lossless_source_idempotently(tmp_path: Path) -> None:
    """A WAV source is copied verbatim; a second call is a no-op cache hit."""
    src = tmp_path / "src.wav"
    _write_test_wav(src, duration_s=1.0)
    dest = tmp_path / "cache" / "src.wav"

    did_work_first = ensure_audio_cached(src, dest, "wav")
    assert did_work_first is True
    assert dest.read_bytes() == src.read_bytes()

    dest.write_bytes(b"MODIFIED")  # prove the second call doesn't touch it again
    did_work_second = ensure_audio_cached(src, dest, "wav")
    assert did_work_second is False
    assert dest.read_bytes() == b"MODIFIED"


def test_ensure_audio_cached_transcodes_compressed_source_idempotently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An MP3 source is transcoded once (mocked ffmpeg); a second call is a cache hit."""
    src = tmp_path / "src.mp3"
    src.write_bytes(b"fake mp3 data")
    dest = tmp_path / "cache" / "src.wav"

    calls: list[tuple[Path, Path]] = []

    def fake_transcode(fake_src: Path, fake_dest: Path, audio_format: str) -> None:
        calls.append((fake_src, fake_dest))
        _write_test_wav(fake_dest, duration_s=2.0)

    monkeypatch.setattr(dj_crates, "_ffmpeg_transcode", fake_transcode)

    did_work_first = ensure_audio_cached(src, dest, "wav")
    assert did_work_first is True
    assert calls == [(src, dest)]
    assert dest.exists()

    did_work_second = ensure_audio_cached(src, dest, "wav")
    assert did_work_second is False
    assert calls == [(src, dest)]  # ffmpeg not invoked again


def test_ensure_audio_cached_never_shells_out_for_wav(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression guard: WAV sources must not depend on ffmpeg being installed."""

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("ffmpeg must not be invoked for a lossless source")

    monkeypatch.setattr(dj_crates, "_ffmpeg_transcode", boom)
    src = tmp_path / "src.wav"
    _write_test_wav(src, duration_s=0.5)
    dest = tmp_path / "dest.wav"
    assert ensure_audio_cached(src, dest, "wav") is True


def test_read_duration_seconds_wav_exact(tmp_path: Path) -> None:
    """WAV duration is read exactly via the stdlib wave module (no ffprobe)."""
    wav_path = tmp_path / "clip.wav"
    _write_test_wav(wav_path, duration_s=3.5, rate=8000)
    assert read_duration_seconds(wav_path) == pytest.approx(3.5)


# ── relative_to_user_library ──────────────────────────────────────────────────


def test_relative_to_user_library_extracts_suffix() -> None:
    """Everything below the "User Library" path component is returned."""
    path = Path("somewhere/Ableton/User Library/DJ Crates/Audio/tune.wav")
    assert relative_to_user_library(path) == Path("DJ Crates/Audio/tune.wav")


def test_relative_to_user_library_requires_the_folder() -> None:
    """A path outside any "User Library" folder can't be made library-relative."""
    with pytest.raises(ValueError, match="User Library"):
        relative_to_user_library(Path("elsewhere/tune.wav"))


# ── End-to-end: plan + generate against the packaged template ────────────────


def test_run_crate_generation_writes_valid_alc_clips(tmp_path: Path) -> None:
    """--audio wav: full/drop .alc clips are written with correct grid, name, and sample ref."""
    user_library = tmp_path / "User Library"
    audio_dir = user_library / "DJ Crates" / "Audio"
    crates_dir = user_library / "DJ Crates"

    src_dir = tmp_path / "source"
    src_dir.mkdir()
    src_wav = src_dir / "Test Tune - 9A - Energy 7.wav"
    # bpm 120 -> bar_s 2.0s; drop at 8.0s is exactly 4 bars in, beat_offset 0.0 is in-phase.
    _write_test_wav(src_wav, duration_s=12.0, rate=8000)

    row = _make_row(
        path=src_wav,
        filename=src_wav.name,
        title_key="testtune",
        bpm=120.0,
        beat_offset_s=0.0,
        start_s=1.0,  # off the anchor -> should get an explicit start marker
        drop_s=8.0,
        drop_beat=16.0,
        human_reviewed=True,
        verdict="accepted",
    )

    report = run_crate_generation(
        [row],
        crate_name="Test Crate",
        audio_dir=audio_dir,
        crates_dir=crates_dir,
        audio_mode="wav",
        bar_tolerance=0.02,
        dry_run=False,
    )

    assert not report.failures
    assert report.generated_count == 1
    assert report.drop_generated_count == 1
    assert report.cached_skips == 0

    crate_dir = crates_dir / "Test Crate"
    full_path = crate_dir / "Test Tune (full).alc"
    drop_path = crate_dir / "Test Tune (drop).alc"
    assert full_path.exists()
    assert drop_path.exists()

    cached_audio = audio_dir / "Test Tune.wav"
    assert cached_audio.exists()

    full_clip = AlcClip.load(full_path)
    assert full_clip.name == "Test Tune"
    markers = full_clip.markers
    assert len(markers) >= 2
    assert markers[0].sec_time == pytest.approx(0.0)  # anchor == beat_offset for this in-phase track
    assert markers[0].beat_time == 0.0
    drop_marker = next(m for m in markers if m.sec_time == pytest.approx(8.0))
    assert drop_marker.beat_time == pytest.approx(16.0)
    # The start cue adds no marker: the clip itself begins there (CurrentStart),
    # and a marker would stack against the anchor stating the start twice.
    assert not any(m.sec_time == pytest.approx(1.0) for m in markers)

    file_ref = full_clip.clip.find("SampleRef/FileRef")
    assert file_ref is not None
    rel_path = file_ref.find("RelativePath")
    assert rel_path is not None
    assert rel_path.get("Value") == "DJ Crates/Audio/Test Tune.wav"

    current_start = full_clip.clip.find("CurrentStart")
    assert current_start is not None
    assert float(current_start.get("Value", "")) == pytest.approx(2.0)  # starts at the start cue, not the anchor

    drop_clip = AlcClip.load(drop_path)
    assert drop_clip.name == "Test Tune DROP"
    drop_current_start = drop_clip.clip.find("CurrentStart")
    assert drop_current_start is not None
    assert float(drop_current_start.get("Value", "")) == pytest.approx(16.0)  # drop variant starts at the drop

    # Re-running is idempotent: cache hit, no duplicate ffmpeg/copy work.
    report_again = run_crate_generation(
        [row],
        crate_name="Test Crate",
        audio_dir=audio_dir,
        crates_dir=crates_dir,
        audio_mode="wav",
        bar_tolerance=0.02,
        dry_run=False,
    )
    assert report_again.cached_skips == 1


def test_run_crate_generation_no_drop_track_has_no_drop_variant(tmp_path: Path) -> None:
    """A track with no drop cue only gets a "(full)" clip -- no "(drop)" file."""
    user_library = tmp_path / "User Library"
    audio_dir = user_library / "DJ Crates" / "Audio"
    crates_dir = user_library / "DJ Crates"

    src_wav = tmp_path / "No Drop Tune.wav"
    _write_test_wav(src_wav, duration_s=5.0, rate=8000)

    row = _make_row(path=src_wav, filename=src_wav.name, bpm=120.0, beat_offset_s=0.25, drop_s=None)

    report = run_crate_generation(
        [row],
        crate_name="Crate",
        audio_dir=audio_dir,
        crates_dir=crates_dir,
        audio_mode="wav",
        bar_tolerance=0.02,
        dry_run=False,
    )
    assert not report.failures
    assert report.generated_count == 1
    assert report.drop_generated_count == 0
    assert len(report.no_drop_tracks) == 1

    crate_dir = crates_dir / "Crate"
    assert (crate_dir / "No Drop Tune (full).alc").exists()
    assert not (crate_dir / "No Drop Tune (drop).alc").exists()


def test_run_crate_generation_dry_run_writes_nothing(tmp_path: Path) -> None:
    """--dry-run reports the plan but touches no filesystem paths at all."""
    user_library = tmp_path / "User Library"
    audio_dir = user_library / "DJ Crates" / "Audio"
    crates_dir = user_library / "DJ Crates"
    src_wav = tmp_path / "Dry Run Tune.wav"
    # File doesn't even need to exist for a dry run -- no I/O should be attempted.
    row = _make_row(path=src_wav, filename=src_wav.name, bpm=140.0, beat_offset_s=0.0, drop_s=16.0)

    report = run_crate_generation(
        [row],
        crate_name="Crate",
        audio_dir=audio_dir,
        crates_dir=crates_dir,
        audio_mode="wav",
        bar_tolerance=0.02,
        dry_run=True,
    )
    assert report.generated_count == 1
    assert report.drop_generated_count == 1
    assert not audio_dir.exists()
    assert not crates_dir.exists()


def test_build_plan_uses_derived_name_and_bar_fold_anchor(tmp_path: Path) -> None:
    """build_plan wires derive_clip_name/sanitize_filename/compute_anchor together."""
    row = _make_row(
        path=Path("t.mp3"),
        filename="Some Tune - 9A - Energy 7.mp3",
        bpm=174.0,
        beat_offset_s=6.89655172365633e-05,
        drop_s=44.138,
    )
    plan = build_plan(row, bar_tolerance=0.02)
    assert plan.clip_name == "Some Tune"
    assert plan.safe_name == "Some Tune"
    assert plan.anchor.anchor_s == pytest.approx(row.beat_offset_s)
    assert plan.has_drop is True


# ── ensure ffprobe path is at least reachable/mockable for non-wav durations ──


def test_read_duration_seconds_non_wav_uses_ffprobe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A non-WAV file's duration is read via a (mocked) ffprobe call."""
    flac_path = tmp_path / "clip.flac"
    flac_path.write_bytes(b"not a real flac, ffprobe is mocked")

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert cmd[0] == "ffprobe"
        return subprocess.CompletedProcess(cmd, 0, stdout="4.25\n", stderr="")

    monkeypatch.setattr("abletoolz.dj_crates.subprocess.run", fake_run)
    assert read_duration_seconds(flac_path) == pytest.approx(4.25)


# ── MP3 decoder offset (link mode) ────────────────────────────────────────────


def _synthetic_mp3_bytes(
    *, tag: bytes = b"Info", signature: bytes = b"LAME3.99r", delay: int = 576, with_gapless: bool = True
) -> bytes:
    """Build a minimal MPEG1-L3 stereo first frame with a Xing/Info header.

    ``tag`` picks the header kind ("Info" = Live plays the frame as audio, +1152;
    "Xing" = both decoders skip it). ``with_gapless`` controls whether the extension
    carries ``signature`` + a plausible delay; without it the region stays zeros.
    """
    frame = bytearray(1044)
    frame[0:4] = bytes((0xFF, 0xFB, 0x90, 0x00))  # MPEG1 L3, 128kbps, 44.1kHz, stereo
    xing_at = 4 + 32
    frame[xing_at : xing_at + 4] = tag
    frame[xing_at + 4 : xing_at + 8] = (0x07).to_bytes(4, "big")  # frames+bytes+TOC
    ext = xing_at + 8 + 4 + 4 + 100
    if with_gapless:
        frame[ext : ext + len(signature)] = signature
        frame[ext + 21] = (delay >> 4) & 0xFF
        frame[ext + 22] = ((delay & 0x0F) << 4) | 0x06
        frame[ext + 23] = 0xC0
    return bytes(frame)


def _write_mp3(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_mp3_offset_measured_cases(tmp_path: Path) -> None:
    """Trim+surplus model classes: Info+sig 2257, Xing 1105, tagless 0, Info-no-sig 1152."""
    audio_frame = bytes((0xFF, 0xFB, 0x90, 0x00)) + b"\x01" * 1040  # real first frame, no tag
    cases = [
        ("info_lame.mp3", _synthetic_mp3_bytes(tag=b"Info", signature=b"LAME3.97 "), 2257.0),  # Gravity
        ("info_lavf.mp3", _synthetic_mp3_bytes(tag=b"Info", signature=b"Lavf55.33"), 2257.0),  # Caligo
        ("info_nosig.mp3", _synthetic_mp3_bytes(tag=b"Info", with_gapless=False), 1152.0),  # repaired files
        ("xing_lavf.mp3", _synthetic_mp3_bytes(tag=b"Xing", signature=b"Lavf56.4."), 1105.0),  # Dune
        ("xing_lame.mp3", _synthetic_mp3_bytes(tag=b"Xing", signature=b"LAME3.99r"), 1105.0),
        ("tagless.mp3", audio_frame, 0.0),  # golden_no_info: real frame, no metadata tag
        ("unparseable.mp3", b"\x00" * 5000, 2257.0),  # no frame found: assume majority class
        ("vbr_delay.mp3", _synthetic_mp3_bytes(tag=b"Xing", delay=1024), 1024.0 + 529.0),  # per-file trim
    ]
    for name, data, expected in cases:
        path = _write_mp3(tmp_path, name, data)
        assert mp3_decoder_offset_seconds(path, 44100) * 44100 == pytest.approx(expected), name


def test_mp3_offset_seeks_past_large_id3(tmp_path: Path) -> None:
    """Multi-MB ID3 blocks (album art) must be seeked past by declared size (R.E.M bug)."""
    art = b"\xAA" * 500_000  # bigger than any sane read window
    size = len(art)
    id3 = bytearray(b"ID3\x04\x00\x00")
    id3 += bytes(((size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F))
    id3 += art
    combined = _write_mp3(tmp_path, "big_art.mp3", bytes(id3) + _synthetic_mp3_bytes(tag=b"Info"))
    assert mp3_decoder_offset_seconds(combined, 44100) * 44100 == pytest.approx(2257.0)


def test_mp3_offset_ignores_loose_lame_strings(tmp_path: Path) -> None:
    """Loose encoder strings (e.g. ID3 TSSE comments) are irrelevant to the class."""
    data = _synthetic_mp3_bytes(tag=b"Xing", with_gapless=False) + b"LAME3.100 loose comment text"
    loose = _write_mp3(tmp_path, "loose.mp3", data)
    assert mp3_decoder_offset_seconds(loose, 44100) == 0.0  # Xing skipped by both, no sig -> no trim


def test_mp3_gapless_sniffer_skips_id3v2(tmp_path: Path) -> None:
    """An ID3v2 tag before the first frame must be skipped, not scanned for sync bytes."""
    id3 = bytearray(b"ID3\x04\x00\x00")
    body = b"\x00" * 200
    size = len(body)
    id3 += bytes(((size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F))
    id3 += body
    combined = _write_mp3(tmp_path, "id3.mp3", bytes(id3) + _synthetic_mp3_bytes())
    assert mp3_decoder_offset_seconds(combined, 44100) * 44100 == pytest.approx(2257.0)


def test_mp3_decoder_offset_scales_with_sample_rate(tmp_path: Path) -> None:
    """The same sample count means a different number of seconds at a different rate."""
    tagged = _write_mp3(tmp_path, "tagged.mp3", _synthetic_mp3_bytes())
    assert mp3_decoder_offset_seconds(tagged, 48000) == pytest.approx(2257 / 48000)


# ── Link mode audio placement ─────────────────────────────────────────────────


def _fake_ffprobe(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
    """Dispatch canned ffprobe output by which -show_entries was requested."""
    show_entries = cmd[cmd.index("-show_entries") + 1]
    if show_entries == "format=duration":
        return subprocess.CompletedProcess(cmd, 0, stdout="120.0\n", stderr="")
    if show_entries == "stream=sample_rate":
        return subprocess.CompletedProcess(cmd, 0, stdout="44100\n", stderr="")
    raise AssertionError(f"unexpected ffprobe args: {cmd}")


def _fake_ffprobe_at_rate(sample_rate: int) -> object:
    """Like :func:`_fake_ffprobe`, but reporting a caller-chosen (non-44.1k) sample rate."""

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        show_entries = cmd[cmd.index("-show_entries") + 1]
        if show_entries == "format=duration":
            return subprocess.CompletedProcess(cmd, 0, stdout="120.0\n", stderr="")
        if show_entries == "stream=sample_rate":
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{sample_rate}\n", stderr="")
        raise AssertionError(f"unexpected ffprobe args: {cmd}")

    return fake_run


def test_resolve_link_placement_mp3_applies_offset_from_columns(tmp_path: Path) -> None:
    """A 44.1kHz Info+gapless MP3 gets 2257 samples of offset from v2 columns alone."""
    mp3_path = tmp_path / "tune.mp3"  # never even created: columns are the source
    audio_dir = tmp_path / "User Library" / "DJ Crates" / "Audio"

    row = _make_row(path=mp3_path, filename="tune.mp3", duration_s=120.0, **_mp3_row_fields())
    placement = resolve_link_placement(row, audio_dir=audio_dir, safe_name="tune")

    expected_offset = 2257 / 44100
    assert placement.absolute_path == mp3_path
    assert placement.time_offset_s == pytest.approx(expected_offset)
    assert placement.duration_s == pytest.approx(120.0 + expected_offset)
    assert placement.did_work is False
    assert placement.offset_unverified is False  # 44100 Hz is the verified rate
    assert placement.relative_path == Path("DJ Crates/Audio/tune.mp3")
    # Deliberately non-resolving: link mode never writes anything under audio_dir.
    assert not (audio_dir / "tune.mp3").exists()


def test_resolve_link_placement_offset_classes_from_columns(tmp_path: Path) -> None:
    """Column-computed classes: Info+ext 2257, Info-no-ext 1152, Xing 1105, tagless 0."""
    audio_dir = tmp_path / "User Library" / "DJ Crates" / "Audio"
    cases = [
        (_mp3_row_fields(), 2257.0),
        (_mp3_row_fields(lame_ext=False, delay=None), 1152.0),  # spliced repair class
        (_mp3_row_fields(tag="Xing"), 1105.0),
        (_mp3_row_fields(tag="", lame_ext=False, delay=None), 0.0),
    ]
    for fields, expected in cases:
        row = _make_row(path=tmp_path / "t.mp3", filename="t.mp3", **fields)
        placement = resolve_link_placement(row, audio_dir=audio_dir, safe_name="t")
        assert placement.time_offset_s * 44100 == pytest.approx(expected), fields


def test_resolve_link_placement_mp3_flags_non_44k_as_unverified(tmp_path: Path) -> None:
    """A non-44.1kHz MP3 still gets the offset (scaled), but is flagged unverified."""
    audio_dir = tmp_path / "User Library" / "DJ Crates" / "Audio"
    fields = _mp3_row_fields()
    fields["samplerate"] = 48000
    row = _make_row(path=tmp_path / "tune.mp3", filename="tune.mp3", **fields)
    placement = resolve_link_placement(row, audio_dir=audio_dir, safe_name="tune")

    assert placement.time_offset_s == pytest.approx(2257 / 48000)
    assert placement.offset_unverified is True


def test_resolve_link_placement_wav_has_no_offset(tmp_path: Path) -> None:
    """A WAV source in link mode gets zero offset and is referenced in place."""
    wav_path = tmp_path / "tune.wav"
    audio_dir = tmp_path / "User Library" / "DJ Crates" / "Audio"

    row = _make_row(path=wav_path, filename="tune.wav", duration_s=5.0)
    placement = resolve_link_placement(row, audio_dir=audio_dir, safe_name="tune")

    assert placement.absolute_path == wav_path
    assert placement.time_offset_s == 0.0
    assert placement.duration_s == pytest.approx(5.0)
    assert placement.did_work is False


def test_resolve_link_placement_mp3_missing_samplerate_is_loud(tmp_path: Path) -> None:
    """An MP3 row without a samplerate column value is a hard error, not a guess."""
    row = _make_row(path=tmp_path / "no_tag.mp3", filename="no_tag.mp3")
    with pytest.raises(ValueError, match="samplerate"):
        resolve_link_placement(
            row, audio_dir=tmp_path / "User Library" / "DJ Crates" / "Audio", safe_name="no_tag"
        )


def test_run_crate_generation_link_mode_mp3_shifts_grid_by_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end link mode: no audio is copied, and every authored time is shifted."""
    user_library = tmp_path / "User Library"
    audio_dir = user_library / "DJ Crates" / "Audio"
    crates_dir = user_library / "DJ Crates"

    src_dir = tmp_path / "source"
    src_dir.mkdir()
    mp3_path = src_dir / "Test Tune - 9A - Energy 7.mp3"

    # bpm 120 -> bar_s 2.0s; drop at 8.0s is exactly 4 bars in, beat_offset 0.0 is in-phase.
    row = _make_row(
        path=mp3_path,
        filename=mp3_path.name,
        bpm=120.0,
        beat_offset_s=0.0,
        start_s=0.0,
        drop_s=8.0,
        drop_beat=16.0,
        **_mp3_row_fields(),
    )

    report = run_crate_generation(
        [row],
        crate_name="Test Crate",
        audio_dir=audio_dir,
        crates_dir=crates_dir,
        audio_mode="link",
        bar_tolerance=0.02,
        dry_run=False,
    )
    assert not report.failures
    assert report.generated_count == 1
    assert report.drop_generated_count == 1

    # No audio was ever copied or transcoded anywhere.
    assert not audio_dir.exists()

    offset = 2257 / 44100
    crate_dir = crates_dir / "Test Crate"
    full_clip = AlcClip.load(crate_dir / "Test Tune (full).alc")
    markers = full_clip.markers
    assert markers[0].sec_time == pytest.approx(offset)  # anchor shifted by the decoder offset
    assert markers[0].beat_time == 0.0
    drop_marker = next(m for m in markers if m.beat_time == pytest.approx(16.0))
    assert drop_marker.sec_time == pytest.approx(8.0 + offset)

    file_ref = full_clip.clip.find("SampleRef/FileRef")
    assert file_ref is not None
    path_el = file_ref.find("Path")
    rel_el = file_ref.find("RelativePath")
    assert path_el is not None and rel_el is not None
    assert path_el.get("Value") == mp3_path.as_posix()
    assert rel_el.get("Value") == "DJ Crates/Audio/Test Tune.mp3"

    current_start = full_clip.clip.find("CurrentStart")
    assert current_start is not None
    assert float(current_start.get("Value", "")) == pytest.approx(0.0)  # full variant still starts at beat 0

    drop_clip = AlcClip.load(crate_dir / "Test Tune (drop).alc")
    drop_current_start = drop_clip.clip.find("CurrentStart")
    assert drop_current_start is not None
    assert float(drop_current_start.get("Value", "")) == pytest.approx(16.0)


def test_run_crate_generation_link_mode_non_44k_mp3_is_flagged_not_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-44.1kHz MP3 still generates in link mode -- just flagged as unverified."""
    user_library = tmp_path / "User Library"
    audio_dir = user_library / "DJ Crates" / "Audio"
    crates_dir = user_library / "DJ Crates"

    mp3_path = tmp_path / "off_rate.mp3"
    fields = _mp3_row_fields()
    fields["samplerate"] = 48000
    row = _make_row(path=mp3_path, filename="off_rate.mp3", bpm=120.0, beat_offset_s=0.0, drop_s=8.0, **fields)

    report = run_crate_generation(
        [row],
        crate_name="Crate",
        audio_dir=audio_dir,
        crates_dir=crates_dir,
        audio_mode="link",
        bar_tolerance=0.02,
        dry_run=False,
    )
    assert not report.failures
    assert report.generated_count == 1
    assert len(report.non_44k_warnings) == 1
    assert (crates_dir / "Crate" / "off_rate (full).alc").exists()


def test_run_crate_generation_link_mode_default_audio_flag() -> None:
    """--audio defaults to "link" (no copy, no transcode)."""
    args = dj_crates.parse_arguments(["picks.tsv", "--crate", "Test"])
    assert args.audio == "link"




def test_resolve_output_dirs_explicit_paths_win() -> None:
    """Explicit dirs pass through untouched; audio defaults inside the crates dir."""
    crates, audio = dj_crates.resolve_output_dirs(Path("crates"), None)
    assert crates == Path("crates")
    assert audio == Path("crates") / "Audio"


def test_resolve_output_dirs_uses_user_library(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(dj_crates, "default_ableton_user_library", lambda: tmp_path)
    crates, audio = dj_crates.resolve_output_dirs(None, None)
    assert crates == tmp_path / "DJ Crates"
    assert audio == crates / "Audio"


def test_resolve_output_dirs_refuses_to_guess(monkeypatch: pytest.MonkeyPatch) -> None:
    """No User Library found and no --crates-dir: error out, never invent a path."""
    monkeypatch.setattr(dj_crates, "default_ableton_user_library", lambda: None)
    with pytest.raises(ValueError, match="--crates-dir"):
        dj_crates.resolve_output_dirs(None, None)


def test_mirror_requires_a_marker_value() -> None:
    """--mirror without a folder name is an error; there is no default marker."""
    with pytest.raises(SystemExit):
        dj_crates.parse_arguments(["picks.tsv", "--crate", "Test", "--mirror"])
