r"""abletoolz.dj_crates -- build a DJ "crate" of pre-gridded ``.alc`` clips.

Turns a cue_finder TSV export (externally analyzed DJ tracks: BPM, first-beat time,
start/drop hotcues) into a folder of Ableton Live Clip files inside the user's Ableton
User Library. Two clip variants are written per track: ``(full)`` (starts at the grid
anchor) and ``(drop)`` (starts at the drop cue, skipped when the track has no drop).

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
  authored time shifted by :func:`mp3_decoder_offset_seconds` -- one of two constant
  sample counts keyed on whether the file's Xing/Info header carries a gapless block
  (see :data:`LIVE_MP3_OFFSET_TAGGED_SAMPLES` / :data:`LIVE_MP3_OFFSET_UNTAGGED_SAMPLES`),
  verified sample-exact at 44100 Hz only; other sample rates still get the same
  sample-count offset (scaled by that file's own rate) but are flagged in the
  end-of-run report as unverified.
  WAV/AIFF sources need no shift (PCM decode is deterministic) and are referenced in
  place.
* ``wav`` / ``flac`` -- the original transcode-into-library behavior: MP3 (and other
  compressed) sources are transcoded via ffmpeg into a shared cache under
  ``--audio-dir`` (Live's MP3 decoder is offset from ffmpeg's, which would otherwise
  shift the whole grid); WAV/AIFF sources are copied unchanged. Idempotent -- an
  existing cache entry is left alone. This is the fallback for crates meant to be
  self-contained (no cross-volume/UNC references).

CLI usage::

    python -m abletoolz.dj_crates picks.tsv --crate "Dnb Picks" --dry-run
"""

from __future__ import annotations

import argparse
import csv
import enum
import logging
import re
import shutil
import subprocess
import sys
import wave
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Literal

from abletoolz.alc import AlcClip, AlcError
from abletoolz.misc import C, R, Y, default_ableton_user_library

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

_LOSSLESS_EXTENSIONS = {".wav", ".aiff", ".aif"}
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


# ── TSV parsing ──────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class PicksRow:
    """One parsed row of a cue_finder TSV export (schema v2).

    The ``mp3_*`` and ``samplerate`` columns are cue_finder's own structural header
    parse (export contract: all times are in the honest-decode timeline; consumers
    whose decoder disagrees translate using these fields). Empty for non-MP3 rows.
    """

    path: Path
    filename: str
    title_key: str
    duration_s: float
    bpm: float
    beat_offset_s: float
    start_s: float
    start_beat: float
    start_bar: float
    drop_s: float | None
    drop_beat: float | None
    drop_bar: float | None
    bars_start_to_drop: float | None
    confidence: float
    needs_review: bool
    verdict: str
    human_reviewed: bool
    mp3_tag: str
    mp3_lame_ext: bool
    mp3_encoder_delay: int | None
    samplerate: int | None


# Columns that distinguish a schema-v2 export from v1. Their absence means the TSV
# predates the header-class contract and must be re-exported, not guessed at.
_SCHEMA_V2_COLUMNS = ("mp3_tag", "mp3_lame_ext", "mp3_encoder_delay", "samplerate")


def _parse_bool(value: str) -> bool:
    """Parse a TSV "True"/"False" boolean field."""
    if value == "True":
        return True
    if value == "False":
        return False
    raise ValueError(f"expected 'True' or 'False', got {value!r}")


def _parse_optional_float(value: str) -> float | None:
    """Parse a TSV float field that may be empty (e.g. ``drop_s`` with no drop)."""
    return float(value) if value else None


def _parse_optional_int(value: str) -> int | None:
    """Parse a TSV int field that may be empty (non-MP3 rows)."""
    return int(value) if value else None


