"""Common candidate selection utilities for fixing sample paths."""

from __future__ import annotations

import contextlib
import pathlib
import wave
from collections.abc import Mapping
from typing import Any

from mutagen import File as mutagen_file
from mutagen import MutagenError


def _parts(path: pathlib.Path) -> list[str]:
    """Return lowercase POSIX-like path segments excluding empty and dot."""
    return [seg for seg in path.as_posix().lower().split("/") if seg not in ("", ".")]


def get_audio_length_seconds(path: pathlib.Path) -> float | None:
    """Return duration using mutagen first; fallback to WAV/AIFF headers."""
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


def _folder_scores(original_path: pathlib.Path, candidate: pathlib.Path) -> tuple[int, int, int, int, int]:
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


def _length_score(target_length: float | None, candidate: pathlib.Path) -> int:
    """Return an integer score prioritizing close duration matches; 0 if unknown."""
    if target_length is None:
        return 0
    cand_len = get_audio_length_seconds(candidate)
    if cand_len is None:
        return 0
    diff_ms = abs(cand_len - target_length) * 1000.0
    return int(max(0.0, 1_000_000 - diff_ms))


def _size_score(target_size: int | None, meta: Mapping[str, Any]) -> int:
    """Return binary score for exact size match from DB meta."""
    if target_size is None:
        return 0
    try:
        return 1 if int(meta.get("size", -1)) == int(target_size) else 0
    except (TypeError, ValueError):
        return 0


def _mtime_score(target_mtime: int | None, meta: Mapping[str, Any]) -> int:
    """Return binary score for exact mtime match from DB meta."""
    if target_mtime is None:
        return 0
    try:
        return 1 if int(meta.get("last_modified", -1)) == int(target_mtime) else 0
    except (TypeError, ValueError):
        return 0


def select_best_candidate_by_name(
    db: Mapping[str, Mapping[str, Any]],
    file_name: str,
    original_path: pathlib.Path,
    *,
    target_length: float | None = None,
    target_size: int | None = None,
    target_mtime: int | None = None,
) -> pathlib.Path | None:
    """Pick best candidate for file_name using duration and folder heuristics."""
    candidates = [pathlib.Path(p) for p, meta in db.items() if meta.get("name") == file_name]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    def score(candidate: pathlib.Path) -> tuple[int, int, int, int, int, int, int, int]:
        meta = db.get(str(candidate), {})
        length = _length_score(target_length, candidate)
        size = _size_score(target_size, meta)
        mtime = _mtime_score(target_mtime, meta)
        suffix, parent, prefix, overlap, neg_len = _folder_scores(original_path, candidate)
        return (length, size, mtime, suffix, parent, prefix, overlap, neg_len)

    return max(candidates, key=score)


def order_candidates_by_name(
    db: Mapping[str, Mapping[str, Any]],
    file_name: str,
    original_path: pathlib.Path,
    *,
    target_length: float | None = None,
    target_size: int | None = None,
    target_mtime: int | None = None,
) -> list[pathlib.Path]:
    """Return all candidates ordered by descending score using the same heuristics."""
    candidates = [pathlib.Path(p) for p, meta in db.items() if meta.get("name") == file_name]
    if not candidates:
        return []

    def score(candidate: pathlib.Path) -> tuple[int, int, int, int, int, int, int, int]:
        meta = db.get(str(candidate), {})
        length = _length_score(target_length, candidate)
        size = _size_score(target_size, meta)
        mtime = _mtime_score(target_mtime, meta)
        suffix, parent, prefix, overlap, neg_len = _folder_scores(original_path, candidate)
        return (length, size, mtime, suffix, parent, prefix, overlap, neg_len)

    return sorted(candidates, key=score, reverse=True)


def is_factory_pack_path(path: pathlib.Path) -> bool:
    """Return True if path points inside Ableton factory content."""
    s = path.as_posix()
    return "/Resources/Builtin/Samples" in s or "Ableton/Factory Packs" in s
