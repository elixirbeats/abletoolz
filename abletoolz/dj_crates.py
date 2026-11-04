r"""abletoolz.dj_crates -- make tracks open in Live already warped and on the bar.

Reads a track list (:func:`read_track_list`: a CSV or JSON of each track's audio
file, tempo and a downbeat, optionally its drop, start and length, from any
analyzer) and makes what ``--make`` names:

* ``alc`` -- a folder of Ableton Live Clip files inside the user's Ableton User
  Library, two per track: ``(full)`` (starts at the start cue) and ``(drop)``
  (starts at the drop cue, skipped when the track has no drop).
* ``asd`` -- a sidecar beside each track's audio (:func:`write_track_asd`), the
  full clip's grid, so the file opens warped however it reaches Live.

Grid convention (per :mod:`abletoolz.alc`): beat 0 (1.1.1) is the *anchor*, computed
here by folding the drop back by whole bars until it sits as close to the track start
as possible (:func:`compute_anchor`) -- not simply the tagged first beat, and not the
drop itself. Live has been verified to honor a spliced ``.alc`` grid verbatim on
drag-in from the browser.

Audio strategy (``--audio {link,wav,flac}``, default ``link``):

* ``link`` -- no copy, no transcode. The clip's ``SampleRef`` points straight at the
  source file (absolute path, possibly UNC), with a deliberately non-resolving
  ``RelativePath`` so Live falls back to the absolute path (mirrors how Live's own
  sets reference cross-volume files -- see the ``RelativePathType=1`` examples in
  ``test/sample_missing_fix Project/11.2.10_abs_rel.xml``). MP3 sources need every
  authored time shifted by Live's decoder offset (:func:`read_mp3_header`, the
  model above :data:`MPEG_DECODER_DELAY_SAMPLES`), counted in samples and converted
  with each file's own sample rate. Lossless sources need no shift (their decode is
  deterministic) and are referenced in place.
* ``wav`` / ``flac`` -- MP3 (and other compressed) sources are transcoded via ffmpeg
  into a shared cache under ``--audio-dir`` (Live's MP3 decoder is offset from
  ffmpeg's, which would otherwise shift the whole grid); lossless sources are copied
  unchanged. Idempotent -- an existing cache entry is left alone. This is the
  fallback for self-contained crates (no cross-volume/UNC references).

CLI usage::

    abletoolz-dj-crates tracks.csv --make alc asd --crate "Dnb Picks" --dry-run
"""

from __future__ import annotations

import argparse
import csv
import enum
import json
import logging
import re
import shutil
import subprocess
import sys
import unicodedata
import wave
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Literal

from abletoolz.alc import AlcClip, AlcError
from abletoolz.ansi import C, R, Y
from abletoolz.formats.asd import AsdFile, write_grid
from abletoolz.machine import Locations

logger = logging.getLogger(__name__)


class AudioMode(enum.StrEnum):
    """How source audio is placed into a generated crate."""

    LINK = "link"
    WAV = "wav"
    FLAC = "flac"


# The transcode-cache modes are a strict subset of AudioMode (LINK never gets cached).
type CacheFormat = Literal[AudioMode.WAV, AudioMode.FLAC]


class FfmpegCodec(enum.StrEnum):
    """ffmpeg encoder names for the transcode-cache formats."""

    PCM_S16LE = "pcm_s16le"
    FLAC = "flac"


_CACHE_CODECS: dict[CacheFormat, FfmpegCodec] = {
    AudioMode.WAV: FfmpegCodec.PCM_S16LE,
    AudioMode.FLAC: FfmpegCodec.FLAC,
}

# Lossless sources decode with no offset: linked or cached, they are used as they are.
_LOSSLESS_EXTENSIONS = {".wav", ".aiff", ".aif", ".flac"}
_DEFAULT_BAR_TOLERANCE = 0.02
# Headroom to the right of the last cue in a generated clip's opening view:
# enough of the following section to see where it goes, without zooming out so
# far that the bar lines at the cue stop being readable.
_VIEW_MARGIN_BARS = 16.0

_TEMPLATE_PACKAGE = "abletoolz.data"
_TEMPLATE_RESOURCE = "clip_template.alc"

# A downbeat this close before time zero counts as being AT zero, rather than
# stepping the anchor forward a whole bar. Mirrors cue_finder's ANCHOR_SNAP_S;
# both must agree or this module re-derives the anchor the analyser rejected.
_ANCHOR_SNAP_S = 0.025

_INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')
# "Tune - 9A - Energy 7" -> "Tune"; "Tune - 9A" -> "Tune". Camelot keys are 1-12 + A/B.
_CAMELOT_ENERGY_SUFFIX = re.compile(r"^(?P<name>.+?) - \d{1,2}[AB] - Energy \d+$")
_CAMELOT_SUFFIX = re.compile(r"^(?P<name>.+?) - \d{1,2}[AB]$")


# ── Track list ───────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class TrackGrid:
    """One track's grid, as a track list gives it: seconds as FFmpeg decodes the file (gapless MP3s trimmed).

    ``downbeat`` is any downbeat, the first beat of a bar; ``drop`` a cue on one, which decides the bar phase when
    given. ``start`` is where the full clip begins, the grid's first downbeat when absent; ``length`` is the track's
    length, read from the file when absent.
    """

    path: Path
    bpm: float
    downbeat: float
    drop: float | None = None
    start: float | None = None
    length: float | None = None


TRACK_FIELDS = ("path", "bpm", "downbeat", "drop", "start", "length")
_REQUIRED_FIELDS = ("path", "bpm", "downbeat")


