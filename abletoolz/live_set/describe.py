"""LLM-facing describe surface: a tiered, deduplicated view of a set.

A real arrangement measured before this existed: 16 near-identical 32-note
clips (the same pattern re-placed across a track) cost ~90% wasted tokens
under a raw per-clip dump, and audio clips were invisible to the domain API
at all, forcing a raw XML walk just to map the arrangement. This module
answers both: clip *content* is deduplicated into a ``patterns`` table keyed
by exact (length, notes) identity, clip *placement* references a pattern by
id, and audio clips are read through ``Clips.audio()`` like any other clip.

Three levels tier how much of that content ships. STRUCTURE is tracks and
per-track clip counts only -- enough to see the shape of a set. PATTERNS
adds the deduplicated note content and every placement, with per-note
nuance (probability, velocity deviation, disabled) silently dropped.
FULL keeps that nuance, at the cost of patterns that would otherwise
dedupe no longer doing so once one note's nuance differs.

``set.describe(level)`` returns a typed ``DescribeDocument`` -- frozen
dataclasses throughout, the same shape ``MidiClipRef``/``TrackDevices``
already use elsewhere in this package. The dict/JSON conversion happens
exactly once, at the serialization edge (``to_wire``/``describe_json``);
nothing upstream of that indexes into a bare dict.

Track ids and clip-to-track association both go through ``Clips.track_of``,
identity-based rather than name-based: duplicate track names are real (a
real set can carry two tracks named the same), and a describe() that
silently merged them under one name would be worse than the raw XML walk
it replaces.
"""

from __future__ import annotations

import dataclasses
import enum
import json
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

from abletoolz.live_set.clips import AudioClipRef, ClipLocation, MidiClipRef, NoteArray, encode_note
from abletoolz.live_set.devices import TrackDevices
from abletoolz.live_set.tracks import AbletonTrack
from abletoolz.misc import get_element

if TYPE_CHECKING:
    from abletoolz.live_set.document import AbletonSet

# Live's own track tags, mapped to the schema's short names. MasterTrack is
# pre-12, MainTrack 12+ (schema.tag("master_track", ...)); both mean "main".
_TRACK_TYPE = {
    "MidiTrack": "midi",
    "AudioTrack": "audio",
    "GroupTrack": "group",
    "ReturnTrack": "return",
    "MasterTrack": "main",
    "MainTrack": "main",
}

# The main track carries no Id attribute in any corpus fixture, across every
# version. -1 is Live's own convention for "no id" (TrackGroupId reads -1 on
# an ungrouped track), reused here rather than inventing a new sentinel.
_MAIN_TRACK_ID = -1


class DescribeLevel(enum.StrEnum):
    """How much of a set's content ``Describe`` emits."""

    STRUCTURE = "structure"
    PATTERNS = "patterns"
    FULL = "full"


# --- the document, typed throughout ------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class SetInfo:
    creator: str
    major: int
    bpm: float


@dataclasses.dataclass(frozen=True, slots=True)
class TrackClipCounts:
    session: int
    arrangement: int


@dataclasses.dataclass(frozen=True, slots=True)
class TrackEntry:
    id: int
    name: str
    type: str
    group_id: int | None
    chain: tuple[str, ...]
    clips: TrackClipCounts | None = None  # STRUCTURE level only


