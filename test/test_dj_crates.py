"""Tests for abletoolz.dj_crates.

No test here depends on ffmpeg/ffprobe being installed: WAV sources exercise the
"copy unchanged" cache path, and the MP3/ffmpeg transcode path is exercised with
:func:`abletoolz.dj_crates._ffmpeg_transcode` monkeypatched out.
"""

from __future__ import annotations

import json
import subprocess
import wave
from pathlib import Path

import pytest

from abletoolz import dj_crates
from abletoolz.alc import AlcClip
from abletoolz.dj_crates import (
    AsdOutcome,
    Output,
    RunReport,
    TrackGrid,
    audio_dest_extension,
    build_plan,
    compute_anchor,
    derive_clip_name,
    ensure_audio_cached,
    filter_rows,
    library_relative_path,
    mp3_decoder_offset_seconds,
    read_duration_seconds,
    read_mp3_header,
    read_track_list,
    resolve_link_placement,
    run_crate_generation,
    sanitize_filename,
)
from abletoolz.formats.asd import AsdFile, write_grid


def _write_test_wav(path: Path, *, duration_s: float, rate: int = 8000) -> None:
    """Synthesize a tiny silent mono 16-bit WAV of an exact duration (stdlib only)."""
    nframes = round(duration_s * rate)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(rate)
        wav_file.writeframes(b"\x00\x00" * nframes)


def _make_row(**overrides: object) -> TrackGrid:
    """A TrackGrid with sane defaults, overridable per test."""
    fields: dict[str, object] = {"path": Path("track.wav"), "bpm": 120.0, "downbeat": 0.0, "length": 10.0}
    fields.update(overrides)
    return TrackGrid(**fields)  # type: ignore[arg-type]  # kwargs are dynamically typed by design


# ── Track list ───────────────────────────────────────────────────────────────


def test_a_csv_track_list_reads_its_columns_and_ignores_the_rest(tmp_path: Path) -> None:
    """A spreadsheet's CSV, BOM and extra columns included: empty cells are absent values."""
    tracks = tmp_path / "tracks.csv"
    tracks.write_text(
        "\ufeffpath,bpm,downbeat,drop,start,length,rating\n"
        "D:/Music/a.mp3,174,0.1,44.2,0.1,300,5\n"
        "D:/Music/b.wav,128,0.5,,,,\n",
        encoding="utf-8",
    )
    first, second = read_track_list(tracks)
    assert first == TrackGrid(Path("D:/Music/a.mp3"), 174.0, 0.1, drop=44.2, start=0.1, length=300.0)
    assert second == TrackGrid(Path("D:/Music/b.wav"), 128.0, 0.5)


def test_a_json_track_list_is_a_list_of_objects(tmp_path: Path) -> None:
    tracks = tmp_path / "tracks.json"
    tracks.write_text(json.dumps([{"path": "a.wav", "bpm": 120, "downbeat": 1.5, "drop": None, "label": "x"}]))
    assert read_track_list(tracks) == [TrackGrid(Path("a.wav"), 120.0, 1.5)]


@pytest.mark.parametrize(
    ("name", "text", "error"),
    [
        ("tracks.csv", "path,bpm\na.wav,120\n", "tracks.csv, track 2: no downbeat"),
        ("tracks.csv", "path,bpm,downbeat\na.wav,fast,0\n", "bpm is 'fast', not a number"),
        ("tracks.csv", "path,bpm,downbeat\na.wav,0,0\n", "bpm is 0.0"),
        ("tracks.json", '{"path": "a.wav"}', "a list of objects"),
        ("tracks.tsv", "", "a .csv or .json file"),
    ],
)
def test_a_track_list_that_cannot_be_read_says_where(tmp_path: Path, name: str, text: str, error: str) -> None:
    tracks = tmp_path / name
    tracks.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=error):
        read_track_list(tracks)


# ── filter_rows ──────────────────────────────────────────────────────────────


def test_filter_rows_include_is_case_insensitive_and_ored() -> None:
    """Multiple --include substrings are OR'd together, case-insensitively."""
    rows = [
        _make_row(path=Path(r"\nas\Dnb Picks\a.mp3")),
        _make_row(path=Path(r"\nas\House Picks\b.mp3")),
        _make_row(path=Path(r"\nas\Techno Picks\c.mp3")),
    ]
    filtered = filter_rows(rows, includes=["dnb picks", "TECHNO"])
    assert {str(r.path) for r in filtered} == {r"\nas\Dnb Picks\a.mp3", r"\nas\Techno Picks\c.mp3"}