def _seconds(value: object, field: str, where: str) -> float | None:
    """A number in a track list, None when the cell is empty."""
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{where}: {field} is {value!r}, not a number")
    try:
        return float(value)
    except ValueError:
        raise ValueError(f"{where}: {field} is {value!r}, not a number") from None


def track_from_record(record: Mapping[str, object], where: str) -> TrackGrid:
    """One track from a track list's row or object; ``where`` names it in errors."""
    missing = [name for name in _REQUIRED_FIELDS if record.get(name) in (None, "")]
    if missing:
        raise ValueError(f"{where}: no {', '.join(missing)}")
    bpm, downbeat = _seconds(record["bpm"], "bpm", where), _seconds(record["downbeat"], "downbeat", where)
    assert bpm is not None and downbeat is not None
    if bpm <= 0:
        raise ValueError(f"{where}: bpm is {bpm}")
    return TrackGrid(
        path=Path(str(record["path"])),
        bpm=bpm,
        downbeat=downbeat,
        drop=_seconds(record.get("drop"), "drop", where),
        start=_seconds(record.get("start"), "start", where),
        length=_seconds(record.get("length"), "length", where),
    )


def read_track_list(path: Path) -> list[TrackGrid]:
    """A track list: a CSV with a header row, or a JSON list of objects, holding :data:`TRACK_FIELDS`.

    Any other columns or keys are ignored, so a tool can write its own beside them.
    """
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:  # -sig: spreadsheets often write a BOM
            records: list[Mapping[str, object]] = list(csv.DictReader(handle))
        first = 2  # the header is row 1
    elif suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise ValueError(f"{path.name}: a JSON track list is a list of objects")
        records, first = data, 1
    else:
        raise ValueError(f"{path.name}: a track list is a .csv or .json file")
    return [track_from_record(record, f"{path.name}, track {n}") for n, record in enumerate(records, first)]


def filter_rows(
    rows: Sequence[TrackGrid],
    *,
    includes: Sequence[str] = (),
    limit: int | None = None,
) -> list[TrackGrid]:
    """Tracks whose path holds one of ``includes`` (any case), at most ``limit`` of them."""
    filtered: Sequence[TrackGrid] = rows
    if includes:
        lowered = [s.lower() for s in includes]
        filtered = [row for row in filtered if any(s in str(row.path).lower() for s in lowered)]
    if limit is not None:
        filtered = filtered[:limit]
    return list(filtered)


# ── Grid anchor (bar-fold rule) ──────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class AnchorResult:
    """Where a track's grid anchor (beat 0) lands, per the bar-fold rule.

    ``residual_bars`` is the signed distance (in bars) between the drop and the
    nearest whole-bar multiple back from it, ignoring the "don't go negative" fold
    adjustment -- it flags when a track's downbeat, drop and tempo disagree about bar
    phase (e.g. a downbeat 0.25 bars out of phase with the drop).
    """

    anchor_s: float
    n_bars: int | None
    residual_bars: float
    bar_phase_warning: bool


def compute_anchor(
    *, bpm: float, downbeat: float, drop: float | None, bar_tolerance: float = _DEFAULT_BAR_TOLERANCE
) -> AnchorResult:
    """Compute the grid anchor (beat 0) for one track.

    With a drop: the anchor is the drop folded back by whole bars (``round``, not
    ``floor`` -- floor loses whole bars to float noise) so it lands on the same bar
    phase as the drop, as close to the track start as possible without going
    negative. Without a drop: the downbeat folded back the same way.

    It works in the track list's own time. A per-track offset (the MP3 decoder
    correction, :func:`mp3_decoder_offset_seconds`) is applied later, at clip
    authoring, and shifts the downbeat and the drop alike, so ``n_bars`` and
    ``residual_bars`` are unchanged by it.
    """
    bar_s = 240.0 / bpm
    if drop is None:
        anchor_s = downbeat % bar_s
        if bar_s - anchor_s <= _ANCHOR_SNAP_S:
            anchor_s = 0.0
        return AnchorResult(anchor_s=anchor_s, n_bars=None, residual_bars=0.0, bar_phase_warning=False)

    drop_s = drop
    raw_bars = (drop_s - downbeat) / bar_s
    n_bars = round(raw_bars)
    anchor_s = drop_s - n_bars * bar_s
    if anchor_s < 0.0:
        # The nearest downbeat is before the file starts. Stepping forward a
        # whole bar puts 1.1.1 a bar after the music when the drop sits a hair
        # under whole bars (597 library tracks are within a millisecond of it),
        # so a negligible shortfall snaps to zero; only mid-bar steps forward.
        if -anchor_s <= _ANCHOR_SNAP_S:
            anchor_s = 0.0
        else:
            n_bars -= 1
            anchor_s = drop_s - n_bars * bar_s
    residual_bars = raw_bars - round(raw_bars)
    return AnchorResult(
        anchor_s=anchor_s,
        n_bars=n_bars,
        residual_bars=residual_bars,
        bar_phase_warning=abs(residual_bars) > bar_tolerance,
    )


# ── Clip naming ──────────────────────────────────────────────────────────────


def derive_clip_name(filename: str) -> str:
    """Derive a human-readable clip name from a track's filename.

    Strips a trailing " - <camelot key> - Energy <n>" or " - <camelot key>" suffix
    when trivially matchable (e.g. "Tune - 9A - Energy 7" -> "Tune"); otherwise
    returns the filename stem unchanged.
    """
    stem = Path(filename).stem
    for pattern in (_CAMELOT_ENERGY_SUFFIX, _CAMELOT_SUFFIX):
        match = pattern.match(stem)
        if match:
            return match.group("name")
    return stem


