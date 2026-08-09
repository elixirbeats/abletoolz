"""Tests for the .asd parser/writer against Live-generated ground-truth fixtures.

Fixture facts asserted here were established by the byte-exact structural analysis in
abletoolz/asd/FORMAT.md (offsets refer to that document).
"""

import struct
import wave
from pathlib import Path

import pytest

from abletoolz.asd.parser import AsdFile, ListValue, Obj, PrimArray, WarpMarker
from abletoolz.asd.writer import write_grid

FIXTURES = Path(__file__).parent / "asd_fixtures"
NO_WARP = FIXTURES / "no_warp_markers.asd"
WITH_WARP = FIXTURES / "with_warp_markers.asd"
PINE_LEGACY = FIXTURES / "pine-021.wav.asd"

ALL_FIXTURES = [NO_WARP, WITH_WARP, PINE_LEGACY]


# ── Round trip ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ALL_FIXTURES, ids=lambda p: p.name)
def test_round_trip_byte_identity(path: Path) -> None:
    """load() -> to_bytes() reproduces every fixture byte-for-byte."""
    original = path.read_bytes()
    assert AsdFile.load(path).to_bytes() == original


# ── Parsed-field ground truth (documented offsets in FORMAT.md) ────────────


def test_no_warp_fixture_fields() -> None:
    """Live-12 MP3 fixture without auto-warp analysis."""
    asd = AsdFile.load(NO_WARP)
    assert len(asd.documents) == 2
    assert asd.documents[0].root.cls == "SampleData"
    assert asd.documents[1].root.cls == "AufTaktData"
    assert len(asd.lead_table) == 11980

    assert asd.is_warped is True  # data offset 0xC617
    assert asd.loop_on is True  # 0xC66B
    assert asd.warp_mode == 3  # 0xC628
    assert asd.extra_length == 526  # 0xF377
    assert asd.original_file_size == 10_794_273  # 0xF37B
    assert asd.markers == []  # empty list at 0xC660

    sd = asd.sample_data()
    onsets = sd.values["OnSets"]
    assert isinstance(onsets, Obj)
    positions = onsets.values["Positions"]
    assert isinstance(positions, PrimArray)
    assert positions.count == 1435  # 0xC670
    auftakt = sd.values["AufTaktData"]
    assert isinstance(auftakt, Obj)
    assert auftakt.values["IsSet"] == 0  # unset sentinel pattern
    overview = sd.values["OverView"]
    assert isinstance(overview, Obj)
    levels = overview.values["OverViewLevels"]
    assert isinstance(levels, ListValue)
    level_counts: list[int] = []
    for elem in levels.elems:
        bins = elem.values["InterleavedBinData"]
        assert isinstance(bins, PrimArray)
        level_counts.append(bins.count)
    assert level_counts == [365060, 2856, 24, 4]


def test_with_warp_fixture_differs_only_in_auftakt() -> None:
    """Auto-warped fixture: markers still empty; grid data lives in AufTaktData."""
    asd = AsdFile.load(WITH_WARP)
    assert asd.markers == []  # Live 12 auto-warp writes NO markers into .asd
    assert asd.is_warped is True
    sd = asd.sample_data()
    auftakt = sd.values["AufTaktData"]
    assert isinstance(auftakt, Obj)
    assert auftakt.values["IsSet"] == 1
    chunk = auftakt.values["PreprocessedDataChunk"]
    assert isinstance(chunk, PrimArray)
    assert chunk.count == 317_904  # first differing byte vs no_warp is this count at 0xF366
    assert asd.documents[1].root.cls == "AufTaktData"


def test_legacy_fixture_fields() -> None:
    """Older-Live WAV fixture (40-field schema, no AufTaktData document)."""
    asd = AsdFile.load(PINE_LEGACY)
    assert len(asd.documents) == 1
    assert asd.extra_length == 0  # WAV source
    assert asd.original_file_size == 80_996
    assert asd.markers == []
    schema_names = [cd.name for cd in asd.documents[0].schema]
    assert "BeatTrackState" in schema_names  # legacy-only class


# ── Grid writing ───────────────────────────────────────────────────────────


def test_set_grid_rewrites_markers_and_flags(tmp_path: Path) -> None:
    """set_grid on a Live-generated file: markers + flags written, analysis preserved."""
    work = tmp_path / "track.mp3.asd"
    work.write_bytes(WITH_WARP.read_bytes())
    asd = AsdFile.load(work)
    asd.set_grid(bpm=174.0, anchor_seconds=0.532, duration_seconds=264.9)
    asd.save(backup=False)

    out = AsdFile.load(work)
    markers = out.markers
    assert len(markers) == 2
    assert markers[0].sec_time == 0.532
    assert markers[0].beat_time == 0.0
    assert markers[1].sec_time == 264.9
    assert markers[1].beat_time == pytest.approx((264.9 - 0.532) * 174.0 / 60.0)
    assert out.is_warped is True
    markers_generated = out.sample_data().values["MarkersGenerated"]
    assert isinstance(markers_generated, Obj)
    assert markers_generated.values["Value"] == 1

    # WarpMarker class def registered in the schema table
    doc = out.sample_data_doc()
    wm = doc.class_def("WarpMarker")
    assert wm.field_count == 2
    assert wm.fields == [("SecTime", 0x17), ("BeatTime", 0x17)]

    # AufTaktData reset to the unset sentinel in both documents
    auftakt = out.sample_data().values["AufTaktData"]
    assert isinstance(auftakt, Obj)
    assert auftakt.values["IsSet"] == 0
    assert out.documents[1].root.values["IsSet"] == 0

    # Analysis sections preserved verbatim
    src = AsdFile.load(WITH_WARP)
    assert out.lead_table == src.lead_table
    onsets_out = out.sample_data().values["OnSets"]
    onsets_src = src.sample_data().values["OnSets"]
    assert onsets_out == onsets_src
    assert out.sample_data().values["OverView"] == src.sample_data().values["OverView"]