def test_filter_rows_limit_caps_the_count() -> None:
    rows = [_make_row(path=Path(name)) for name in ("a.wav", "b.wav", "c.wav")]
    assert [r.path.name for r in filter_rows(rows, limit=1)] == ["a.wav"]


# ── compute_anchor (bar-fold rule) ───────────────────────────────────────────


def test_compute_anchor_no_drop_uses_the_downbeat_in_the_first_bar() -> None:
    """With no drop cue, a downbeat in the first bar is the anchor."""
    result = compute_anchor(bpm=174.0, downbeat=0.5, drop=None)
    assert result.anchor_s == 0.5
    assert result.n_bars is None
    assert result.bar_phase_warning is False


def test_compute_anchor_no_drop_folds_a_later_downbeat_back() -> None:
    """Any downbeat will do: one 15 bars in (120 BPM, 2 s bars) folds back to the first bar."""
    assert compute_anchor(bpm=120.0, downbeat=30.25, drop=None).anchor_s == pytest.approx(0.25)


def test_compute_anchor_gravity_lands_on_beat_offset() -> None:
    """Real cue_finder numbers (Agbo - Gravity): in-phase drop, anchor == the downbeat."""
    bpm = 174.0
    downbeat = 6.89655172365633e-05
    drop = 44.138
    result = compute_anchor(bpm=bpm, downbeat=downbeat, drop=drop)
    assert result.anchor_s == pytest.approx(downbeat)
    assert result.n_bars == 32
    assert result.n_bars * 4 == 128  # matches cue_finder's tagged drop_beat
    assert result.bar_phase_warning is False
    assert result.residual_bars == pytest.approx(0.0, abs=1e-9)


def test_compute_anchor_caligo_flags_bar_phase_mismatch() -> None:
    """Real cue_finder numbers (Alix Perez & Monty - Caligo): 47.75-bar fold, flagged."""
    result = compute_anchor(bpm=174.0, downbeat=0.3119310344827608, drop=66.174, bar_tolerance=0.02)
    assert result.n_bars == 47
    assert result.anchor_s == pytest.approx(1.3464137931034514)
    assert result.anchor_s >= 0.0
    assert result.residual_bars == pytest.approx(-0.25, abs=1e-6)
    assert result.bar_phase_warning is True


def test_compute_anchor_respects_bar_tolerance() -> None:
    """A generous --bar-tolerance suppresses the warning for the same off-phase drop."""
    result = compute_anchor(bpm=174.0, downbeat=0.3119310344827608, drop=66.174, bar_tolerance=0.5)
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
    """Lossless sources keep their own extension no matter what --audio-format is."""
    assert audio_dest_extension(Path("a.wav"), "wav") == ".wav"
    assert audio_dest_extension(Path("a.wav"), "flac") == ".wav"
    assert audio_dest_extension(Path("a.aiff"), "wav") == ".aiff"
    assert audio_dest_extension(Path("a.flac"), "flac") == ".flac"
    assert audio_dest_extension(Path("a.flac"), "wav") == ".flac"


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


# ── library_relative_path ─────────────────────────────────────────────────────


def test_library_relative_path_extracts_suffix() -> None:
    """Everything below the "User Library" path component is returned."""
    path = Path("somewhere/Ableton/User Library/DJ Crates/Audio/tune.wav")
    assert library_relative_path(path) == Path("DJ Crates/Audio/tune.wav")


def test_library_relative_path_outside_library_never_resolves() -> None:
    """Outside any "User Library" a marker path keeps Live on the absolute Path."""
    result = library_relative_path(Path("elsewhere/tune.wav"))
    assert result == Path("abletoolz outside library/tune.wav")


# ── Duplicate-source dedupe ──────────────────────────────────────────────────