def sanitize_filename(name: str) -> str:
    """Replace filesystem-unsafe characters in a name meant to become a filename.

    NFC-normalized first: a source file named on a Mac arrives decomposed
    ("e" + combining accent), and a clip written under those bytes collides
    with its precomposed twin in every sync layer that compares normalized
    names -- one tune, two spellings, permanent conflict.
    """
    return _INVALID_FILENAME_CHARS.sub("_", unicodedata.normalize("NFC", name)).strip()


# ── MP3 decoder offset (link mode) ───────────────────────────────────────────

# MODEL, ground-truth verified via synthetic golden files with a known drop
# position plus five real-track WAV<->MP3 onset pairings, all sample-exact.
# Live-vs-ffmpeg offset = trim + surplus:
#   trim:    ffmpeg honors the gapless block (header delay + 529 samples) when the
#            extension carries a valid LAME/Lavf/Lavc signature; Live never trims.
#   surplus: Live decodes an "Info"-tagged metadata frame as audio (+1 frame: 1152
#            samples, or 576 for the MPEG-2 and 2.5 files below 32 kHz);
#            both decoders skip a "Xing"-tagged frame.
# Measured: Info+sig d576 -> 2257 (golden + Gravity/Caligo/HandGestures/R.E.M);
# Xing+sig d576 -> 1105 (Dune); real frame, no tag -> 0 (golden). The Info-without-
# sig class (predicts 1152) is what cue_finder's Mixxx info-frame repair produces --
# golden-verifiable the same way. Every term is a sample count, so the offset converts
# to seconds with each file's own sample rate. Measured in Live 12.4.25b1 (2026-11-03)
# against WAVs of the same click: 2257 samples at 32, 44.1 and 48 kHz, 1681 at the
# MPEG-2 rates 22.05 and 24 kHz, each within half a sample.
MPEG_DECODER_DELAY_SAMPLES = 529
MPEG1_FRAME_SAMPLES = 1152
MPEG2_FRAME_SAMPLES = 576
# Sample rates by MPEG version (the frame header's version bits) and rate index.
_MPEG_SAMPLE_RATES = {0x03: (44100, 48000, 32000), 0x02: (22050, 24000, 16000), 0x00: (11025, 12000, 8000)}


# A plausible gapless-block delay; the upper bound rejects 0xFFF garbage (the
# block is absent or unusable). Zero is a real delay. Measured on an MP3
# ("Phlegmatic Dogs - Cuatrocats (Volac Remix)") whose LAME extension declares
# 0: the stream holds 10070 frames = 11600640 samples and ffmpeg decodes
# 11598959, discarding exactly 1681 = 1152 + 529. ffmpeg applies the 529 decoder
# delay whenever the tag is present, whatever the declared value, which is what
# the offset model does too (31 library files in this class).
_GAPLESS_DELAY_RANGE = range(0, 4096)

_GAPLESS_SIGNATURES = (b"LAME", b"Lavf", b"Lavc")


def _first_layer3_frame(path: Path) -> tuple[bytes, int | None]:
    """The bytes after an MP3's ID3v2 block, and where its first Layer III frame header starts there, if anywhere.

    Seeks past the ID3v2 block by its declared size: embedded album art can be
    multiple MB, so a fixed read window would miss the first frame.
    """
    with path.open("rb") as fh:
        head = fh.read(10)
        if head[:3] == b"ID3" and len(head) == 10:
            size = (head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]
            fh.seek(10 + size + (10 if head[5] & 0x10 else 0))
            data = fh.read(65536)
        else:
            data = head + fh.read(65536)
    for pos in range(len(data) - 3):
        b0, b1 = data[pos], data[pos + 1]
        if b0 == 0xFF and (b1 & 0xE0) == 0xE0 and (b1 >> 3) & 0x03 != 0x01 and (b1 >> 1) & 0x03 == 0x01:
            return data, pos  # Layer III frame header, MPEG-1, 2 or 2.5
    return data, None


@dataclass(frozen=True, slots=True)
class Mp3Header:
    """What an MP3's first frame says: Live's decode offset in samples, the sample rate and the channel count."""

    offset_samples: int
    sample_rate: int
    channels: int


def read_mp3_header(path: Path) -> Mp3Header:
    """Read the first MPEG frame's header, tag and gapless extension at their structural offsets.

    Loose encoder strings elsewhere (e.g. an ID3 TSSE "LAME ..." text frame) are
    irrelevant. See the model comment above. A file with no Layer III frame is refused.
    """
    data, pos = _first_layer3_frame(path)
    if pos is None:
        raise ValueError(f"{path.name}: no MPEG Layer III frame found")
    b1, b2, b3 = data[pos + 1], data[pos + 2], data[pos + 3]
    version, rate_index = (b1 >> 3) & 0x03, (b2 >> 2) & 0x03
    if rate_index == 0x03:
        raise ValueError(f"{path.name}: its first frame names no sample rate")
    sample_rate = _MPEG_SAMPLE_RATES[version][rate_index]
    mono = (b3 >> 6) == 0x03
    mpeg1 = version == 0x03
    channels = 1 if mono else 2
    xing_at = pos + 4 + ((17 if mono else 32) if mpeg1 else (9 if mono else 17))  # the side information
    tag = data[xing_at : xing_at + 4]
    if tag not in (b"Xing", b"Info"):
        return Mp3Header(0, sample_rate, channels)  # real audio frame first: both decoders start identically
    flags = int.from_bytes(data[xing_at + 4 : xing_at + 8], "big")
    ext = xing_at + 8
    for bit, width in ((0x01, 4), (0x02, 4), (0x04, 100), (0x08, 4)):
        if flags & bit:
            ext += width
    trim = 0
    if ext + 24 <= len(data) and data[ext : ext + 4] in _GAPLESS_SIGNATURES:
        delay = (data[ext + 21] << 4) | (data[ext + 22] >> 4)
        if delay in _GAPLESS_DELAY_RANGE:
            trim = delay + MPEG_DECODER_DELAY_SAMPLES
    surplus = (MPEG1_FRAME_SAMPLES if mpeg1 else MPEG2_FRAME_SAMPLES) if tag == b"Info" else 0
    return Mp3Header(trim + surplus, sample_rate, channels)