def test_marker_wire_format_matches_prior_reverse_engineering(tmp_path: Path) -> None:
    """Byte layout of written markers: count, 32-byte tagged records, terminator."""
    work = tmp_path / "track.mp3.asd"
    work.write_bytes(NO_WARP.read_bytes())
    asd = AsdFile.load(work)
    asd.set_grid(bpm=170.0, anchor_seconds=1.5)
    data = asd.to_bytes()

    tag = b"\x00\x0aWarpMarker"
    hits = [i for i in range(len(data)) if data.startswith(tag, i)]
    assert len(hits) == 3  # 1 schema def + 2 list elements
    first_record = hits[1]
    # u32 marker count immediately before the first record
    assert struct.unpack_from("<I", data, first_record - 4)[0] == 2
    # records are 32 bytes apart; payload = u32 index + f64 SecTime + f64 BeatTime
    assert hits[2] - first_record == 32
    idx0, sec0, beat0 = struct.unpack_from("<Idd", data, first_record + len(tag))
    assert (idx0, sec0, beat0) == (0, 1.5, 0.0)
    idx1, sec1, beat1 = struct.unpack_from("<Idd", data, hits[2] + len(tag))
    assert idx1 == 1
    assert sec1 == pytest.approx(1.5 + 240.0 / 170.0)
    assert beat1 == pytest.approx(4.0)  # one bar
    # list terminator after the last record
    assert data[hits[2] + 32 : hits[2] + 34] == b"\x00\x00"
    # schema def: the "decoy": tag followed by field count 2
    assert struct.unpack_from("<I", data, hits[0] + len(tag))[0] == 2


def test_set_grid_on_legacy_schema(tmp_path: Path) -> None:
    """Grid writing works on the old (pine) schema too."""
    work = tmp_path / "pine.wav.asd"
    work.write_bytes(PINE_LEGACY.read_bytes())
    asd = AsdFile.load(work)
    asd.set_grid(bpm=120.0, anchor_seconds=0.0)
    asd.save(backup=False)
    out = AsdFile.load(work)
    assert len(out.markers) == 2
    assert out.is_warped is True


def test_clean_legacy_api(tmp_path: Path) -> None:
    """The pre-existing public API (load/markers/clean/save) still works."""
    work = tmp_path / "track.mp3.asd"
    work.write_bytes(NO_WARP.read_bytes())
    asd = AsdFile.load(work)
    asd.clean(2.25)
    asd.save()  # default backup=True
    assert (tmp_path / "track.mp3.asd.bak").exists()
    out = AsdFile.load(work)
    assert out.markers == [WarpMarker(0, 2.25, 0.0)]


# ── Cold synthesis ─────────────────────────────────────────────────────────


def _make_test_wav(path: Path, seconds: float = 2.0, rate: int = 44100) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00\x00\x00" * int(seconds * rate))


def test_write_grid_cold_synthesis(tmp_path: Path) -> None:
    """write_grid on a file Live never analyzed produces a parseable, correct .asd."""
    audio = tmp_path / "tune.wav"
    _make_test_wav(audio, seconds=2.0)
    asd_path = tmp_path / "tune.wav.asd"
    write_grid(asd_path, bpm=174.0, anchor_seconds=0.5, audio_path=audio)

    out = AsdFile.load(asd_path)
    assert out.original_file_size == audio.stat().st_size
    assert out.extra_length == 0
    assert out.is_warped is True
    markers = out.markers
    assert len(markers) == 2
    assert markers[0] == WarpMarker(0, 0.5, 0.0)
    assert markers[1].sec_time == pytest.approx(2.0)  # WAV duration
    assert markers[1].beat_time == pytest.approx(1.5 * 174.0 / 60.0)
    assert len(out.documents) == 2
    assert out.documents[1].root.cls == "AufTaktData"
    # structure mirrors the Live-12 fixtures
    schema_names = [cd.name for cd in out.documents[0].schema]
    assert schema_names[0] == "SampleData"
    assert "WarpMarker" in schema_names


def test_write_grid_rewrite_existing(tmp_path: Path) -> None:
    """write_grid on an existing .asd goes through the rewrite path."""
    work = tmp_path / "track.mp3.asd"
    work.write_bytes(NO_WARP.read_bytes())
    write_grid(work, bpm=174.0, anchor_seconds=0.532)
    out = AsdFile.load(work)
    assert len(out.markers) == 2
    assert out.original_file_size == 10_794_273  # untouched


def test_write_grid_cold_requires_audio(tmp_path: Path) -> None:
    """Cold synthesis without the audio file is refused."""
    with pytest.raises(ValueError, match="audio_path"):
        write_grid(tmp_path / "missing.wav.asd", bpm=174.0, anchor_seconds=0.5)