def parse_picks_row(record: Mapping[str, str]) -> PicksRow:
    """Parse one cue_finder TSV record (as produced by :class:`csv.DictReader`)."""
    return PicksRow(
        path=Path(record["path"]),
        filename=record["filename"],
        title_key=record["title_key"],
        duration_s=float(record["duration_s"]),
        bpm=float(record["bpm"]),
        beat_offset_s=float(record["beat_offset_s"]),
        start_s=float(record["start_s"]),
        start_beat=float(record["start_beat"]),
        start_bar=float(record["start_bar"]),
        drop_s=_parse_optional_float(record["drop_s"]),
        drop_beat=_parse_optional_float(record["drop_beat"]),
        drop_bar=_parse_optional_float(record["drop_bar"]),
        bars_start_to_drop=_parse_optional_float(record["bars_start_to_drop"]),
        confidence=float(record["confidence"]),
        needs_review=_parse_bool(record["needs_review"]),
        verdict=record["verdict"],
        human_reviewed=_parse_bool(record["human_reviewed"]),
        mp3_tag=record["mp3_tag"],
        mp3_lame_ext=_parse_bool(record["mp3_lame_ext"]) if record["mp3_lame_ext"] else False,
        mp3_encoder_delay=_parse_optional_int(record["mp3_encoder_delay"]),
        samplerate=_parse_optional_int(record["samplerate"]),
    )


def read_picks_tsv(path: Path) -> list[PicksRow]:
    """Read and parse a cue_finder TSV export. Rejects pre-v2 exports loudly."""
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        fields = reader.fieldnames or []
        missing = [c for c in _SCHEMA_V2_COLUMNS if c not in fields]
        if missing:
            raise ValueError(
                f"{path.name} is a pre-v2 cue_finder export (missing columns: {', '.join(missing)}). "
                "Re-export with a schema_version >= 2 cue-finder."
            )
        return [parse_picks_row(record) for record in reader]


def filter_rows(
    rows: Sequence[PicksRow],
    *,
    includes: Sequence[str] = (),
    reviewed_only: bool = False,
    limit: int | None = None,
) -> list[PicksRow]:
    """Filter TSV rows by path substring(s) (OR'd), review status, and a count cap."""
    filtered: Sequence[PicksRow] = rows
    if includes:
        lowered = [s.lower() for s in includes]
        filtered = [row for row in filtered if any(s in str(row.path).lower() for s in lowered)]
    if reviewed_only:
        filtered = [row for row in filtered if row.human_reviewed]
    if limit is not None:
        filtered = filtered[:limit]
    return list(filtered)


# ── Grid anchor (bar-fold rule) ──────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class AnchorResult:
    """Where a track's grid anchor (beat 0) lands, per the bar-fold rule.

    ``residual_bars`` is the signed distance (in bars) between the drop and the
    nearest whole-bar multiple back from it, ignoring the "don't go negative" fold
    adjustment -- it flags when the analyzer's ``beat_offset_s``/``drop_s``/``bpm``
    disagree about bar phase (e.g. a track whose tagged first beat is 0.25 bars out
    of phase with its drop).
    """

    anchor_s: float
    n_bars: int | None
    residual_bars: float
    bar_phase_warning: bool


def compute_anchor(
    *, bpm: float, beat_offset_s: float, drop_s: float | None, bar_tolerance: float = _DEFAULT_BAR_TOLERANCE
) -> AnchorResult:
    """Compute the grid anchor (beat 0) for one track.

    With a drop: the anchor is the drop folded back by whole bars (``round``, not
    ``floor`` -- floor loses whole bars to float noise) so it lands on the same bar
    phase as the drop, as close to the track start as possible without going
    negative. Without a drop: the anchor is the track's tagged first beat.

    This operates purely in the analyzer's own time coordinates -- a constant
    per-track offset (e.g. the MP3 link-mode decoder correction, see
    :func:`mp3_decoder_offset_seconds`) is applied later, at clip-authoring time, and
    does not change the bar-fold math (it shifts ``beat_offset_s`` and ``drop_s`` by
    the same amount, leaving their difference, and therefore ``n_bars``/
    ``residual_bars``, unchanged).
    """
    if drop_s is None:
        return AnchorResult(anchor_s=beat_offset_s, n_bars=None, residual_bars=0.0, bar_phase_warning=False)

    bar_s = 240.0 / bpm
    raw_bars = (drop_s - beat_offset_s) / bar_s
    n_bars = round(raw_bars)
    anchor_s = drop_s - n_bars * bar_s
    if anchor_s < 0.0:
        # The nearest downbeat is before the file starts. Stepping forward a
        # whole bar (the old unconditional behaviour) puts 1.1.1 a full bar
        # after the music whenever the drop sits a hair below a whole number of
        # bars -- 597 library tracks land within a millisecond of that. When the
        # shortfall is negligible, treat the downbeat as being at zero instead;
        # only step forward when it is genuinely mid-bar.
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
    """Replace filesystem-unsafe characters in a name meant to become a filename."""
    return _INVALID_FILENAME_CHARS.sub("_", name).strip()