def mp3_decode_offset_samples(path: Path) -> int:
    """Samples to add to FFmpeg-decode times to match Live's decode of this MP3."""
    return read_mp3_header(path).offset_samples


def mp3_decoder_offset_seconds(path: Path) -> float:
    """Seconds to add to FFmpeg-decode times to match Live's decode of this MP3, at its own sample rate."""
    header = read_mp3_header(path)
    return header.offset_samples / header.sample_rate


# ── Audio cache (transcode / copy, idempotent -- ``wav``/``flac`` modes) ─────


def audio_dest_extension(src: Path, audio_format: CacheFormat) -> str:
    """The cached audio file's extension.

    Lossless sources are copied unchanged and keep their own extension.
    Only compressed sources (MP3, ...) get transcoded, to ``audio_format``.
    """
    suffix = src.suffix.lower()
    return suffix if suffix in _LOSSLESS_EXTENSIONS else f".{audio_format}"


def _ffmpeg_transcode(src: Path, dest: Path, audio_format: CacheFormat) -> None:
    """Transcode ``src`` to ``dest`` via ffmpeg. Isolated so tests can mock it out."""
    codec = _CACHE_CODECS[audio_format]
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-c:a", codec, str(dest)],
        check=True,
    )


def ensure_audio_cached(src: Path, dest: Path, audio_format: CacheFormat) -> bool:
    """Ensure ``dest`` holds a decoder-deterministic copy of ``src``.

    Compressed sources are transcoded via ffmpeg, lossless ones copied unchanged (see
    the module docstring). Idempotent: a pre-existing ``dest`` is left alone.

    Returns:
        True if a copy or transcode ran; False on a cache hit.
    """
    if dest.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() in _LOSSLESS_EXTENSIONS:
        shutil.copy2(src, dest)
    else:
        _ffmpeg_transcode(src, dest, audio_format)
    return True


def read_duration_seconds(path: Path) -> float:
    """Read the exact duration of an audio file already on disk.

    WAV uses the stdlib ``wave`` module (frame count / rate -- exact, no extra
    dependency). Other formats (FLAC, AIFF, MP3, ...) shell out to ``ffprobe``.
    """
    if path.suffix.lower() == ".wav":
        with wave.open(str(path), "rb") as wav_file:
            return wav_file.getnframes() / float(wav_file.getframerate())
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


def read_channel_count(path: Path) -> int:
    """How many channels an audio file has: WAV through ``wave``, an MP3 from its first frame, others via ffprobe."""
    suffix = path.suffix.lower()
    if suffix == ".wav":
        with wave.open(str(path), "rb") as wav_file:
            return wav_file.getnchannels()
    if suffix == ".mp3":
        return read_mp3_header(path).channels
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=channels",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(result.stdout.strip())


# ── Library-relative sample paths (RelativePathType 6) ───────────────────────


def library_relative_path(path: Path) -> Path:
    """RelativePath value (``RelativePathType`` 6: User Library-relative) for ``path``.

    Everything below the "User Library" component when there is one. Crates built
    outside a Library have no honest type-6 value, so return a marker path that can
    never resolve there -- Live falls through to the absolute ``Path`` field, the
    same fallback link mode relies on by design. Neither path needs to exist.
    """
    parts = path.parts
    if "User Library" in parts:
        return Path(*parts[parts.index("User Library") + 1 :])
    return Path("abletoolz outside library") / path.name


# ── Audio placement (where the clip's audio lives, per --audio mode) ────────


@dataclass(slots=True, frozen=True)
class AudioPlacement:
    """Where a track's clip audio lives, and how cue-times must shift to match it."""

    absolute_path: Path
    relative_path: Path
    duration_s: float
    time_offset_s: float
    did_work: bool


def _fake_relative_path(audio_dir: Path, safe_name: str, ext: str) -> Path:
    """A deliberately non-resolving RelativePath so Live falls back to the absolute Path.

    Shaped like a wav/flac-mode cache path; link mode never writes there.
    """
    return library_relative_path(audio_dir / f"{safe_name}{ext}")


def resolve_link_placement(row: TrackGrid, *, audio_dir: Path, safe_name: str) -> AudioPlacement:
    """Resolve a track's audio placement for ``--audio link`` (no copy, no transcode).

    An MP3's offset comes from its first frame header; the length from the track list, else from the file.
    """
    src = row.path
    suffix = src.suffix.lower()
    offset = mp3_decoder_offset_seconds(src) if suffix == ".mp3" else 0.0
    length = row.length if row.length is not None else read_duration_seconds(src)
    return AudioPlacement(
        absolute_path=src,
        relative_path=_fake_relative_path(audio_dir, safe_name, suffix),
        duration_s=length + offset,
        time_offset_s=offset,
        did_work=False,
    )


def resolve_cached_placement(
    row: TrackGrid, *, audio_dir: Path, safe_name: str, audio_format: CacheFormat
) -> AudioPlacement:
    """Resolve a track's audio placement for ``--audio wav``/``--audio flac``."""
    dest = audio_dir / f"{safe_name}{audio_dest_extension(row.path, audio_format)}"
    did_work = ensure_audio_cached(row.path, dest, audio_format)
    duration_s = read_duration_seconds(dest)
    return AudioPlacement(
        absolute_path=dest,
        relative_path=library_relative_path(dest),
        duration_s=duration_s,
        time_offset_s=0.0,
        did_work=did_work,
    )


