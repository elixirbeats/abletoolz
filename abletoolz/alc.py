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

The container -- gzip with Live's header, the XML prolog -- is
:mod:`abletoolz.formats.live_document`, shared with ``.als``; this module is only what a
clip means.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from abletoolz.errors import ElementNotFound
from abletoolz.formats import FormatError, live_document
from abletoolz.live_set.sample_ref import SampleRef
from abletoolz.live_set.settings import Setting, find, resolve
from abletoolz.live_set.xml_edit import number_value
from abletoolz.schema import Attr, Tag
from abletoolz.schema.objects import Clip
from abletoolz.schema.versioning import Version, parse_creator


class AlcError(FormatError):
    """Raised for malformed, missing, or unsupported ``.alc`` content."""


# Two warp markers closer than this are one instant: well under a millisecond,
# so it never merges markers a person meant apart, yet catches a landmark that
# differs from an existing marker only by float round-tripping through XML.
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


def _grid_beat(markers: list[WarpMarker], seconds: float) -> float:
    """The beat at audio time ``seconds`` on the line through the grid's outermost markers.

    Outermost, not the first two: every marker is colinear, but only the outer
    pair is sure to span a non-zero interval. A drop landmark on beat one shares
    the anchor's ``SecTime``, which is ordinary in house.
    """
    anchor, end = markers[0], markers[-1]
    if end.sec_time == anchor.sec_time:
        raise AlcError("degenerate warp grid: both markers share the same SecTime")
    slope = (end.beat_time - anchor.beat_time) / (end.sec_time - anchor.sec_time)
    return anchor.beat_time + (seconds - anchor.sec_time) * slope


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


def _find_audio_clip(root: ET.Element) -> ET.Element:
    """Locate the (first) ``<AudioClip>`` in an .alc document."""
    clip = root.find(".//" + Tag.AUDIO_CLIP)
    if clip is None:
        raise AlcError("No <AudioClip> found in .alc document")
    return clip