# ── MP3 decoder offset (link mode) ───────────────────────────────────────────

# MODEL, ground-truth verified via synthetic golden files with a known drop
# position plus five real-track WAV<->MP3 onset pairings, all sample-exact.
# Live-vs-ffmpeg offset = trim + surplus:
#   trim:    ffmpeg honors the gapless block (header delay + 529 samples) when the
#            extension carries a valid LAME/Lavf/Lavc signature; Live never trims.
#   surplus: Live decodes an "Info"-tagged metadata frame as audio (+1152 samples);
#            both decoders skip a "Xing"-tagged frame.
# Measured: Info+sig d576 -> 2257 (golden + Gravity/Caligo/HandGestures/R.E.M);
# Xing+sig d576 -> 1105 (Dune); real frame, no tag -> 0 (golden). The Info-without-
# sig class (predicts 1152) is what cue_finder's Mixxx info-frame repair produces --
# golden-verifiable the same way. Verified only at 44100 Hz; other rates get the
# sample count scaled by their own rate but are flagged in the run report.
MPEG_DECODER_DELAY_SAMPLES = 529
MPEG1_FRAME_SAMPLES = 1152

_VERIFIED_MP3_SAMPLE_RATE = 44100

# A plausible gapless-block delay. The upper bound rejects 0xFFF garbage, which
# means the block is absent or unusable.
#
# Zero is ACCEPTED, though it used to be excluded. Measured on a real MP3
# ("Phlegmatic Dogs - Cuatrocats (Volac Remix)") whose LAME extension declares a
# delay of 0: the stream holds 10070 frames = 11600640 samples, ffmpeg decodes
# 11598959, so it discards exactly 1681 = 1152 + 529. ffmpeg applies the 529
# decoder delay whenever the tag is present, regardless of the declared value.
# Excluding 0 made this function return 1152 while the row-based
# `resolve_link_placement` returned 1681 -- the two disagreed by 12 ms on the
# 31 library files in this class.
_GAPLESS_DELAY_RANGE = range(0, 4096)

_GAPLESS_SIGNATURES = (b"LAME", b"Lavf", b"Lavc")


def mp3_decode_offset_samples(path: Path) -> int:
    """Samples to add to analyzer timestamps to match Live's decode of this MP3.

    Seeks past the ID3v2 block by its declared size (embedded album art can be
    multiple MB -- a fixed read window misclassifies those files), then reads the
    first MPEG frame's tag and gapless extension at their structural offsets.
    Loose encoder strings elsewhere (e.g. an ID3 TSSE "LAME ..." text frame) are
    irrelevant. See the model comment above.
    """
    with path.open("rb") as fh:
        head = fh.read(10)
        if head[:3] == b"ID3" and len(head) == 10:
            size = (head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]
            fh.seek(10 + size + (10 if head[5] & 0x10 else 0))
            data = fh.read(65536)
        else:
            data = head + fh.read(65536)
    pos = 0
    while pos + 4 <= len(data):
        b0, b1, b2, b3 = data[pos : pos + 4]
        if b0 == 0xFF and (b1 & 0xE0) == 0xE0 and (b1 >> 3) & 0x03 == 0x03 and (b1 >> 1) & 0x03 == 0x01:
            break  # MPEG1 Layer III frame header
        pos += 1
    else:
        # Unparseable: assume the collection's majority class (Info + gapless sig).
        return MPEG1_FRAME_SAMPLES + 576 + MPEG_DECODER_DELAY_SAMPLES
    mono = (b3 >> 6) == 0x03
    xing_at = pos + 4 + (17 if mono else 32)
    tag = data[xing_at : xing_at + 4]
    if tag not in (b"Xing", b"Info"):
        return 0  # real audio frame first: both decoders start identically
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
    surplus = MPEG1_FRAME_SAMPLES if tag == b"Info" else 0
    return trim + surplus