# ── Per-track planning + generation ──────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class TrackPlan:
    """Name and grid anchor computed for one track, independent of audio strategy."""

    row: TrackGrid
    clip_name: str
    safe_name: str
    anchor: AnchorResult

    @property
    def has_drop(self) -> bool:
        """Whether this track has a drop cue (and therefore a "(drop)" variant)."""
        return self.row.drop is not None


def build_plan(row: TrackGrid, *, bar_tolerance: float) -> TrackPlan:
    """Compute the clip name and grid anchor for one track."""
    clip_name = derive_clip_name(row.path.name)
    safe_name = sanitize_filename(clip_name)
    anchor = compute_anchor(bpm=row.bpm, downbeat=row.downbeat, drop=row.drop, bar_tolerance=bar_tolerance)
    return TrackPlan(row=row, clip_name=clip_name, safe_name=safe_name, anchor=anchor)


@dataclass(slots=True, frozen=True)
class _AdjustedTimes:
    """Track cue times shifted into the audio file's own decode timeline."""

    anchor_s: float
    drop_s: float | None
    start_s: float
    duration_s: float


def _adjust_times(plan: TrackPlan, placement: AudioPlacement) -> _AdjustedTimes:
    offset = placement.time_offset_s
    drop_s = plan.row.drop + offset if plan.row.drop is not None else None
    start = plan.row.start if plan.row.start is not None else plan.anchor.anchor_s
    return _AdjustedTimes(
        anchor_s=plan.anchor.anchor_s + offset,
        drop_s=drop_s,
        start_s=start + offset,
        duration_s=placement.duration_s,
    )


def _apply_markers(clip: AlcClip, plan: TrackPlan, times: _AdjustedTimes) -> None:
    """Add the drop marker, if there is one.

    No start-cue marker: clips begin at the start cue, which ``CurrentStart``
    already records. The anchor and the start cue both derive from the drop
    grid and often land tens of milliseconds apart (24 ms on Busta Rhymes
    "Touch It", 41 ms on Phlegmatic Dogs "Cuatrocats"), so a start marker would
    stack a second warp marker at the head of the clip where the user expects one.
    """
    if times.drop_s is not None and plan.anchor.n_bars is not None:
        clip.add_grid_marker(times.drop_s, beat_time=float(plan.anchor.n_bars * 4))


def _build_variant(
    plan: TrackPlan,
    times: _AdjustedTimes,
    placement: AudioPlacement,
    *,
    name: str,
    clip_start_s: float,
    view_end_s: float,
    relative_path_type: int | None,
) -> AlcClip:
    """Author one clip variant (full or drop) from the template."""
    clip = load_template()
    clip.set_grid(bpm=plan.row.bpm, grid_start_seconds=times.anchor_s, duration_seconds=times.duration_s)
    _apply_markers(clip, plan, times)
    # ``relative_path_type`` is only set by an explicit --link-relative-path-type;
    # by default the template's own value (6, "user library") stays -- the correct
    # value for link mode's cross-volume references is pending a live drag test.
    clip.retarget_sample(placement.absolute_path, placement.relative_path, kind=relative_path_type)
    clip.name = name
    clip.set_clip_start(clip_start_s)
    # Open framed on this variant's cue, not the template's full-file view.
    clip.set_view_window(clip_start_s, view_end_s, _VIEW_MARGIN_BARS)
    return clip


def load_template() -> AlcClip:
    """Load the packaged clip template (``abletoolz/data/clip_template.alc``)."""
    with resources.as_file(resources.files(_TEMPLATE_PACKAGE) / _TEMPLATE_RESOURCE) as template_path:
        return AlcClip.load(template_path)


class Output(enum.StrEnum):
    """A format a track list is made into."""

    ALC = "alc"  # a (full) clip and a (drop) clip per track, in a crate folder Live's browser shows
    ASD = "asd"  # a sidecar beside each track's audio, so the file itself opens warped


class AsdOutcome(enum.Enum):
    """What happened to a track's ``.asd`` sidecar."""

    WRITTEN = "written"
    KEPT = "kept"  # it holds clip settings saved in Live, and another grid than this one
    FAILED = "failed"


# How close an existing sidecar's grid must be to count as the one abletoolz would write.
_SAME_GRID_SECONDS = 1e-6


def _holds_grid(asd: AsdFile, bpm: float, anchor_s: float) -> bool:
    """Whether ``asd``'s first two warp markers are this grid: beat 0 at ``anchor_s``, at ``bpm``."""
    markers = asd.markers
    if len(markers) < 2 or markers[0].beat_time != 0.0 or abs(markers[0].sec_time - anchor_s) > _SAME_GRID_SECONDS:
        return False
    first, second = markers[0], markers[1]
    return (
        abs((second.beat_time - first.beat_time) * 60.0 / bpm - (second.sec_time - first.sec_time))
        <= _SAME_GRID_SECONDS
    )