def _read_markers(clip: ET.Element) -> list[WarpMarker]:
    """Read ``<WarpMarkers>`` children, sorted by ``SecTime``."""
    warp_markers_el = _get(clip, Tag.WARP_MARKERS)
    markers = [
        WarpMarker(
            marker_id=int(_req_attr(el, Attr.ID)),
            sec_time=float(_req_attr(el, Attr.SEC_TIME)),
            beat_time=float(_req_attr(el, Attr.BEAT_TIME)),
        )
        for el in warp_markers_el.findall(Tag.WARP_MARKER)
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
    warp_markers_el = _get(clip, Tag.WARP_MARKERS)
    existing = warp_markers_el.findall(Tag.WARP_MARKER)
    if len(existing) == 2:
        anchor_el, end_el = existing
    else:
        for el in existing:
            warp_markers_el.remove(el)
        anchor_el = ET.SubElement(warp_markers_el, Tag.WARP_MARKER)
        anchor_el.set(Attr.ID, "0")
        end_el = ET.SubElement(warp_markers_el, Tag.WARP_MARKER)
        end_el.set(Attr.ID, "1")
    anchor_el.set(Attr.SEC_TIME, number_value(anchor_sec))
    anchor_el.set(Attr.BEAT_TIME, number_value(anchor_beat))
    end_el.set(Attr.SEC_TIME, number_value(end_sec))
    end_el.set(Attr.BEAT_TIME, number_value(end_beat))


class AlcClip:
    """A parsed ``.alc`` (Ableton Live Clip) document, wrapping its single ``<AudioClip>``."""

    def __init__(self, root: ET.Element) -> None:
        """Wrap an already-parsed ``.alc`` document root (``<Ableton>``)."""
        self._root = root
        self._clip = _find_audio_clip(root)
        creator = root.get(Attr.CREATOR)
        if creator is None:
            raise AlcError("the .alc document has no Creator, so its Live version is unknown")
        self._version = parse_creator(creator)

    @property
    def version(self) -> Version:
        """The Live version that wrote the clip document."""
        return self._version

    def setting(self, member: Clip) -> Setting:
        """One of the clip's settings, where this document's Live version keeps it."""
        try:
            return resolve(self._clip, member, self._version, label="AudioClip")
        except ElementNotFound as error:
            raise AlcError(str(error)) from error

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
        """Load and parse an ``.alc`` file."""
        return cls(live_document.read(path))

    def to_xml_bytes(self) -> bytes:
        """Serialize the document to XML bytes (header + body + trailing newline)."""
        return live_document.to_xml_bytes(self._root)

    def save(self, path: Path) -> None:
        """Write the clip to ``path`` atomically, with the gzip header Live's browser insists on."""
        path.parent.mkdir(parents=True, exist_ok=True)
        live_document.write(path, live_document.encode(self._root))

    # ── Name / color ─────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        """The clip's ``<Name>`` (shown in Live's clip view and on Push 3's display)."""
        return self.setting(Clip.NAME).raw

    @name.setter
    def name(self, value: str) -> None:
        self.setting(Clip.NAME).set(value)

    @property
    def color_index(self) -> int:
        """The clip's color index (Live's built-in clip color palette index)."""
        return int(self.setting(Clip.COLOR).raw)

    @color_index.setter
    def color_index(self, value: int) -> None:
        self.setting(Clip.COLOR).set(value)

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
        self.setting(Clip.IS_WARPED).set(True)
        for member, beats in (
            (Clip.CURRENT_START, clip_start_beats),
            (Clip.CURRENT_END, clip_end_beats),
            (Clip.LOOP_START, clip_start_beats),
            (Clip.LOOP_END, clip_end_beats),
            (Clip.OUT_MARKER, clip_end_beats),
            (Clip.HIDDEN_LOOP_START, clip_start_beats),
            (Clip.HIDDEN_LOOP_END, clip_end_beats),
        ):
            self.setting(member).set(beats)

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

        warp_markers_el = _get(self._clip, Tag.WARP_MARKERS)
        elements = warp_markers_el.findall(Tag.WARP_MARKER)
        next_id = max(int(_req_attr(el, Attr.ID)) for el in elements) + 1
        new_el = ET.Element(Tag.WARP_MARKER)
        new_el.set(Attr.ID, str(next_id))
        new_el.set(Attr.SEC_TIME, number_value(seconds))
        new_el.set(Attr.BEAT_TIME, number_value(beat_time))

        insert_at = sum(1 for el in elements if float(_req_attr(el, Attr.SEC_TIME)) < seconds)
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
        beat = _grid_beat(markers, seconds)
        self.setting(Clip.CURRENT_START).set(beat)
        if self.setting(Clip.LOOP_ON).value is False:
            self.setting(Clip.LOOP_START).set(beat)
            self.setting(Clip.HIDDEN_LOOP_START).set(beat)

    def set_view_window(self, start_seconds: float, end_seconds: float, margin_bars: float) -> None:
        """Set the clip view's visible window to cover `start_seconds` through
        `end_seconds`, plus `margin_bars` of headroom on the right.

        Live stores this as ``ScrollerTimePreserver`` (Left/RightTime, in beats).
        The template's window runs far past any real clip, so a generated clip
        would open zoomed out too far to read the grid at the cue; the window is
        framed on the start cue, the drop and a little after. Written in the
        clip's own beat space via the warp grid, so it lands on the same bar
        lines at any tempo.
        """
        markers = self.markers
        if len(markers) < 2:
            raise AlcError("set_view_window requires an existing 2-marker grid")
        left = _grid_beat(markers, start_seconds)
        right = _grid_beat(markers, end_seconds) + margin_bars * 4.0
        left_time = find(self._clip, Clip.LEFT_TIME, self._version)
        right_time = find(self._clip, Clip.RIGHT_TIME, self._version)
        if left_time is None or right_time is None:
            return
        # A drop-variant clip starts AT the drop, so both ends collapse to one
        # point; give it the margin as its whole span instead of a zero window.
        if right - left < margin_bars * 4.0:
            right = left + margin_bars * 4.0
        left_time.set(left)
        right_time.set(right)

    @staticmethod
    def drop_alignment(*, bpm: float, grid_start_seconds: float, drop_seconds: float) -> DropAlignment:
        """Where a drop cue lands relative to the grid: a per-track consistency check.

        For a correctly gridded 4/4 track, the drop should sit almost exactly on a bar
        boundary — ``residual_beats`` near 0. Large residuals indicate the external
        analyzer's ``grid_start``/``drop``/``bpm`` values disagree with each other.
        """
        beats = _beats_from_seconds(drop_seconds - grid_start_seconds, bpm)
        bars = round(beats / 4.0)
        residual_beats = beats - bars * 4.0
        return DropAlignment(beats=beats, bars=bars, residual_beats=residual_beats)

    # ── Sample retargeting ───────────────────────────────────────────────────

    def retarget_sample(self, absolute_path: Path, relative_path: Path, *, kind: int | None = None) -> None:
        """Point the clip's ``SampleRef`` at a different audio file.

        ``relative_path`` is REQUIRED and must resolve to the same file under the
        reference root its ``RelativePathType`` implies: ``kind`` when given,
        else the clip's own (``6`` = user library in the fixtures). Live
        resolves ``RelativePath`` FIRST: leaving a stale one in place makes Live
        silently load the OLD sample even though ``Path`` points elsewhere
        (observed in Live — the clip kept playing the old audio). Cross-volume
        targets (e.g. UNC) have no valid relative path; copy the audio under the
        library and reference the copy instead.

        Also updates ``OriginalFileSize`` when ``absolute_path`` exists (the field
        the ``.asd`` format's stale-file detection keys on — see
        ``doc/ASD_FORMAT.md``). ``OriginalCrc`` is left untouched: whether
        Live's ``.alc`` loader treats a stale CRC differently is unverified.
        """
        try:
            reference = SampleRef(_get(self._clip, Tag.SAMPLE_REF), self._version)
            reference.point_relative(
                relative_path,
                reference.get_relative_type() if kind is None else kind,
                absolute=absolute_path,
                size=absolute_path.stat().st_size if absolute_path.exists() else None,
            )
        except ElementNotFound as error:
            raise AlcError(str(error)) from error