def mp3_decoder_offset_seconds(path: Path, sample_rate: int) -> float:
    """Seconds to add to analyzer timestamps to match Live's MP3 decode timeline.

    See :func:`mp3_decode_offset_samples` -- verified only at 44100 Hz; other
    rates apply the same sample count but the result is unverified (the caller
    should flag it, see ``AudioPlacement.offset_unverified``).
    """
    return mp3_decode_offset_samples(path) / sample_rate


def read_sample_rate(path: Path) -> int:
    """Read an audio file's sample rate via ``ffprobe``."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(result.stdout.strip())


# ── Audio cache (transcode / copy, idempotent -- ``wav``/``flac`` modes) ─────


def audio_dest_extension(src: Path, audio_format: CacheFormat) -> str:
    """The cached audio file's extension.

    Lossless sources (WAV/AIFF) are copied unchanged and keep their own extension.
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

    MP3 (and other compressed) sources are transcoded via ffmpeg -- Live's MP3 decoder
    is offset from ffmpeg's timeline, which would shift the whole grid. WAV/AIFF
    sources are copied unchanged. Idempotent: a pre-existing ``dest`` is left alone.

    Returns:
        True if work was performed (copy or transcode); False if ``dest`` already
        existed (cache hit -- nothing done).
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


def _set_relative_path_type(clip: AlcClip, value: int) -> None:
    """Override the clip's ``SampleRef/FileRef/RelativePathType``.

    Only called when ``--link-relative-path-type`` is explicitly passed; by default
    the template's own value (6, "user library") is left untouched -- the correct
    value for link mode's cross-volume references is pending a live drag test.
    """
    element = clip.clip.find("SampleRef/FileRef/RelativePathType")
    if element is None:
        raise AlcError("clip is missing SampleRef/FileRef/RelativePathType")
    element.set("Value", str(value))


# ── Audio placement (where the clip's audio lives, per --audio mode) ────────


@dataclass(slots=True, frozen=True)
class AudioPlacement:
    """Where a track's clip audio lives, and how cue-times must shift to match it."""

    absolute_path: Path
    relative_path: Path
    duration_s: float
    time_offset_s: float
    did_work: bool
    offset_unverified: bool = False


def _fake_relative_path(audio_dir: Path, safe_name: str, ext: str) -> Path:
    """A deliberately non-resolving RelativePath so Live falls back to the absolute Path.

    Shaped like a real (wav/flac-mode) cache path, but nothing is ever written there
    in link mode -- the point is for Live's relative-path resolution to fail and fall
    through to the absolute ``Path`` field.
    """
    return library_relative_path(audio_dir / f"{safe_name}{ext}")


def mp3_offset_from_row(row: PicksRow) -> float:
    """Live decode offset (seconds) computed from the export's v2 header columns.

    Data-driven version of :func:`mp3_decode_offset_samples`: cue_finder's export
    carries the same structural header parse (``mp3_tag``/``mp3_lame_ext``/
    ``mp3_encoder_delay``/``samplerate``), so link mode needs no file access at all.
    Same measured model: trim (delay + 529 when a gapless extension exists; Live
    never trims) + surplus (1152 when the tag frame is "Info"; Live plays it).
    """
    if row.samplerate is None:
        raise ValueError(f"{row.filename}: MP3 row without a samplerate column value")
    trim = (row.mp3_encoder_delay or 0) + MPEG_DECODER_DELAY_SAMPLES if row.mp3_lame_ext else 0
    surplus = MPEG1_FRAME_SAMPLES if row.mp3_tag == "Info" else 0
    return (trim + surplus) / row.samplerate