def write_track_asd(plan: TrackPlan, times: _AdjustedTimes, placement: AudioPlacement) -> AsdOutcome:
    """Write the track's grid into the ``.asd`` beside its audio, so the file opens warped however it reaches Live.

    The grid is the full clip's: beat 0 at the anchor, at the track's tempo, in the audio's own decode timeline. A
    sidecar holding clip settings someone saved in Live is kept when its grid is another one; a sidecar of Live's
    analysis alone is rewritten with that analysis kept.
    """
    audio = placement.absolute_path
    asd_path = audio.with_name(audio.name + ".asd")
    if asd_path.exists():
        existing = AsdFile.load(asd_path)
        if existing.loads_clip_defaults and not _holds_grid(existing, plan.row.bpm, times.anchor_s):
            return AsdOutcome.KEPT
    write_grid(
        asd_path,
        bpm=plan.row.bpm,
        anchor_seconds=times.anchor_s,
        audio_path=audio,
        duration_seconds=times.duration_s,
        channel_count=read_channel_count(audio),
    )
    return AsdOutcome.WRITTEN


@dataclass(slots=True)
class TrackResult:
    """The outcome of generating (or planning) one track."""

    plan: TrackPlan
    transcoded: bool = False
    generated_full: bool = False
    generated_drop: bool = False
    error: str | None = None
    asd: AsdOutcome | None = None
    asd_error: str | None = None


def generate_track(
    plan: TrackPlan,
    *,
    crate_dir: Path,
    audio_dir: Path,
    audio_mode: AudioMode,
    relative_path_type: int | None = None,
    make: Collection[Output] = (Output.ALC,),
) -> TrackResult:
    """Resolve the audio placement, then write what ``make`` names: the (full)/(drop) clips, the audio's sidecar."""
    result = TrackResult(plan=plan)
    try:
        placement = (
            resolve_link_placement(plan.row, audio_dir=audio_dir, safe_name=plan.safe_name)
            if audio_mode == AudioMode.LINK
            else resolve_cached_placement(
                plan.row, audio_dir=audio_dir, safe_name=plan.safe_name, audio_format=audio_mode
            )
        )
        result.transcoded = placement.did_work
        times = _adjust_times(plan, placement)
        if Output.ALC in make:
            _write_clips(plan, times, placement, crate_dir, relative_path_type, result)
    except (OSError, ValueError, subprocess.CalledProcessError, AlcError) as exc:
        result.error = str(exc)
        return result
    if Output.ASD in make:
        try:
            result.asd = write_track_asd(plan, times, placement)
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            result.asd, result.asd_error = AsdOutcome.FAILED, str(exc)
    return result


def _write_clips(
    plan: TrackPlan,
    times: _AdjustedTimes,
    placement: AudioPlacement,
    crate_dir: Path,
    relative_path_type: int | None,
    result: TrackResult,
) -> None:
    """Write the track's (full) clip and, when it has a drop, its (drop) clip."""
    full_clip = _build_variant(
        plan,
        times,
        placement,
        name=plan.clip_name,
        # The start CUE, not the grid anchor: the anchor is a downbeat a
        # whole number of bars before the drop, incidental to where the
        # music starts. Starting there opened full clips early or late
        # (-3.99 beats on Bredren "Flick Knife", start 0.000, anchor 1.375;
        # +4 on Enei "The Greatest Trick", start 2.525, anchor 1.146) while
        # both drops stayed exact.
        clip_start_s=times.start_s,
        # Start through drop on screen at once makes the grid checkable at
        # a glance; the start alone when there is no drop.
        view_end_s=times.drop_s if times.drop_s is not None else times.start_s,
        relative_path_type=relative_path_type,
    )
    full_clip.save(crate_dir / f"{plan.safe_name} (full).alc")
    result.generated_full = True

    if times.drop_s is not None:
        drop_clip = _build_variant(
            plan,
            times,
            placement,
            name=f"{plan.clip_name} DROP",
            clip_start_s=times.drop_s,
            view_end_s=times.drop_s,
            relative_path_type=relative_path_type,
        )
        drop_clip.save(crate_dir / f"{plan.safe_name} (drop).alc")
        result.generated_drop = True


# ── Run report ────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class RunReport:
    """Summary of a full crate-generation run, for the end-of-run report."""

    dry_run: bool
    audio_mode: AudioMode
    make: frozenset[Output] = frozenset({Output.ALC})
    results: list[TrackResult] = field(default_factory=list)
    #: (skipped, kept) plans whose rows mapped to the same clip file (e.g. m4a + mp3 rips).
    duplicate_skips: list[tuple[TrackPlan, TrackPlan]] = field(default_factory=list)

    @property
    def generated_count(self) -> int:
        """Tracks whose "(full)" variant was generated (or planned, in a dry run)."""
        return sum(1 for r in self.results if r.generated_full and r.error is None)

    @property
    def drop_generated_count(self) -> int:
        """Tracks whose "(drop)" variant was generated (or planned, in a dry run)."""
        return sum(1 for r in self.results if r.generated_drop and r.error is None)

    def asd_results(self, outcome: AsdOutcome) -> list[TrackResult]:
        """Tracks whose ``.asd`` sidecar came out as ``outcome``."""
        return [r for r in self.results if r.asd is outcome]

    @property
    def cached_skips(self) -> int:
        """Tracks whose audio was already cached (transcode/copy skipped)."""
        return sum(1 for r in self.results if r.error is None and r.generated_full and not r.transcoded)

    @property
    def no_drop_tracks(self) -> list[TrackResult]:
        """Tracks with no drop cue (anchored at the tagged first beat instead)."""
        return [r for r in self.results if r.plan.row.drop is None and r.error is None]

    @property
    def bar_phase_warnings(self) -> list[TrackResult]:
        """Tracks whose drop is out of bar-phase with their tagged first beat."""
        return [r for r in self.results if r.plan.anchor.bar_phase_warning and r.error is None]

    @property
    def failures(self) -> list[TrackResult]:
        """Tracks that failed to generate."""
        return [r for r in self.results if r.error is not None]


