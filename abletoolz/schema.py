"""Version-aware XML tag names.

Ableton renames tags across versions; every rename lives in this one table so
feature code asks for the logical name and never hardcodes a spelling. Only
renames that feature code touches get entries — the full census of known
renames is doc/VERSION_DIFFS.md.
"""

from __future__ import annotations

# Logical name -> (min_version, tag) entries, newest first, floor entry last.
_TAGS: dict[str, tuple[tuple[tuple[int, int, int], str], ...]] = {
    # Live 12 renamed the master track.
    "master_track": (
        ((12, 0, 0), "MainTrack"),
        ((0, 0, 0), "MasterTrack"),
    ),
    # Live 12 fixed the historical 'Sesstion' typo.
    "track_width": (
        ((12, 0, 0), "ViewStateSessionTrackWidth"),
        ((0, 0, 0), "ViewStateSesstionTrackWidth"),
    ),
    # Live 11.0 renamed ColorIndex to Color (11.0.0 itself already uses Color).
    "color": (
        ((11, 0, 0), "Color"),
        ((0, 0, 0), "ColorIndex"),
    ),
}


def tag(name: str, version: tuple[int, int, int]) -> str:
    """Return the XML tag for logical ``name`` in a set saved by ``version``."""
    entries = _TAGS[name]
    for min_version, tag_name in entries:
        if version >= min_version:
            return tag_name
    return entries[-1][1]
