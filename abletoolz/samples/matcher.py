"""What the relink lookup measures about a candidate file beside its record: its length and its folder."""

from __future__ import annotations

import contextlib
import pathlib
import wave
from functools import lru_cache

from mutagen import File as mutagen_file
from mutagen import MutagenError


def _parts(path: pathlib.Path) -> list[str]:
    """Return lowercase POSIX-like path segments excluding empty and dot."""
    return [seg for seg in str(path).replace("\\", "/").lower().split("/") if seg not in ("", ".")]


def get_audio_length_seconds(path: pathlib.Path) -> float | None:
    """Cache duration until the file's size or nanosecond modification time changes."""
    try:
        metadata = path.stat()
    except (FileNotFoundError, PermissionError):
        return None
    return _read_audio_length_seconds(path, metadata.st_size, metadata.st_mtime_ns)


@lru_cache(maxsize=4096)
def _read_audio_length_seconds(path: pathlib.Path, size: int, modified_ns: int) -> float | None:
    """Read a particular file revision; revision arguments are part of the cache key."""
    # Prefer mutagen for broad codec support (mp3/mp4/ogg/flac/wav/aiff)
    try:
        info = mutagen_file(str(path))
        if info is not None and getattr(info, "info", None) is not None:
            length = getattr(info.info, "length", None)
            if isinstance(length, (int, float)):
                return float(length)
    except (MutagenError, OSError):
        pass
    # Fallback for plain PCM wav; mutagen already covers aiff and the rest.
    if path.suffix.lower() == ".wav":
        try:
            with contextlib.closing(wave.open(str(path), "rb")) as f:
                frames = f.getnframes()
                rate = f.getframerate()
                return frames / float(rate) if rate else None
        except (wave.Error, FileNotFoundError, PermissionError):
            return None
    return None


def folder_scores(original_path: pathlib.Path, candidate: pathlib.Path) -> tuple[int, int, int, int, int]:
    """Compute folder-based scores: suffix, parent, prefix, overlap, -len(path)."""
    orig_parts = _parts(original_path)[:-1]
    cand_parts = _parts(candidate)[:-1]

    # Longest common suffix
    suffix = 0
    for a, b in zip(reversed(orig_parts), reversed(cand_parts), strict=False):
        if a == b:
            suffix += 1
        else:
            break

    parent_match = 1 if orig_parts and cand_parts and orig_parts[-1] == cand_parts[-1] else 0

    # Longest common prefix
    prefix = 0
    for a, b in zip(orig_parts, cand_parts, strict=False):
        if a == b:
            prefix += 1
        else:
            break

    overlap = len(set(orig_parts).intersection(set(cand_parts)))
    neg_len = -len(str(candidate))
    return suffix, parent_match, prefix, overlap, neg_len


def length_score(target_length: float | None, candidate: pathlib.Path) -> int:
    """Return an integer score prioritizing close duration matches; 0 if unknown."""
    if target_length is None:
        return 0
    cand_len = get_audio_length_seconds(candidate)
    if cand_len is None:
        return 0
    diff_ms = abs(cand_len - target_length) * 1000.0
    return int(max(0.0, 1_000_000 - diff_ms))


def is_factory_pack_path(path: pathlib.Path) -> bool:
    """Return True if path points inside Ableton factory content."""
    s = path.as_posix()
    return "/Resources/Builtin/Samples" in s or "Ableton/Factory Packs" in s
