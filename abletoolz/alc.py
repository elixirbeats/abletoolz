"""abletoolz.alc — author Ableton Live Clip (.alc) files: gzip + ElementTree.

An ``.alc`` file is a miniature ``.als``: the full ``<Ableton><LiveSet>...`` document,
containing exactly one ``<AudioClip>`` buried under
``Tracks/AudioTrack/DeviceChain/MainSequencer/ClipSlotList/ClipSlot``. Live 12 honors the
clip's warp markers, clip range (``CurrentStart``/``CurrentEnd``), and ``Loop`` block
verbatim on drag-in from the browser — verified against a hand-spliced 172 BPM
grid (``test/alc_fixtures/audeka_gridfix_verified.alc``).

Beat convention (user-decided): 1.1.1 (beat 0) is the song grid start (``grid_start_seconds``),
not the drop. A track's drop then lands at
``(drop_seconds - grid_start_seconds) * bpm / 60``, which should sit on a 4/4 bar boundary —
:meth:`AlcClip.drop_alignment` is a per-track consistency check on that.

Serialization mirrors the idioms in :mod:`abletoolz.live_set` (gzip open/save,
``ElementTree``, header + body + trailing newline) without inheriting any of its
set-specific parsing logic.
"""

from __future__ import annotations

import gzip
import os
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

_XML_HEADER = b'<?xml version="1.0" encoding="UTF-8"?>\n'
_XML_FOOTER = b"\n"


class AlcError(Exception):
    """Raised for malformed, missing, or unsupported ``.alc`` content."""


# Two warp markers closer together than this are the same instant as far as a
# grid is concerned. Well under a millisecond, so it cannot merge two markers a
# person meant to keep apart, while still catching a landmark authored at a time
# that only differs from an existing marker by float round-tripping through XML.
_MARKER_EPSILON_S = 1e-6


@dataclass(slots=True, frozen=True)
class WarpMarker:
    """One ``<WarpMarker>`` record: an audio time (seconds) pinned to a beat position."""

    marker_id: int
    sec_time: float
    beat_time: float


@dataclass(slots=True, frozen=True)
class DropAlignment:
    """Where a drop cue lands relative to an authored grid.

    ``residual_beats`` is the signed distance (in beats) from the drop to the nearest 4/4
    bar boundary; it should be ~0 for a correctly gridded, on-beat drop.
    """

    beats: float
    bars: int
    residual_beats: float


def _beats_from_seconds(seconds_delta: float, bpm: float) -> float:
    """``seconds_delta`` converted to beats at a constant ``bpm``.

    Order of operations matters for bit-exact reproduction of Live's own arithmetic
    (``x * bpm / 60.0``, evaluated left-to-right) — do not refactor into
    ``x * (bpm / 60.0)``, which rounds differently.
    """
    return seconds_delta * bpm / 60.0


def _format_number(value: float) -> str:
    """Render a float the way Live's XML serializer does: bare integers, else ``repr``."""
    if value == int(value):
        return str(int(value))
    return repr(value)


def _req_attr(element: ET.Element, attr: str) -> str:
    """Return a required attribute value, raising :class:`AlcError` if absent."""
    value = element.get(attr)
    if value is None:
        raise AlcError(f"<{element.tag}> is missing required attribute {attr!r}")
    return value


def _get(element: ET.Element, tag: str) -> ET.Element:
    """Return a required direct child, raising :class:`AlcError` if absent."""
    found = element.find(tag)
    if found is None:
        raise AlcError(f"<{element.tag}> is missing required child <{tag}>")
    return found


def _get_value(element: ET.Element, tag: str) -> str:
    """Return a required child's ``Value`` attribute."""
    return _req_attr(_get(element, tag), "Value")


def _set_value(element: ET.Element, tag: str, value: str) -> None:
    """Set a required child's ``Value`` attribute."""
    _get(element, tag).set("Value", value)


def _find_audio_clip(root: ET.Element) -> ET.Element:
    """Locate the (first) ``<AudioClip>`` in an .alc document."""
    clip = root.find(".//AudioClip")
    if clip is None:
        raise AlcError("No <AudioClip> found in .alc document")
    return clip


def _read_markers(clip: ET.Element) -> list[WarpMarker]:
    """Read ``<WarpMarkers>`` children, sorted by ``SecTime``."""
    warp_markers_el = _get(clip, "WarpMarkers")
    markers = [
        WarpMarker(
            marker_id=int(_req_attr(el, "Id")),
            sec_time=float(_req_attr(el, "SecTime")),
            beat_time=float(_req_attr(el, "BeatTime")),
        )
        for el in warp_markers_el.findall("WarpMarker")
    ]
    markers.sort(key=lambda m: m.sec_time)
    return markers