@dataclasses.dataclass(frozen=True, slots=True)
class Pattern:
    id: str
    length: float | None
    notes: tuple[NoteArray, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class SessionPlacement:
    """A MIDI clip in a session slot. Audio session clips have no placement
    shape in this document -- see the module docstring's dedup discussion."""

    track_id: int
    slot: int
    name: str
    pattern: str


@dataclasses.dataclass(frozen=True, slots=True)
class ArrangementMidiPlacement:
    track_id: int
    start: float
    name: str
    pattern: str


@dataclasses.dataclass(frozen=True, slots=True)
class ArrangementAudioPlacement:
    track_id: int
    start: float
    length: float | None
    sample: str


type ArrangementPlacement = ArrangementMidiPlacement | ArrangementAudioPlacement


@dataclasses.dataclass(frozen=True, slots=True)
class DescribeDocument:
    """The whole ``describe()`` result. ``None`` fields are omitted at STRUCTURE."""

    set: SetInfo
    tracks: tuple[TrackEntry, ...]
    patterns: tuple[Pattern, ...] | None = None
    session: tuple[SessionPlacement, ...] | None = None
    arrangement: tuple[ArrangementPlacement, ...] | None = None


def _round(value: float) -> float:
    return round(value, 4)


def _track_id(track: AbletonTrack) -> int:
    return _MAIN_TRACK_ID if track.id is None else int(track.id)


def _group_id(track: AbletonTrack) -> int | None:
    return None if track.group_id in (None, "-1") else int(track.group_id)


def _session_slot_index(track: AbletonTrack, clip_element: ET.Element) -> int:
    """Which session slot on ``track`` holds ``clip_element``, by identity."""
    for index, slot in enumerate(track.track_root.iterfind("DeviceChain/MainSequencer/ClipSlotList/ClipSlot")):
        value = get_element(slot, "ClipSlot.Value")
        if any(child is clip_element for child in value):
            return index
    raise ValueError("Session clip not found in its own track's slot list")


def _freeze_notes(notes: tuple[NoteArray, ...]) -> tuple[object, ...]:
    """A hashable form of an encoded note list -- dicts become sorted item tuples."""
    return tuple(
        tuple(tuple(sorted(item.items())) if isinstance(item, dict) else item for item in note) for note in notes
    )


class _PatternTable:
    """First-seen-order pattern registry: (length, notes) identity -> "pN"."""

    def __init__(self) -> None:
        self._patterns: list[Pattern] = []
        self._by_signature: dict[tuple[object, ...], str] = {}

    def id_for(self, clip: MidiClipRef, *, extended: bool) -> str:
        length = None if clip.length is None else _round(clip.length)
        notes = tuple(encode_note(note, extended=extended) for note in clip.notes)
        signature = (length, _freeze_notes(notes))
        pattern_id = self._by_signature.get(signature)
        if pattern_id is None:
            pattern_id = f"p{len(self._patterns)}"
            self._by_signature[signature] = pattern_id
            self._patterns.append(Pattern(id=pattern_id, length=length, notes=notes))
        return pattern_id

    def patterns(self) -> tuple[Pattern, ...]:
        return tuple(self._patterns)


class Describe:
    """Builds the tiered ``DescribeDocument`` for one set. Callable: ``set.describe(level)``."""

    def __init__(self, live_set: AbletonSet) -> None:
        self._set = live_set

    def __call__(self, level: DescribeLevel = DescribeLevel.STRUCTURE) -> DescribeDocument:
        tracks = self._set.tracks.load()
        inventory = self._set.devices.inventory()
        midi_clips = self._set.clips.midi()
        audio_clips = self._set.clips.audio()

        set_info = SetInfo(
            creator=self._set.version or "",
            major=self._set.version_tuple[0],
            bpm=_round(self._set.transport.bpm()),
        )
        track_entries = self._track_entries(tracks, inventory, midi_clips, audio_clips, level)
        if level is DescribeLevel.STRUCTURE:
            return DescribeDocument(set=set_info, tracks=track_entries)

        table = _PatternTable()
        extended = level is DescribeLevel.FULL
        session: list[SessionPlacement] = []
        arrangement: list[ArrangementPlacement] = []

        for clip in midi_clips:
            pattern_id = table.id_for(clip, extended=extended)
            track = self._set.clips.track_of(clip.clip_element)
            if clip.location is ClipLocation.SESSION:
                session.append(
                    SessionPlacement(
                        track_id=_track_id(track),
                        slot=_session_slot_index(track, clip.clip_element),
                        name=clip.name,
                        pattern=pattern_id,
                    )
                )
            else:
                arrangement.append(
                    ArrangementMidiPlacement(
                        track_id=_track_id(track),
                        start=_round(clip.start_time),
                        name=clip.name,
                        pattern=pattern_id,
                    )
                )

        for audio_clip in audio_clips:
            if audio_clip.location is not ClipLocation.ARRANGEMENT:
                continue
            track = self._set.clips.track_of(audio_clip.clip_element)
            arrangement.append(
                ArrangementAudioPlacement(
                    track_id=_track_id(track),
                    start=_round(audio_clip.start_time),
                    length=None if audio_clip.length is None else _round(audio_clip.length),
                    sample=audio_clip.sample_name,
                )
            )

        return DescribeDocument(
            set=set_info,
            tracks=track_entries,
            patterns=table.patterns(),
            session=tuple(session),
            arrangement=tuple(arrangement),
        )

    def _track_entries(
        self,
        tracks: list[AbletonTrack],
        inventory: list[TrackDevices],
        midi_clips: list[MidiClipRef],
        audio_clips: list[AudioClipRef],
        level: DescribeLevel,
    ) -> tuple[TrackEntry, ...]:
        clip_counts: dict[int, TrackClipCounts] | None = None
        if level is DescribeLevel.STRUCTURE:
            session_counts = dict.fromkeys((id(track) for track in tracks), 0)
            arrangement_counts = dict.fromkeys((id(track) for track in tracks), 0)
            all_clips: list[MidiClipRef | AudioClipRef] = [*midi_clips, *audio_clips]
            for clip in all_clips:
                track = self._set.clips.track_of(clip.clip_element)
                key = id(track)
                if key not in session_counts:
                    continue
                if clip.location is ClipLocation.SESSION:
                    session_counts[key] += 1
                else:
                    arrangement_counts[key] += 1
            clip_counts = {
                key: TrackClipCounts(session=session_counts[key], arrangement=arrangement_counts[key])
                for key in session_counts
            }

        entries: list[TrackEntry] = []
        for track, chain in zip(tracks, inventory[: len(tracks)], strict=True):
            entries.append(
                TrackEntry(
                    id=_track_id(track),
                    name=track.name,
                    type=_TRACK_TYPE[track.type],
                    group_id=_group_id(track),
                    chain=tuple(device.display_name for device in chain.devices),
                    clips=None if clip_counts is None else clip_counts[id(track)],
                )
            )

        main = inventory[-1]
        entries.append(
            TrackEntry(
                id=_MAIN_TRACK_ID,
                name=main.track_name,
                type=_TRACK_TYPE[main.track_type],
                group_id=None,
                chain=tuple(device.display_name for device in main.devices),
                clips=None if clip_counts is None else TrackClipCounts(session=0, arrangement=0),
            )
        )
        return tuple(entries)


# --- serialization edge: dataclasses -> the wire JSON shape -----------------


def _track_wire(entry: TrackEntry) -> dict[str, object]:
    wire: dict[str, object] = {
        "id": entry.id,
        "name": entry.name,
        "type": entry.type,
        "group_id": entry.group_id,
        "chain": list(entry.chain),
    }
    if entry.clips is not None:
        wire["clips"] = {"session": entry.clips.session, "arrangement": entry.clips.arrangement}
    return wire


def _pattern_wire(pattern: Pattern) -> dict[str, object]:
    return {"length": pattern.length, "notes": [list(note) for note in pattern.notes]}


def _placement_wire(placement: ArrangementPlacement | SessionPlacement) -> dict[str, object]:
    if isinstance(placement, SessionPlacement):
        return {
            "track_id": placement.track_id,
            "slot": placement.slot,
            "name": placement.name,
            "pattern": placement.pattern,
        }
    if isinstance(placement, ArrangementMidiPlacement):
        return {
            "track_id": placement.track_id,
            "start": placement.start,
            "name": placement.name,
            "pattern": placement.pattern,
        }
    return {
        "track_id": placement.track_id,
        "start": placement.start,
        "length": placement.length,
        "sample": placement.sample,
    }


def to_wire(document: DescribeDocument) -> dict[str, object]:
    """``DescribeDocument`` -> the exact JSON shape the schema specifies."""
    wire: dict[str, object] = {
        "set": {"creator": document.set.creator, "major": document.set.major, "bpm": document.set.bpm},
        "tracks": [_track_wire(entry) for entry in document.tracks],
    }
    if document.patterns is not None:
        wire["patterns"] = {pattern.id: _pattern_wire(pattern) for pattern in document.patterns}
    if document.session is not None:
        wire["session"] = [_placement_wire(placement) for placement in document.session]
    if document.arrangement is not None:
        wire["arrangement"] = [_placement_wire(placement) for placement in document.arrangement]
    return wire


def describe_json(live_set: AbletonSet, level: DescribeLevel = DescribeLevel.STRUCTURE) -> str:
    """JSON string for ``live_set.describe(level)``, compact and in the emitted key order."""
    return json.dumps(to_wire(live_set.describe(level)), separators=(",", ":"))