def resolve_link_placement(row: PicksRow, *, audio_dir: Path, safe_name: str) -> AudioPlacement:
    """Resolve a track's audio placement for ``--audio link`` (no copy, no transcode).

    MP3 offsets and durations come straight from the export's v2 columns -- no
    ffprobe, no file reads. Non-44.1kHz MP3s still get the offset (scaled by their
    own rate) but ``offset_unverified`` is set so the caller can flag it.
    """
    src = row.path
    suffix = src.suffix.lower()
    relative_path = _fake_relative_path(audio_dir, safe_name, suffix)

    if suffix == ".mp3":
        offset = mp3_offset_from_row(row)
        return AudioPlacement(
            absolute_path=src,
            relative_path=relative_path,
            duration_s=row.duration_s + offset,
            time_offset_s=offset,
            did_work=False,
            offset_unverified=row.samplerate != _VERIFIED_MP3_SAMPLE_RATE,
        )

    return AudioPlacement(
        absolute_path=src,
        relative_path=relative_path,
        duration_s=row.duration_s,
        time_offset_s=0.0,
        did_work=False,
    )


def resolve_cached_placement(
    row: PicksRow, *, audio_dir: Path, safe_name: str, audio_format: CacheFormat
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
    """Name and grid anchor computed for one TSV row, independent of audio strategy."""

    row: PicksRow
    clip_name: str
    safe_name: str
    anchor: AnchorResult

    @property
    def has_drop(self) -> bool:
        """Whether this track has a drop cue (and therefore a "(drop)" variant)."""
        return self.row.drop_s is not None


def build_plan(row: PicksRow, *, bar_tolerance: float) -> TrackPlan:
    """Compute the clip name and grid anchor for one TSV row."""
    clip_name = derive_clip_name(row.filename)
    safe_name = sanitize_filename(clip_name)
    anchor = compute_anchor(
        bpm=row.bpm, beat_offset_s=row.beat_offset_s, drop_s=row.drop_s, bar_tolerance=bar_tolerance
    )
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
    drop_s = plan.row.drop_s + offset if plan.row.drop_s is not None else None
    return _AdjustedTimes(
        anchor_s=plan.anchor.anchor_s + offset,
        drop_s=drop_s,
        start_s=plan.row.start_s + offset,
        duration_s=placement.duration_s,
    )


def _apply_markers(clip: AlcClip, plan: TrackPlan, times: _AdjustedTimes) -> None:
    """Add the drop marker, if there is one.

    No start-cue marker. It used to be added because full clips began at the
    ANCHOR, so the start cue needed something to show where it was; clips now
    begin at the start cue itself and ``CurrentStart`` already records it, which
    left the marker carrying no information the clip did not state twice.

    It was also actively unhelpful. The anchor and the start cue are both
    derived from the drop grid, so they often land within a few tens of
    milliseconds of one another -- measured at 24 ms on Busta Rhymes "Touch It"
    and 41 ms on Phlegmatic Dogs "Cuatrocats" -- giving two warp markers stacked
    at the head of the clip where the user expects one.
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
    clip.retarget_sample(placement.absolute_path, placement.relative_path)
    if relative_path_type is not None:
        _set_relative_path_type(clip, relative_path_type)
    clip.name = name
    clip.set_clip_start(clip_start_s)
    # Open zoomed to the first few phrases from wherever this variant starts,
    # rather than the template's full-file view: the cue and the bar lines
    # around it are what a person opens one of these to look at.
    clip.set_view_window(clip_start_s, view_end_s, _VIEW_MARGIN_BARS)
    return clip


def load_template() -> AlcClip:
    """Load the packaged clip template (``abletoolz/data/clip_template.alc``)."""
    with resources.as_file(resources.files(_TEMPLATE_PACKAGE) / _TEMPLATE_RESOURCE) as template_path:
        return AlcClip.load(template_path)


@dataclass(slots=True)
class TrackResult:
    """The outcome of generating (or planning) one track."""

    plan: TrackPlan
    transcoded: bool = False
    generated_full: bool = False
    generated_drop: bool = False
    offset_unverified: bool = False
    error: str | None = None


def generate_track(
    plan: TrackPlan,
    *,
    crate_dir: Path,
    audio_dir: Path,
    audio_mode: AudioMode,
    relative_path_type: int | None = None,
) -> TrackResult:
    """Resolve the audio placement and write the (full)/(drop) .alc clips."""
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
        result.offset_unverified = placement.offset_unverified
        times = _adjust_times(plan, placement)

        full_clip = _build_variant(
            plan,
            times,
            placement,
            name=plan.clip_name,
            # The start CUE, not the grid anchor. The anchor is beat one of the
            # grid -- a downbeat placed a whole number of bars before the drop --
            # and where it falls relative to the music's actual start is
            # incidental. Anchoring the clip to it opened every full clip early
            # or late by however much the two differ: measured -3.99 beats on
            # Bredren "Flick Knife" (start 0.000, anchor 1.375) and +4 beats on
            # Enei "The Greatest Trick" (start 2.525, anchor 1.146), while both
            # drops stayed exact -- one timeline, so a right drop beside a wrong
            # start is a start-selection bug, not an offset one.
            clip_start_s=times.start_s,
            # The full clip opens framed on start-through-drop: both cues on
            # screen at once is what makes a generated grid checkable at a
            # glance. Falls back to the start alone when there is no drop.
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
    except (OSError, ValueError, subprocess.CalledProcessError, AlcError) as exc:
        result.error = str(exc)
    return result


# ── Run report ────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class RunReport:
    """Summary of a full crate-generation run, for the end-of-run report."""

    dry_run: bool
    audio_mode: AudioMode
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

    @property
    def cached_skips(self) -> int:
        """Tracks whose audio was already cached (transcode/copy skipped)."""
        return sum(1 for r in self.results if r.error is None and r.generated_full and not r.transcoded)

    @property
    def no_drop_tracks(self) -> list[TrackResult]:
        """Tracks with no drop cue (anchored at the tagged first beat instead)."""
        return [r for r in self.results if r.plan.row.drop_s is None and r.error is None]

    @property
    def bar_phase_warnings(self) -> list[TrackResult]:
        """Tracks whose drop is out of bar-phase with their tagged first beat."""
        return [r for r in self.results if r.plan.anchor.bar_phase_warning and r.error is None]

    @property
    def non_44k_warnings(self) -> list[TrackResult]:
        """Link-mode MP3s at a non-44.1kHz sample rate (decoder offset unverified there)."""
        return [r for r in self.results if r.offset_unverified and r.error is None]

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


# Same-name duplicate resolution: lossless sources play with no decoder offset at
# all, the MP3 offset is a measured model (MPEG_DECODER_DELAY_SAMPLES), anything
# else (m4a/AAC rips) plays with an unverified offset in Live.
_GRID_RELIABILITY = {".wav": 0, ".aif": 0, ".aiff": 0, ".flac": 0, ".mp3": 1}


def _source_preference(path: Path) -> int:
    """Rank a source file by how trustworthy its grid alignment is (lower wins)."""
    return _GRID_RELIABILITY.get(path.suffix.lower(), 2)


def run_crate_generation(
    rows: Sequence[PicksRow],
    *,
    crate_name: str,
    audio_dir: Path,
    crates_dir: Path,
    audio_mode: AudioMode,
    bar_tolerance: float,
    dry_run: bool,
    relative_path_type: int | None = None,
    mirror_marker: str | None = None,
) -> RunReport:
    """Build the crate: plan every row, then generate (unless ``dry_run``).

    Rows that map to the same clip file -- the same tune ripped in two formats, or
    same-named files from different folders in flat mode -- generate one clip: the
    most grid-reliable source wins and the losers land in ``duplicate_skips``.
    """
    crate_root = crates_dir / crate_name
    report = RunReport(dry_run=dry_run, audio_mode=audio_mode)
    chosen: dict[tuple[Path, str], TrackPlan] = {}
    for row in rows:
        plan = build_plan(row, bar_tolerance=bar_tolerance)
        crate_dir = crate_root
        if mirror_marker is not None:
            crate_dir = crate_root / mirror_subpath(Path(row.path), mirror_marker)
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
                TrackResult(plan=plan, transcoded=False, generated_full=True, generated_drop=plan.has_drop)
            )
            continue
        report.results.append(
            generate_track(
                plan,
                crate_dir=crate_dir,
                audio_dir=audio_dir,
                audio_mode=audio_mode,
                relative_path_type=relative_path_type,
            )
        )
    return report


def print_report(report: RunReport) -> None:
    """Log the end-of-run summary: counts, no-drop tracks, bar-phase warnings, failures."""
    verb = "Would generate" if report.dry_run else "Generated"
    logger.info(
        "%s%s %s clip(s) (%s with a drop variant)", C, verb, report.generated_count, report.drop_generated_count
    )
    if not report.dry_run and report.audio_mode != AudioMode.LINK:
        logger.info("%sSkipped (already cached) transcodes: %s", C, report.cached_skips)

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

    non_44k = report.non_44k_warnings
    if non_44k:
        logger.info(
            "%s%s track(s) are MP3 at a non-44.1kHz sample rate under --audio link "
            "(decoder offset is unverified at that rate, still generated):",
            Y,
            len(non_44k),
        )
        for result in non_44k:
            logger.info("%s  - %s", Y, result.plan.clip_name)

    failures = report.failures
    if failures:
        logger.info("%s%s failure(s):", R, len(failures))
        for result in failures:
            logger.info("%s  - %s: %s", R, result.plan.clip_name, result.error)


# ── CLI ────────────────────────────────────────────────────────────────────────


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the DJ crate generator."""
    parser = argparse.ArgumentParser(
        prog="python -m abletoolz.dj_crates",
        description="Build a DJ crate of pre-gridded .alc clips from a cue_finder TSV export.",
    )
    parser.add_argument("tsv", type=Path, help="Path to the cue_finder TSV export.")
    parser.add_argument("--crate", required=True, help="Crate name (created as a subfolder under --crates-dir).")
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="SUBSTR",
        help="Case-insensitive substring filter on the source path; repeatable (OR'd together).",
    )
    parser.add_argument(
        "--reviewed-only", action="store_true", default=False, help="Only include rows with human_reviewed=True."
    )
    parser.add_argument("--limit", type=int, default=None, help="Cap the number of tracks processed (after filtering).")
    parser.add_argument(
        "--audio",
        choices=["link", "wav", "flac"],
        default="link",
        help="Audio strategy (default: link). 'link' references source files in place with no copy/transcode "
        "(MP3 grids are shifted by a constant, empirically-measured decoder offset, verified only at "
        "44100 Hz -- other rates are flagged in the report but still generated). 'wav'/'flac' transcode "
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
    return parser.parse_args(argv)


def resolve_output_dirs(crates_dir: Path | None, audio_dir: Path | None) -> tuple[Path, Path]:
    """CLI-level smarts: fall back to Live's User Library only when it actually exists.

    Raises ValueError when no crates dir was given and the User Library isn't at
    this OS's standard location — the caller must ask instead of guessing.
    """
    if crates_dir is None:
        user_library = default_ableton_user_library()
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
        crates_dir, audio_dir = resolve_output_dirs(args.crates_dir, args.audio_dir)
    except ValueError as exc:
        logger.error("%s%s", R, exc)
        return 2

    rows = read_picks_tsv(args.tsv)
    filtered = filter_rows(rows, includes=args.include, reviewed_only=args.reviewed_only, limit=args.limit)
    if not filtered:
        logger.info("%sNo tracks matched the given filters.", R)
        return 1

    logger.info("%s%s track(s) selected for crate %r", C, len(filtered), args.crate)
    report = run_crate_generation(
        filtered,
        crate_name=args.crate,
        audio_dir=audio_dir.expanduser(),
        crates_dir=crates_dir.expanduser(),
        audio_mode=args.audio,
        bar_tolerance=args.bar_tolerance,
        dry_run=args.dry_run,
        relative_path_type=args.link_relative_path_type,
        mirror_marker=args.mirror,
    )
    print_report(report)
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