def _write_two_markers(
    clip: ET.Element, anchor_sec: float, anchor_beat: float, end_sec: float, end_beat: float
) -> None:
    """Write a constant-tempo two-marker grid, reusing existing marker Ids where possible.

    If the clip already has exactly two ``<WarpMarker>`` elements, their ``Id`` attributes
    are preserved and only ``SecTime``/``BeatTime`` are rewritten (this is what reproduces
    the Live-verified ``audeka_gridfix_verified.alc`` fixture byte-for-byte in the marker
    fields). Otherwise the existing markers are discarded and replaced with two fresh ones
    (``Id="0"``, ``Id="1"``).
    """
    warp_markers_el = _get(clip, "WarpMarkers")
    existing = warp_markers_el.findall("WarpMarker")
    if len(existing) == 2:
        anchor_el, end_el = existing
    else:
        for el in existing:
            warp_markers_el.remove(el)
        anchor_el = ET.SubElement(warp_markers_el, "WarpMarker")
        anchor_el.set("Id", "0")
        end_el = ET.SubElement(warp_markers_el, "WarpMarker")
        end_el.set("Id", "1")
    anchor_el.set("SecTime", _format_number(anchor_sec))
    anchor_el.set("BeatTime", _format_number(anchor_beat))
    end_el.set("SecTime", _format_number(end_sec))
    end_el.set("BeatTime", _format_number(end_beat))