def test_run_crate_generation_dedupes_same_clip_name(tmp_path: Path) -> None:
    """Two rips of the same tune yield ONE clip: the most grid-reliable format wins.

    Lossless beats mp3 (no decoder offset at all), mp3 beats m4a (measured offset
    model vs unverified) -- and the loser is reported, never silently overwritten.
    """
    rows = [
        _make_row(path=Path("tunes/Tune.m4a")),
        _make_row(path=Path("tunes/Tune.mp3")),
        _make_row(path=Path("tunes/Other.wav")),
        _make_row(path=Path("tunes/Other.mp3")),
    ]
    report = run_crate_generation(
        rows,
        crate_name="Crate",
        audio_dir=tmp_path / "Audio",
        crates_dir=tmp_path,
        audio_mode="link",
        bar_tolerance=0.02,
        dry_run=True,
    )
    assert report.generated_count == 2
    assert [(s.row.path.suffix, k.row.path.suffix) for s, k in report.duplicate_skips] == [
        (".m4a", ".mp3"),
        (".mp3", ".wav"),
    ]


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

    row = _make_row(path=src_wav, bpm=120.0, downbeat=0.0, start=1.0, drop=8.0)

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

    row = _make_row(path=src_wav, bpm=120.0, downbeat=0.25)

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
    row = _make_row(path=src_wav, bpm=140.0, downbeat=0.0, drop=16.0)

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
    row = _make_row(path=Path("Some Tune - 9A - Energy 7.mp3"), bpm=174.0, downbeat=6.89655172365633e-05, drop=44.138)
    plan = build_plan(row, bar_tolerance=0.02)
    assert plan.clip_name == "Some Tune"
    assert plan.safe_name == "Some Tune"
    assert plan.anchor.anchor_s == pytest.approx(row.downbeat)
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
    *,
    tag: bytes = b"Info",
    signature: bytes = b"LAME3.99r",
    delay: int = 576,
    with_gapless: bool = True,
    rate: int = 44100,
) -> bytes:
    """Build a minimal Layer III stereo first frame with a Xing/Info header.

    ``tag`` picks the header kind ("Info" = Live plays the frame as audio, +1 frame;
    "Xing" = both decoders skip it). ``with_gapless`` controls whether the extension
    carries ``signature`` + a plausible delay; without it the region stays zeros.
    ``rate`` picks the MPEG version: 32 to 48 kHz is MPEG-1, 16 to 24 kHz MPEG-2, with
    576-sample frames and shorter side information.
    """
    mpeg1_rates, mpeg2_rates = (44100, 48000, 32000), (22050, 24000, 16000)
    mpeg1 = rate in mpeg1_rates
    index = (mpeg1_rates if mpeg1 else mpeg2_rates).index(rate)
    frame = bytearray(1044)
    frame[0:4] = bytes((0xFF, 0xFB if mpeg1 else 0xF3, 0x90 | index << 2, 0x00))  # Layer III, 128kbps, stereo
    xing_at = 4 + (32 if mpeg1 else 17)
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


def _id3(body: bytes) -> bytes:
    """An ID3v2.4 tag holding ``body``, its size written syncsafe."""
    size = len(body)
    syncsafe = bytes(((size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F))
    return b"ID3\x04\x00\x00" + syncsafe + body


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        pytest.param(_synthetic_mp3_bytes(tag=b"Info", signature=b"LAME3.97 "), 2257.0, id="Info, LAME (Gravity)"),
        pytest.param(_synthetic_mp3_bytes(tag=b"Info", signature=b"Lavf55.33"), 2257.0, id="Info, Lavf (Caligo)"),
        pytest.param(_synthetic_mp3_bytes(tag=b"Info", with_gapless=False), 1152.0, id="Info, no signature (repaired)"),
        pytest.param(_synthetic_mp3_bytes(tag=b"Xing", signature=b"Lavf56.4."), 1105.0, id="Xing, Lavf (Dune)"),
        pytest.param(_synthetic_mp3_bytes(tag=b"Xing", signature=b"LAME3.99r"), 1105.0, id="Xing, LAME"),
        pytest.param(bytes((0xFF, 0xFB, 0x90, 0x00)) + b"\x01" * 1040, 0.0, id="a real frame, no tag"),
        pytest.param(_synthetic_mp3_bytes(tag=b"Xing", delay=1024), 1024.0 + 529.0, id="a per-file trim"),
        # Measured in Live 12.4.25b1 on 2026-11-03: 2257 samples at 32, 44.1 and 48 kHz, 1681 at 22.05 and 24 kHz.
        pytest.param(_synthetic_mp3_bytes(rate=48000), 2257.0, id="48 kHz"),
        pytest.param(_synthetic_mp3_bytes(rate=32000), 2257.0, id="32 kHz"),
        pytest.param(_synthetic_mp3_bytes(rate=24000), 1681.0, id="24 kHz, MPEG-2: a 576-sample frame"),
        pytest.param(_synthetic_mp3_bytes(rate=22050), 1681.0, id="22.05 kHz, MPEG-2: a 576-sample frame"),
        pytest.param(_synthetic_mp3_bytes(rate=22050, tag=b"Xing"), 1105.0, id="MPEG-2 Xing"),
        pytest.param(_id3(b"\x00" * 200) + _synthetic_mp3_bytes(), 2257.0, id="ID3v2 skipped, not scanned"),
        # Album art bigger than any sane read window is seeked past by its declared size (the R.E.M bug).
        pytest.param(_id3(b"\xaa" * 500_000) + _synthetic_mp3_bytes(), 2257.0, id="a multi-MB ID3"),
        # An ID3 TSSE comment, say: Xing is skipped by both decoders, and no signature means no trim.
        pytest.param(
            _synthetic_mp3_bytes(tag=b"Xing", with_gapless=False) + b"LAME3.100 loose comment text",
            0.0,
            id="loose encoder strings",
        ),
    ],
)
def test_mp3_offset_classes(tmp_path: Path, data: bytes, expected: float) -> None:
    """Trim+surplus model classes, in samples at the file's own rate, read from its first frame."""
    path = _write_mp3(tmp_path, "tune.mp3", data)
    header = read_mp3_header(path)
    assert header.offset_samples == expected
    assert mp3_decoder_offset_seconds(path) == pytest.approx(expected / header.sample_rate)