def mirror_subpath(source: Path, marker: str) -> Path:
    """The source's own folder hierarchy below ``marker``, as a relative path.

    A crate of three thousand clips in one flat folder is unusable in Live's
    browser, so ``--mirror`` reproduces the collection's structure instead:
    ``...\\DJ Collection\\Drum N Bass\\Dnb Picks\\x.mp3`` puts the clip under
    ``Drum N Bass/Dnb Picks``. Matching is on a path COMPONENT, not a substring,
    so a file that merely has the marker in its name cannot displace a folder.

    Returns an empty relative path for sources sitting at the marker itself, or
    for sources that do not contain it at all -- those land at the crate root
    rather than being dropped.
    """
    parts = source.parts
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] == marker:
            return Path(*parts[index + 1 : -1])
    return Path()


def _source_preference(path: Path) -> int:
    """Rank a source file by how trustworthy its grid alignment is (lower wins).

    Lossless sources have no decoder offset, the MP3 offset is a measured model
    (MPEG_DECODER_DELAY_SAMPLES), and anything else (m4a/AAC rips) plays with an
    unverified offset in Live.
    """
    suffix = path.suffix.lower()
    return 0 if suffix in _LOSSLESS_EXTENSIONS else 1 if suffix == ".mp3" else 2


def run_crate_generation(
    rows: Sequence[TrackGrid],
    *,
    crate_name: str,
    audio_dir: Path,
    crates_dir: Path,
    audio_mode: AudioMode,
    bar_tolerance: float,
    dry_run: bool,
    relative_path_type: int | None = None,
    mirror_marker: str | None = None,
    make: Collection[Output] = (Output.ALC,),
) -> RunReport:
    """Build the crate: plan every row, then generate (unless ``dry_run``).

    Rows that map to the same clip file -- the same tune ripped in two formats, or
    same-named files from different folders in flat mode -- generate one clip: the
    most grid-reliable source wins and the losers land in ``duplicate_skips``.
    """
    crate_root = crates_dir / crate_name
    report = RunReport(dry_run=dry_run, audio_mode=audio_mode, make=frozenset(make))
    chosen: dict[tuple[Path, str], TrackPlan] = {}
    for row in rows:
        plan = build_plan(row, bar_tolerance=bar_tolerance)
        crate_dir = crate_root
        if mirror_marker is not None:
            crate_dir = crate_root / mirror_subpath(row.path, mirror_marker)
        key = (crate_dir, plan.safe_name.casefold())
        existing = chosen.get(key)
        if existing is None:
            chosen[key] = plan
        elif _source_preference(plan.row.path) < _source_preference(existing.row.path):
            chosen[key] = plan
            report.duplicate_skips.append((existing, plan))
        else:
            report.duplicate_skips.append((plan, existing))
    for (crate_dir, _), plan in chosen.items():
        if dry_run:
            report.results.append(
                TrackResult(
                    plan=plan,
                    transcoded=False,
                    generated_full=Output.ALC in make,
                    generated_drop=Output.ALC in make and plan.has_drop,
                    asd=AsdOutcome.WRITTEN if Output.ASD in make else None,
                )
            )
            continue
        report.results.append(
            generate_track(
                plan,
                crate_dir=crate_dir,
                audio_dir=audio_dir,
                audio_mode=audio_mode,
                relative_path_type=relative_path_type,
                make=make,
            )
        )
    return report


def print_report(report: RunReport) -> None:
    """Log the end-of-run summary: counts, no-drop tracks, bar-phase warnings, failures."""
    if Output.ALC in report.make:
        verb = "Would generate" if report.dry_run else "Generated"
        logger.info(
            "%s%s %s clip(s) (%s with a drop variant)", C, verb, report.generated_count, report.drop_generated_count
        )
    if not report.dry_run and report.audio_mode != AudioMode.LINK:
        logger.info("%sSkipped (already cached) transcodes: %s", C, report.cached_skips)
    if Output.ASD in report.make:
        verb = "Would write" if report.dry_run else "Wrote"
        logger.info("%s%s %s .asd sidecar(s) beside the audio", C, verb, len(report.asd_results(AsdOutcome.WRITTEN)))
        kept_asd = report.asd_results(AsdOutcome.KEPT)
        if kept_asd:
            logger.info(
                "%s%s .asd sidecar(s) kept: they hold clip settings saved in Live, another grid:", Y, len(kept_asd)
            )
            for result in kept_asd:
                logger.info("%s  - %s", Y, result.plan.row.path)
        for result in report.asd_results(AsdOutcome.FAILED):
            logger.info("%s  - .asd for %s: %s", R, result.plan.clip_name, result.asd_error)

    if report.duplicate_skips:
        logger.info(
            "%s%s duplicate source(s) skipped -- same clip name, the most grid-reliable format wins:",
            Y,
            len(report.duplicate_skips),
        )
        for skipped, kept in report.duplicate_skips:
            logger.info("%s  - kept %s over %s", Y, kept.row.path, skipped.row.path)

    no_drop = report.no_drop_tracks
    if no_drop:
        logger.info("%s%s track(s) had no drop cue (anchored at the tagged first beat):", Y, len(no_drop))
        for result in no_drop:
            logger.info("%s  - %s", Y, result.plan.clip_name)

    warnings = report.bar_phase_warnings
    if warnings:
        logger.info(
            "%s%s track(s) have a drop out of bar-phase with the tagged first beat "
            "(still generated -- a drop-anchored grid is correct for mixing):",
            R,
            len(warnings),
        )
        for result in warnings:
            logger.info("%s  - %s: residual %.3f bars", R, result.plan.clip_name, result.plan.anchor.residual_bars)

    failures = report.failures
    if failures:
        logger.info("%s%s failure(s):", R, len(failures))
        for result in failures:
            logger.info("%s  - %s: %s", R, result.plan.clip_name, result.error)