class AlcClip:
    """A parsed ``.alc`` (Ableton Live Clip) document, wrapping its single ``<AudioClip>``."""

    def __init__(self, root: ET.Element) -> None:
        """Wrap an already-parsed ``.alc`` document root (``<Ableton>``)."""
        self._root = root
        self._clip = _find_audio_clip(root)

    @property
    def root(self) -> ET.Element:
        """The document root element (``<Ableton>``)."""
        return self._root

    @property
    def clip(self) -> ET.Element:
        """The wrapped ``<AudioClip>`` element, for advanced/uncovered field access."""
        return self._clip

    # ── Load / save ──────────────────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path) -> AlcClip:
        """Load and parse a gzip-compressed ``.alc`` file."""
        data = path.read_bytes()
        if data[:2] != b"\x1f\x8b":
            raise AlcError(f"{path}: not gzip-compressed, not a valid .alc file")
        xml_text = gzip.decompress(data).decode("utf-8")
        root = ET.fromstring(xml_text)
        return cls(root)

    def to_xml_bytes(self) -> bytes:
        """Serialize the document to XML bytes (header + body + trailing newline)."""
        xml_bytes = ET.tostring(self._root, encoding="utf-8")
        assert isinstance(xml_bytes, bytes)  # ET.tostring(..., encoding=str) always returns bytes
        return _XML_HEADER + xml_bytes + _XML_FOOTER

    def save(self, path: Path) -> None:
        """Gzip-compress and write the clip to ``path``, atomically (write-temp + rename).

        Uses :func:`gzip.compress` rather than :func:`gzip.open`: the latter embeds the
        (temp) filename via the gzip FNAME header flag, and Live's browser refuses to
        index .alc files whose gzip FLG byte differs from Ableton's own ``0x00``.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(path.name + ".tmp")
        tmp_path.write_bytes(gzip.compress(self.to_xml_bytes()))
        os.replace(tmp_path, path)

    # ── Name / color ─────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        """The clip's ``<Name>`` (shown in Live's clip view and on Push 3's display)."""
        return _get_value(self._clip, "Name")

    @name.setter
    def name(self, value: str) -> None:
        _set_value(self._clip, "Name", value)

    @property
    def color_index(self) -> int:
        """The clip's ``<Color>`` index (Live's built-in clip color palette index)."""
        return int(_get_value(self._clip, "Color"))

    @color_index.setter
    def color_index(self, value: int) -> None:
        _set_value(self._clip, "Color", str(value))

    # ── Warp markers ─────────────────────────────────────────────────────────

    @property
    def markers(self) -> list[WarpMarker]:
        """The clip's warp markers, sorted by ``SecTime``."""
        return _read_markers(self._clip)

    # ── Grid authoring ───────────────────────────────────────────────────────

    def set_grid(self, *, bpm: float, grid_start_seconds: float, duration_seconds: float) -> None:
        """Author a two-marker constant-tempo grid covering the full audio file.

        Beat 0 (1.1.1) lands at ``grid_start_seconds`` (the song grid start, per the
        user's convention — NOT the drop). The clip's range is set to run from a
        (generally negative) beat position at audio time 0 through the beat position of
        ``duration_seconds``, i.e. the whole file. Sets ``IsWarped`` true and updates
        ``CurrentStart``/``CurrentEnd`` plus the ``Loop`` block (``LoopStart``, ``LoopEnd``,
        ``OutMarker``, ``HiddenLoopStart``, ``HiddenLoopEnd``) to match.

        Args:
            bpm: exact constant tempo of the track.
            grid_start_seconds: audio time of the song's grid start (beat 0), >= 0.
            duration_seconds: total track length in seconds; must exceed ``grid_start_seconds``.
        """
        if bpm <= 0.0:
            raise ValueError(f"bpm must be positive, got {bpm}")
        if grid_start_seconds < 0.0:
            raise ValueError(f"grid_start_seconds must be >= 0, got {grid_start_seconds}")
        if duration_seconds <= grid_start_seconds:
            raise ValueError(
                f"duration_seconds ({duration_seconds}) must exceed grid_start_seconds ({grid_start_seconds})"
            )

        clip_start_beats = -_beats_from_seconds(grid_start_seconds, bpm)
        clip_end_beats = _beats_from_seconds(duration_seconds - grid_start_seconds, bpm)

        _write_two_markers(self._clip, grid_start_seconds, 0.0, duration_seconds, clip_end_beats)
        _set_value(self._clip, "IsWarped", "true")

        _set_value(self._clip, "CurrentStart", _format_number(clip_start_beats))
        _set_value(self._clip, "CurrentEnd", _format_number(clip_end_beats))

        loop = _get(self._clip, "Loop")
        _set_value(loop, "LoopStart", _format_number(clip_start_beats))
        _set_value(loop, "LoopEnd", _format_number(clip_end_beats))
        _set_value(loop, "OutMarker", _format_number(clip_end_beats))
        _set_value(loop, "HiddenLoopStart", _format_number(clip_start_beats))
        _set_value(loop, "HiddenLoopEnd", _format_number(clip_end_beats))

    def add_grid_marker(self, seconds: float, *, beat_time: float | None = None) -> None:
        """Insert an extra warp marker at ``seconds``, colinear with the existing grid.

        The two-marker grid already fixes the tempo; extra markers are landmarks —
        e.g. a visible, grabbable marker on the drop. When ``beat_time`` is omitted it
        is computed from the grid's slope; pass it explicitly when the exact beat is
        known (e.g. the drop at beat 128) to keep the displayed position clean.
        Markers are kept sorted by ``SecTime``; the new marker gets a fresh ``Id``.
        """
        markers = self.markers
        if len(markers) < 2:
            raise AlcError("add_grid_marker requires an existing grid; call set_grid first")
        # A landmark on top of an existing marker is not a landmark, and a pair
        # of markers at one instant is a grid Live cannot interpret. Happens
        # when the drop falls on the grid anchor.
        if any(abs(m.sec_time - seconds) < _MARKER_EPSILON_S for m in markers):
            return
        first, last = markers[0], markers[-1]
        if beat_time is None:
            slope = (last.beat_time - first.beat_time) / (last.sec_time - first.sec_time)
            beat_time = first.beat_time + (seconds - first.sec_time) * slope

        warp_markers_el = _get(self._clip, "WarpMarkers")
        elements = warp_markers_el.findall("WarpMarker")
        next_id = max(int(_req_attr(el, "Id")) for el in elements) + 1
        new_el = ET.Element("WarpMarker")
        new_el.set("Id", str(next_id))
        new_el.set("SecTime", _format_number(seconds))
        new_el.set("BeatTime", _format_number(beat_time))

        insert_at = sum(1 for el in elements if float(_req_attr(el, "SecTime")) < seconds)
        warp_markers_el.insert(insert_at, new_el)

    def set_clip_start(self, seconds: float) -> None:
        """Set the clip start (playhead start) to an arbitrary audio time.

        Converts ``seconds`` to beats using the clip's existing warp grid (call
        :meth:`set_grid` first). For non-looping clips Live enforces
        ``start == loopStart`` (and resolves any mismatch in favor of the loop brace —
        observed in Live as a start marker sitting slightly before 1.1.1), so
        ``LoopStart`` is kept in sync with ``CurrentStart`` when ``LoopOn`` is false.
        This is how hotcue-variant clips are authored: same file and grid, a different
        start point.
        """
        markers = self.markers
        if len(markers) < 2:
            raise AlcError("set_clip_start requires an existing 2-marker grid; call set_grid first")
        # First and LAST, not first and second. Every marker on this grid is
        # colinear, so any pair gives the same slope -- but only the outermost
        # pair is guaranteed to span a non-zero interval. Taking markers[1]
        # broke on tracks whose drop sits on the grid anchor (a drop on beat one
        # is ordinary in house), because the landmark marker added at the drop
        # then shares the anchor's SecTime and the slope divides by zero.
        anchor, end = markers[0], markers[-1]
        if end.sec_time == anchor.sec_time:
            raise AlcError("degenerate warp grid: both markers share the same SecTime")
        slope = (end.beat_time - anchor.beat_time) / (end.sec_time - anchor.sec_time)
        beat = anchor.beat_time + (seconds - anchor.sec_time) * slope
        _set_value(self._clip, "CurrentStart", _format_number(beat))
        loop = _get(self._clip, "Loop")
        if _get_value(loop, "LoopOn") == "false":
            _set_value(loop, "LoopStart", _format_number(beat))
            _set_value(loop, "HiddenLoopStart", _format_number(beat))

    def set_view_window(self, start_seconds: float, end_seconds: float, margin_bars: float) -> None:
        """Set the clip view's visible window to cover `start_seconds` through
        `end_seconds`, plus `margin_bars` of headroom on the right.

        Live stores this as ``ScrollerTimePreserver`` (Left/RightTime, in beats).
        The template's own window runs far past the end of any real clip, so a
        generated clip opens fully zoomed out with the grid at the cue too small
        to read. What a person opens one of these to see is the start cue, the
        drop, and a little of what follows -- so the window is framed on exactly
        that. Written in the clip's own beat space via the warp grid, so it lands
        on the same bar lines at any tempo.
        """
        markers = self.markers
        if len(markers) < 2:
            raise AlcError("set_view_window requires an existing 2-marker grid")
        # Outermost pair, for the reason given in set_clip_start.
        anchor_marker, end_marker = markers[0], markers[-1]
        if end_marker.sec_time == anchor_marker.sec_time:
            raise AlcError("degenerate warp grid: both markers share the same SecTime")
        slope = (end_marker.beat_time - anchor_marker.beat_time) / (end_marker.sec_time - anchor_marker.sec_time)

        def to_beats(seconds: float) -> float:
            return anchor_marker.beat_time + (seconds - anchor_marker.sec_time) * slope

        scroller = self._clip.find("ScrollerTimePreserver")
        if scroller is None:
            return
        left = to_beats(start_seconds)
        right = to_beats(end_seconds) + margin_bars * 4.0
        # A drop-variant clip starts AT the drop, so both ends collapse to one
        # point; give it the margin as its whole span instead of a zero window.
        if right - left < margin_bars * 4.0:
            right = left + margin_bars * 4.0
        _set_value(scroller, "LeftTime", _format_number(left))
        _set_value(scroller, "RightTime", _format_number(right))

    @staticmethod
    def drop_alignment(*, bpm: float, grid_start_seconds: float, drop_seconds: float) -> DropAlignment:
        """Where a drop cue lands relative to the grid: a per-track consistency check.

        For a correctly gridded 4/4 track, the drop should sit almost exactly on a bar
        boundary — ``residual_beats`` near 0. Large residuals indicate the external
        analyzer's ``grid_start``/``drop``/``bpm`` values disagree with each other.
        """
        beats = (drop_seconds - grid_start_seconds) * bpm / 60.0
        bars = round(beats / 4.0)
        residual_beats = beats - bars * 4.0
        return DropAlignment(beats=beats, bars=bars, residual_beats=residual_beats)

    # ── Sample retargeting ───────────────────────────────────────────────────

    def retarget_sample(self, absolute_path: Path, relative_path: Path) -> None:
        """Point the clip's ``SampleRef`` at a different audio file.

        ``relative_path`` is REQUIRED and must resolve to the same file under the
        reference root implied by the existing ``RelativePathType`` (``6`` = user
        library in the fixtures). Live resolves ``RelativePath`` FIRST: leaving a
        stale one in place makes Live silently load the OLD sample even though
        ``Path`` points elsewhere (observed in Live — the clip kept playing the
        old audio). Cross-volume targets (e.g. UNC) have no valid relative path;
        copy the audio under the library and reference the copy instead.

        Also updates ``OriginalFileSize`` when ``absolute_path`` exists (the field
        the ``.asd`` format's stale-file detection keys on — see
        ``abletoolz/asd/FORMAT.md``). ``OriginalCrc`` is left untouched: whether
        Live's ``.alc`` loader treats a stale CRC differently is unverified.
        """
        file_ref = _get(_get(self._clip, "SampleRef"), "FileRef")
        _set_value(file_ref, "Path", absolute_path.as_posix())
        _set_value(file_ref, "RelativePath", relative_path.as_posix())
        if absolute_path.exists():
            _set_value(file_ref, "OriginalFileSize", str(absolute_path.stat().st_size))