def test_an_mp3_header_gives_its_rate_and_channels(tmp_path: Path) -> None:
    header = read_mp3_header(_write_mp3(tmp_path, "tune.mp3", _synthetic_mp3_bytes(rate=24000)))
    assert (header.sample_rate, header.channels) == (24000, 2)


def test_an_mp3_with_no_frame_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no MPEG Layer III frame"):
        read_mp3_header(_write_mp3(tmp_path, "tune.mp3", b"\x00" * 5000))


# ── Link mode audio placement ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "data", "offset_samples"),
    [("tune.mp3", _synthetic_mp3_bytes(), 2257.0), ("tune.wav", None, 0.0)],
    ids=["mp3", "wav"],
)
def test_resolve_link_placement_references_the_source_in_place(
    tmp_path: Path, name: str, data: bytes | None, offset_samples: float
) -> None:
    """The source is referenced where it lies, only an MP3's header read, and the clip runs its offset longer.

    Where the reference points in the written clip is the link-mode generation test's.
    """
    source = tmp_path / name
    if data is not None:
        source.write_bytes(data)
    audio_dir = tmp_path / "User Library" / "DJ Crates" / "Audio"
    placement = resolve_link_placement(_make_row(path=source, length=120.0), audio_dir=audio_dir, safe_name="tune")

    offset = offset_samples / 44100
    assert placement.absolute_path == source
    assert placement.time_offset_s == pytest.approx(offset)
    assert placement.duration_s == pytest.approx(120.0 + offset)
    assert placement.did_work is False
    assert not audio_dir.exists()


def test_run_crate_generation_link_mode_mp3_shifts_grid_by_offset(tmp_path: Path) -> None:
    """End-to-end link mode: no audio is copied, and every authored time is shifted."""
    user_library = tmp_path / "User Library"
    audio_dir = user_library / "DJ Crates" / "Audio"
    crates_dir = user_library / "DJ Crates"

    src_dir = tmp_path / "source"
    src_dir.mkdir()
    mp3_path = src_dir / "Test Tune - 9A - Energy 7.mp3"
    mp3_path.write_bytes(_synthetic_mp3_bytes())

    # bpm 120 -> bar_s 2.0s; drop at 8.0s is exactly 4 bars in, the downbeat at 0.0 is in-phase.
    row = _make_row(path=mp3_path, bpm=120.0, downbeat=0.0, start=0.0, drop=8.0)

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


def test_run_crate_generation_link_mode_default_audio_flag() -> None:
    """--audio defaults to "link" (no copy, no transcode)."""
    args = dj_crates.parse_arguments(["tracks.csv", "--crate", "Test"])
    assert args.audio == "link"


def test_resolve_output_dirs_explicit_paths_win() -> None:
    """Explicit dirs pass through untouched; audio defaults inside the crates dir."""
    crates, audio = dj_crates.resolve_output_dirs(Path("crates"), None, None)
    assert crates == Path("crates")
    assert audio == Path("crates") / "Audio"


def test_resolve_output_dirs_uses_user_library(tmp_path: Path) -> None:
    crates, audio = dj_crates.resolve_output_dirs(None, None, tmp_path)
    assert crates == tmp_path / "DJ Crates"
    assert audio == crates / "Audio"


def test_resolve_output_dirs_refuses_to_guess() -> None:
    """No User Library found and no --crates-dir: error out, never invent a path."""
    with pytest.raises(ValueError, match="--crates-dir"):
        dj_crates.resolve_output_dirs(None, None, None)