# ── CLI ────────────────────────────────────────────────────────────────────────


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the DJ crate generator."""
    parser = argparse.ArgumentParser(
        prog="abletoolz-dj-crates",
        description="Make tracks open in Live already warped and on the bar, from a list of their tempos and "
        "downbeats: .alc clips in a crate, .asd sidecars beside the audio, or both.",
    )
    parser.add_argument(
        "tracks",
        type=Path,
        help="The track list: a .csv with a header row, or a .json list of objects, with path, bpm and downbeat, "
        "and optionally drop, start and length, in seconds. Other columns are ignored.",
    )
    parser.add_argument(
        "--make",
        nargs="+",
        type=Output,
        choices=list(Output),
        default=[Output.ALC],
        metavar="{alc,asd}",
        help="What to make (default: alc). alc: a (full) and a (drop) clip per track in the crate. asd: a sidecar "
        "beside each track's audio, so the file opens warped however it reaches Live; one holding clip settings "
        "saved in Live with another grid is kept.",
    )
    parser.add_argument("--crate", help="Crate name for alc clips, created as a subfolder under --crates-dir.")
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="SUBSTR",
        help="Case-insensitive substring filter on the source path; repeatable (OR'd together).",
    )
    parser.add_argument("--limit", type=int, default=None, help="Cap the number of tracks processed (after filtering).")
    parser.add_argument(
        "--audio",
        choices=["link", "wav", "flac"],
        default="link",
        help="Audio strategy (default: link). 'link' references source files in place with no copy/transcode "
        "(MP3 grids are shifted by Live's decoder offset, measured per file from its header). "
        "'wav'/'flac' transcode "
        "into a shared library cache (self-contained crate, more disk use). FLAC's grid alignment in "
        "Live is UNTESTED.",
    )
    parser.add_argument(
        "--audio-dir",
        type=Path,
        default=None,
        help="Shared audio cache directory (wav/flac modes) or the base used to build link mode's "
        "deliberately-non-resolving RelativePath. Default: 'Audio' inside --crates-dir.",
    )
    parser.add_argument(
        "--crates-dir",
        type=Path,
        default=None,
        help="Parent directory crates are created under. Default: 'DJ Crates' inside your Ableton "
        "User Library, when it exists at Live's standard location for this OS.",
    )
    parser.add_argument(
        "--mirror",
        default=None,
        metavar="FOLDER",
        help="Reproduce the source collection's folder hierarchy inside the crate, instead of "
        "writing every clip into one flat folder. The value names the folder the hierarchy "
        "starts at.",
    )
    parser.add_argument("--dry-run", action="store_true", default=False, help="Print the plan; write nothing.")
    parser.add_argument(
        "--bar-tolerance",
        type=float,
        default=_DEFAULT_BAR_TOLERANCE,
        help="Residual bars beyond which a bar-phase warning is reported (default: 0.02).",
    )
    parser.add_argument(
        "--link-relative-path-type",
        type=int,
        default=None,
        help="Override SampleRef/FileRef/RelativePathType (link mode). Default: leave the template's own "
        "value (6) untouched -- the correct value for link mode's cross-volume references is pending a "
        "live drag test.",
    )
    args = parser.parse_args(argv)
    if Output.ALC in args.make and not args.crate:
        parser.error("--crate is needed to make alc clips")
    return args


def resolve_output_dirs(
    crates_dir: Path | None, audio_dir: Path | None, user_library: Path | None
) -> tuple[Path, Path]:
    """CLI-level smarts: fall back to Live's User Library only when it actually exists.

    ``user_library`` is that library, None when it isn't at the OS's standard
    location; then a missing crates dir raises ValueError -- the caller must ask
    instead of guessing.
    """
    if crates_dir is None:
        if user_library is None:
            raise ValueError(
                "Could not find your Ableton User Library at its standard location; pass --crates-dir explicitly."
            )
        crates_dir = user_library / "DJ Crates"
    if audio_dir is None:
        audio_dir = crates_dir / "Audio"
    return crates_dir, audio_dir


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: ``python -m abletoolz.dj_crates``."""
    args = parse_arguments(argv)
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")

    try:
        crates_dir, audio_dir = resolve_output_dirs(
            args.crates_dir, args.audio_dir, Locations.this_computer().user_library
        )
    except ValueError as exc:
        if Output.ALC in args.make or args.audio != AudioMode.LINK:
            logger.error("%s%s", R, exc)
            return 2
        crates_dir = audio_dir = Path()  # sidecars beside linked audio use neither folder

    try:
        rows = read_track_list(args.tracks)
    except (OSError, ValueError) as exc:
        logger.error("%s%s", R, exc)
        return 2
    filtered = filter_rows(rows, includes=args.include, limit=args.limit)
    if not filtered:
        logger.info("%sNo tracks matched the given filters.", R)
        return 1

    logger.info("%s%s track(s) selected", C, len(filtered))
    report = run_crate_generation(
        filtered,
        crate_name=args.crate or "",
        audio_dir=audio_dir.expanduser(),
        crates_dir=crates_dir.expanduser(),
        audio_mode=args.audio,
        bar_tolerance=args.bar_tolerance,
        dry_run=args.dry_run,
        relative_path_type=args.link_relative_path_type,
        mirror_marker=args.mirror,
        make=args.make,
    )
    print_report(report)
    return 1 if report.failures or report.asd_results(AsdOutcome.FAILED) else 0


if __name__ == "__main__":
    sys.exit(main())