def test_mirror_requires_a_marker_value() -> None:
    """--mirror without a folder name is an error; there is no default marker."""
    with pytest.raises(SystemExit):
        dj_crates.parse_arguments(["tracks.csv", "--crate", "Test", "--mirror"])


def test_sanitize_filename_normalizes_mac_decomposed_accents() -> None:
    decomposed = "Chant-Me\u0301 Bro"
    precomposed = "Chant-M\u00e9 Bro"
    assert sanitize_filename(decomposed) == precomposed
    assert sanitize_filename(precomposed) == precomposed


# ── .asd sidecars ─────────────────────────────────────────────────────────────


def _crate_with_asd(tmp_path: Path, row: TrackGrid, *, dry_run: bool = False) -> RunReport:
    crates_dir = tmp_path / "User Library" / "DJ Crates"
    return run_crate_generation(
        [row],
        crate_name="Crate",
        audio_dir=crates_dir / "Audio",
        crates_dir=crates_dir,
        audio_mode="link",
        bar_tolerance=0.02,
        dry_run=dry_run,
        make=[Output.ASD],
    )


def _wav_row(tmp_path: Path) -> TrackGrid:
    audio = tmp_path / "Tune.wav"
    _write_test_wav(audio, duration_s=10.0)
    # bpm 120: bars are 2 s, so a drop at 8.25 s folds back to an anchor at 0.25 s.
    return _make_row(path=audio, bpm=120.0, downbeat=0.25, drop=8.25)


def test_asd_carries_the_full_clip_grid_beside_a_wav(tmp_path: Path) -> None:
    report = _crate_with_asd(tmp_path, _wav_row(tmp_path))
    assert [r.asd for r in report.results] == [AsdOutcome.WRITTEN]
    asd = AsdFile.load(tmp_path / "Tune.wav.asd")
    assert asd.loads_clip_defaults
    anchor, end = asd.markers[:2]
    assert (anchor.sec_time, anchor.beat_time) == (pytest.approx(0.25), 0.0)
    assert (end.sec_time, end.beat_time) == (pytest.approx(10.0), pytest.approx((10.0 - 0.25) * 2))


def test_asd_beside_an_mp3_is_in_live_s_timeline(tmp_path: Path) -> None:
    audio = _write_mp3(tmp_path, "Tune.mp3", _synthetic_mp3_bytes() + bytes(4096))
    row = _make_row(path=audio, bpm=120.0, downbeat=0.25, drop=8.25)
    _crate_with_asd(tmp_path, row)
    anchor = AsdFile.load(tmp_path / "Tune.mp3.asd").markers[0]
    assert anchor.sec_time == pytest.approx(0.25 + 2257 / 44100)


def test_asd_with_settings_saved_in_live_for_another_grid_is_kept(tmp_path: Path) -> None:
    row = _wav_row(tmp_path)
    sidecar = tmp_path / "Tune.wav.asd"
    write_grid(sidecar, bpm=128.0, anchor_seconds=0.5, audio_path=row.path)
    before = sidecar.read_bytes()
    report = _crate_with_asd(tmp_path, row)
    assert [r.asd for r in report.results] == [AsdOutcome.KEPT]
    assert sidecar.read_bytes() == before


def test_asd_already_holding_this_grid_is_written_again(tmp_path: Path) -> None:
    row = _wav_row(tmp_path)
    _crate_with_asd(tmp_path, row)
    report = _crate_with_asd(tmp_path, row)
    assert [r.asd for r in report.results] == [AsdOutcome.WRITTEN]


def test_asd_dry_run_writes_no_sidecar(tmp_path: Path) -> None:
    report = _crate_with_asd(tmp_path, _wav_row(tmp_path), dry_run=True)
    assert len(report.asd_results(AsdOutcome.WRITTEN)) == 1
    assert not (tmp_path / "Tune.wav.asd").exists()


def test_asd_alone_makes_no_crate(tmp_path: Path) -> None:
    report = _crate_with_asd(tmp_path, _wav_row(tmp_path))
    assert report.generated_count == 0
    assert not (tmp_path / "User Library").exists()


def test_make_names_the_outputs_and_alc_needs_a_crate() -> None:
    assert dj_crates.parse_arguments(["tracks.csv", "--make", "asd"]).make == [Output.ASD]
    both = dj_crates.parse_arguments(["tracks.csv", "--make", "alc", "asd", "--crate", "DnB"])
    assert both.make == [Output.ALC, Output.ASD]
    with pytest.raises(SystemExit):
        dj_crates.parse_arguments(["tracks.csv", "--make", "alc"])
