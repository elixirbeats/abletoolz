"""MIDI clip and note reading and authoring, plus read-only audio clip discovery.

Notes live at ``MidiClip -> Notes -> KeyTracks -> KeyTrack(Id)``, where each
``KeyTrack`` carries the pitch (``MidiKey/@Value``) and its own ``Notes``
list of ``MidiNoteEvent`` elements. Attribute shape drifts by version: 9.x
carries ``IsEnabled`` and integer velocities; 11.0+ adds ``NoteId``,
``Probability``, ``VelocityDeviation`` and float velocities; 12.x keeps
``NoteId`` and writes the three expression attributes only when they leave
their defaults. Reading is tolerant (absent -> sensible default); writing
follows the set's own version exactly.

Clips live in two places, discovered via the same ``AbletonTrack`` walk
``Tracks`` already uses: session-view ``ClipSlot`` subtrees and arrangement
``DeviceChain/MainSequencer/ClipTimeable`` content. A third location exists
in the wild -- ``GroovePool/Grooves/Groove/Clip`` carries a full ``MidiClip``
copy of whatever clip a groove was last extracted from -- but that clip
belongs to no track and never plays back, so it is deliberately not walked
here, matching the track-scoped nature of this domain.

Two id spaces meet in a clip and must not be confused. ``KeyTrack/@Id`` and
``MidiNoteEvent/@NoteId`` are clip-local: every clip in a set restarts them
at 0 and 1 respectively, and ``Notes/NoteIdGenerator/NextId`` is the
clip's own counter. The set-global space -- owned by ``Pointee``, every
``*AutomationTarget``/``*ModulationTarget`` and ``ControllerTargets.<N>``,
counted by ``LiveSet/NextPointeeId`` -- is what a duplicated clip has to
renumber.
A clip envelope only *references* that space (``EnvelopeTarget/PointeeId``
points at a device parameter living outside the clip), so those references
are left alone unless the id they name was itself renumbered.

``AudioClipRef``/``Clips.audio()`` read the same session/arrangement shape
on ``AudioTrack`` -- same ``ClipSlotList``, same ``Loop`` markers -- but
carry a sample name instead of notes, and there is no note writer for them.

Arrangement placement -- ``place_clip``, ``copy_section``,
``remove_section_clips`` -- treats MIDI and audio clips identically, because
in the arrangement they are identical: both are direct children of a track's
``Events`` list, carrying the same ``Time``/``CurrentStart``/``CurrentEnd``
placement block. ``sections.py`` holds the tree mechanics and the
measurements behind them; everything here is the domain layer on top.
"""

from __future__ import annotations

import copy
import dataclasses
import enum
import pathlib
import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any
from xml.etree import ElementTree as ET

from abletoolz.live_set import sample_ref, sections
from abletoolz.live_set.sections import CopiedPlacement, SkippedStraddler
from abletoolz.live_set.tracks import AbletonTrack
from abletoolz.live_set.xml_edit import number_value, renumber_pointee_ids, shift_indentation, tab_depth
from abletoolz.misc import get_element
from abletoolz.versioning import Version, versioned

if TYPE_CHECKING:
    from abletoolz.live_set.document import AbletonSet

_NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
_PITCH_NAME_RE = re.compile(r"([A-Ga-g])([#b]?)(-?\d{1,2})")
_ACCIDENTALS = {"": 0, "#": 1, "b": -1}


def pitch_name(pitch: int) -> str:
    """MIDI number to Ableton-convention pitch name (middle C = 60 = "C3")."""
    octave = pitch // 12 - 2
    return f"{_NOTE_NAMES[pitch % 12]}{octave}"


def parse_pitch_name(name: str) -> int:
    """Ableton-convention pitch name to MIDI number ("C3" -> 60). Flats accepted."""
    match = _PITCH_NAME_RE.fullmatch(name.strip())
    if match is None:
        raise ValueError(f"Not a pitch name: {name!r}")
    step, accidental, octave = match.groups()
    pitch = _NOTE_NAMES.index(step.upper()) + _ACCIDENTALS[accidental] + (int(octave) + 2) * 12
    if not 0 <= pitch <= 127:
        raise ValueError(f"Pitch name {name!r} is outside MIDI range 0-127")
    return pitch


def _boolean(value: bool) -> str:
    return "true" if value else "false"


class ClipLocation(enum.StrEnum):
    """Where a MidiClip was found in the set."""

    SESSION = "session"
    ARRANGEMENT = "arrangement"


@dataclasses.dataclass(frozen=True, slots=True)
class MidiNote:
    """One MidiNoteEvent, version differences already normalized away.

    Everything but pitch/start/duration defaults to what Live gives a
    freshly drawn note, so authoring a note is ``MidiNote(60, 0.0, 1.0)``.
    """

    pitch: int
    start: float
    duration: float
    velocity: float = 100.0
    off_velocity: float = 64.0
    enabled: bool = True
    probability: float = 1.0
    velocity_deviation: float = 0.0
    note_id: int | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MidiNote:
        """Build a note from the ``MidiClipRef.to_dict`` note shape.

        ``note`` takes an Ableton-convention name ("E3") in place of
        ``pitch``; when a dict carries both -- as ``to_dict`` output does --
        they must agree.
        """
        pitch = data.get("pitch")
        name = data.get("note")
        if pitch is None and name is None:
            raise ValueError("Note needs either 'pitch' or 'note'")
        resolved = int(pitch) if pitch is not None else parse_pitch_name(str(name))
        if pitch is not None and name is not None and parse_pitch_name(str(name)) != resolved:
            raise ValueError(f"pitch {resolved} and note {name!r} name different pitches")
        note_id = data.get("note_id")
        return cls(
            pitch=resolved,
            start=float(data["start"]),
            duration=float(data["duration"]),
            velocity=float(data.get("velocity", 100.0)),
            off_velocity=float(data.get("off_velocity", 64.0)),
            enabled=bool(data.get("enabled", True)),
            probability=float(data.get("probability", 1.0)),
            velocity_deviation=float(data.get("velocity_deviation", 0.0)),
            note_id=int(note_id) if note_id is not None else None,
        )


type NoteArray = list[int | float | bool | dict[str, float | bool]]
"""The compact array shape ``describe()`` emits and ``apply_ops`` accepts.

3 elements at minimum (``[pitch, start, duration]``); velocity and
off_velocity ride together once either leaves default, since a positional
array cannot name the second without the first; a trailing dict carries
non-default probability/velocity_deviation/enabled when present.
"""


def encode_note(note: MidiNote, *, extended: bool = False) -> NoteArray:
    """``MidiNote`` -> its compact array shape, 3 to 6 elements.

    ``extended=True`` (``describe()``'s FULL level) appends a trailing dict
    for probability/velocity_deviation/enabled when any of them leave
    default, forcing velocity and off_velocity to ride along even if both
    are themselves default -- there is no way to place a 6th element without
    the 4th and 5th. ``extended=False`` (PATTERNS level) never emits it.
    """
    array: NoteArray = [note.pitch, round(note.start, 4), round(note.duration, 4)]
    extra: dict[str, float | bool] = {}
    if extended:
        if note.probability != 1.0:
            extra["prob"] = round(note.probability, 4)
        if note.velocity_deviation != 0.0:
            extra["vdev"] = round(note.velocity_deviation, 4)
        if not note.enabled:
            extra["off"] = True
    default_off = note.off_velocity == 64.0
    default_velocity = note.velocity == 100.0
    if extra or not default_off:
        array.append(round(note.velocity, 4))
        array.append(round(note.off_velocity, 4))
    elif not default_velocity:
        array.append(round(note.velocity, 4))
    if extra:
        array.append(extra)
    return array


def _as_number(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Expected a number for {what}, got {value!r}")
    return float(value)


def decode_note_array(values: Sequence[object]) -> MidiNote:
    """The inverse of ``encode_note``: a 3-6 element array back to a ``MidiNote``.

    Level-agnostic -- it reads however many elements are present, so it
    accepts PATTERNS-shape (3-5) and FULL-shape (with the trailing dict)
    arrays alike, whichever an ``apply_ops`` caller hands it.
    """
    if not 3 <= len(values) <= 6:
        raise ValueError(f"Note array must have 3-6 elements, got {len(values)}")
    pitch_value = values[0]
    if isinstance(pitch_value, bool) or not isinstance(pitch_value, int):
        raise ValueError(f"Expected an integer pitch, got {pitch_value!r}")
    start = _as_number(values[1], "start")
    duration = _as_number(values[2], "duration")

    rest = list(values[3:])
    extra: dict[str, float | bool] = {}
    tail = rest[-1] if rest else None
    if isinstance(tail, dict):
        extra = tail
        rest = rest[:-1]
    velocity = _as_number(rest[0], "velocity") if len(rest) >= 1 else 100.0
    off_velocity = _as_number(rest[1], "off_velocity") if len(rest) >= 2 else 64.0
    probability = _as_number(extra["prob"], "probability") if "prob" in extra else 1.0
    velocity_deviation = _as_number(extra["vdev"], "velocity_deviation") if "vdev" in extra else 0.0
    enabled = not extra.get("off", False)

    return MidiNote(
        pitch=pitch_value,
        start=start,
        duration=duration,
        velocity=velocity,
        off_velocity=off_velocity,
        enabled=enabled,
        probability=probability,
        velocity_deviation=velocity_deviation,
    )


@dataclasses.dataclass(frozen=True, slots=True)
class MidiClipRef:
    """One MIDI clip found in the set, with its notes and placement.

    ``clip_element`` is the live handle the write path needs, the same way
    ``SampleRef`` holds the elements it rewrites. It stays out of ``repr``,
    out of equality (so refs still compare on parsed content) and out of
    ``to_dict``, which is a serialization of the clip, not of the tree.
    ``version`` rides along the same way, and for the same reason
    ``TrackDevices`` carries one: a clip can be placed into a different set
    than the one it was read from, and the placement has to compare the two.
    """

    name: str
    track_name: str | None
    location: ClipLocation
    start_time: float
    length: float | None
    notes: tuple[MidiNote, ...]
    version: Version = dataclasses.field(repr=False, compare=False)
    clip_element: ET.Element = dataclasses.field(repr=False, compare=False)

    def to_dict(self) -> dict[str, object]:
        """JSON-ready shape for LLM consumption: flat, both pitch number and name."""
        return {
            "name": self.name,
            "track_name": self.track_name,
            "location": self.location.value,
            "start_time": self.start_time,
            "length": self.length,
            "notes": [
                {
                    "pitch": note.pitch,
                    "note": pitch_name(note.pitch),
                    "start": note.start,
                    "duration": note.duration,
                    "velocity": note.velocity,
                    "off_velocity": note.off_velocity,
                    "enabled": note.enabled,
                    "probability": note.probability,
                    "velocity_deviation": note.velocity_deviation,
                    "note_id": note.note_id,
                }
                for note in self.notes
            ],
        }


@dataclasses.dataclass(frozen=True, slots=True)
class AudioClipRef:
    """One audio clip found in the set: placement and its sample, no note content.

    Mirrors ``MidiClipRef``'s shape. Audio clips carry no notes, so there is
    no ``to_dict``/``from_dict`` pair -- ``describe()`` reads ``sample_name``
    directly. ``clip_element`` follows the same repr/equality exclusion as
    ``MidiClipRef.clip_element``.
    """

    name: str
    track_name: str | None
    location: ClipLocation
    start_time: float
    length: float | None
    sample_name: str
    version: Version = dataclasses.field(repr=False, compare=False)
    clip_element: ET.Element = dataclasses.field(repr=False, compare=False)


def _parse_note(element: ET.Element, pitch: int) -> MidiNote:
    note_id = element.get("NoteId")
    return MidiNote(
        pitch=pitch,
        start=float(element.get("Time", "0")),
        duration=float(element.get("Duration", "0")),
        velocity=float(element.get("Velocity", "0")),
        off_velocity=float(element.get("OffVelocity", "0")),
        enabled=element.get("IsEnabled", "true") == "true",
        probability=float(element.get("Probability", "1")),
        velocity_deviation=float(element.get("VelocityDeviation", "0")),
        note_id=int(note_id) if note_id is not None else None,
    )


def _parse_notes(clip_element: ET.Element) -> list[MidiNote]:
    notes: list[MidiNote] = []
    for key_track in clip_element.iterfind("Notes/KeyTracks/KeyTrack"):
        key_element = key_track.find("MidiKey")
        if key_element is None:
            continue
        pitch_value = key_element.get("Value")
        if pitch_value is None:
            continue
        pitch = int(pitch_value)
        notes.extend(_parse_note(note_element, pitch) for note_element in key_track.iterfind("Notes/MidiNoteEvent"))
    return notes


def _clip_length(clip_element: ET.Element) -> float | None:
    """Loop length in beats, when the clip carries loop markers."""
    loop_start = clip_element.find("Loop/LoopStart")
    loop_end = clip_element.find("Loop/LoopEnd")
    if loop_start is None or loop_end is None:
        return None
    start_value = loop_start.get("Value")
    end_value = loop_end.get("Value")
    if start_value is None or end_value is None:
        return None
    return float(end_value) - float(start_value)


def _parse_clip(
    clip_element: ET.Element, track_name: str | None, location: ClipLocation, version: Version
) -> MidiClipRef:
    name_element = clip_element.find("Name")
    name = name_element.get("Value", "") if name_element is not None else ""
    notes = sorted(_parse_notes(clip_element), key=lambda note: (note.start, note.pitch))
    return MidiClipRef(
        name=name,
        track_name=track_name,
        location=location,
        start_time=float(clip_element.get("Time", "0")),
        length=_clip_length(clip_element),
        notes=tuple(notes),
        version=version,
        clip_element=clip_element,
    )


def _parse_audio_clip(
    clip_element: ET.Element,
    track_name: str | None,
    location: ClipLocation,
    version: Version,
    project_root: pathlib.Path,
) -> AudioClipRef:
    name_element = clip_element.find("Name")
    name = name_element.get("Value", "") if name_element is not None else ""
    sample_ref_element = get_element(clip_element, "SampleRef")
    parsed_sample = sample_ref.SampleRef.from_element(sample_ref_element, version, project_root)
    return AudioClipRef(
        name=name,
        track_name=track_name,
        location=location,
        start_time=float(clip_element.get("Time", "0")),
        length=_clip_length(clip_element),
        sample_name=parsed_sample.name,
        version=version,
        clip_element=clip_element,
    )


def _prepare_notes(notes: Sequence[MidiNote]) -> list[tuple[MidiNote, int]]:
    """Validate a note set and pair every note with the NoteId it will carry.

    Ids are handed out in ``(start, pitch)`` order, which is how Live numbers
    them: the 11.0 and 12.4.5 fixtures both read back 1..n in exactly that
    order. Ids the caller supplied are kept as given and skipped over.
    """
    for note in notes:
        if not 0 <= note.pitch <= 127:
            raise ValueError(f"Pitch {note.pitch} is outside MIDI range 0-127")
        if note.note_id is not None and note.note_id < 1:
            raise ValueError(f"NoteId {note.note_id} is below Live's first id, 1")

    preset = [note.note_id for note in notes if note.note_id is not None]
    if len(set(preset)) != len(preset):
        raise ValueError("Preset note_ids must be unique within a clip")

    by_pitch: dict[int, list[MidiNote]] = {}
    for note in notes:
        by_pitch.setdefault(note.pitch, []).append(note)
    for pitch, group in by_pitch.items():
        group.sort(key=lambda note: note.start)
        for earlier, later in zip(group, group[1:], strict=False):
            if later.start < earlier.start + earlier.duration:
                raise ValueError(
                    f"Notes on pitch {pitch} overlap at beat {later.start}; Live's editor cannot hold overlaps"
                )

    used = set(preset)
    counter = 1
    paired: list[tuple[MidiNote, int]] = []
    for note in sorted(notes, key=lambda note: (note.start, note.pitch)):
        if note.note_id is not None:
            paired.append((note, note.note_id))
            continue
        while counter in used:
            counter += 1
        used.add(counter)
        paired.append((note, counter))
    return paired


def _indent_key_tracks(clip_element: ET.Element, key_tracks: ET.Element) -> None:
    """Pretty-print a rebuilt KeyTracks subtree the way Live writes it.

    Live indents with tabs, so the whole block hangs off one known anchor:
    ``MidiClip``'s own text is the indent of its children, which is where
    ``Notes`` sits, one shallower than ``KeyTracks``. The tail after
    ``KeyTracks`` is left alone -- it belongs to whatever follows, which
    differs by version.
    """
    base = (clip_element.text or "") + "\t"
    key_tracks.text = base + "\t" if len(key_tracks) else None
    for index, key_track in enumerate(key_tracks):
        last_track = index == len(key_tracks) - 1
        key_track.tail = base if last_track else base + "\t"
        key_track.text = base + "\t\t"
        events, midi_key = key_track[0], key_track[1]
        events.text = base + "\t\t\t" if len(events) else None
        for event_index, event in enumerate(events):
            last_event = event_index == len(events) - 1
            event.tail = base + "\t\t" if last_event else base + "\t\t\t"
        events.tail = base + "\t\t"
        midi_key.tail = base + "\t"


def _set_clip_length(clip_element: ET.Element, length: float) -> None:
    """Make a session clip exactly ``length`` beats long, start to loop end.

    Live shows a clip's length from ``CurrentStart``/``CurrentEnd`` and loops
    it between the ``Loop`` markers; every session clip in the fixture corpus
    has all of them agreeing on 0..length, so all of them are written.
    """
    if length <= 0:
        raise ValueError(f"Clip length must be positive, got {length}")
    end = number_value(length)
    get_element(clip_element, "CurrentStart").set("Value", "0")
    get_element(clip_element, "CurrentEnd").set("Value", end)
    loop = get_element(clip_element, "Loop")
    for tag, value in (
        ("LoopStart", "0"),
        ("LoopEnd", end),
        ("OutMarker", end),
        ("HiddenLoopStart", "0"),
        ("HiddenLoopEnd", end),
    ):
        get_element(loop, tag).set("Value", value)


class Clips:
    """MIDI clips and notes of one set: reading them, and authoring new ones."""

    def __init__(self, live_set: AbletonSet) -> None:
        self._set = live_set

    @property
    def version(self) -> Version:
        return self._set.version_tuple

    @property
    def _root(self) -> ET.Element:
        return self._set.root

    def midi(self) -> list[MidiClipRef]:
        """Every MIDI clip in the set, session and arrangement, with its notes."""
        refs: list[MidiClipRef] = []
        for track in self._set.tracks.load():
            if track.type != "MidiTrack":
                continue
            track_name = track.name or None
            for clip_slot in track.clips_clipview():
                midi_clip = clip_slot.find("ClipSlot/Value/MidiClip")
                if midi_clip is not None:
                    refs.append(_parse_clip(midi_clip, track_name, ClipLocation.SESSION, self.version))
            for midi_clip in track.clips_arrangement():
                refs.append(_parse_clip(midi_clip, track_name, ClipLocation.ARRANGEMENT, self.version))
        return refs

    def audio(self) -> list[AudioClipRef]:
        """Every audio clip in the set, session and arrangement, with its sample name.

        Scoped to ``DeviceChain/MainSequencer`` on each ``AudioTrack``, not
        the generic ``clips_clipview``/``clips_arrangement`` walk MIDI uses:
        every track type also carries a ``FreezeSequencer`` with its own
        ``ClipSlotList``, and freezing an audio track would leave an
        ``AudioClip`` there too. No fixture in the corpus freezes one, but
        unlike a frozen MIDI track (which renders to audio, never MidiClip),
        the tag alone can't tell a frozen render apart from a real clip, so
        this stays scoped to MainSequencer rather than guessing.
        """
        refs: list[AudioClipRef] = []
        project_root = self._set.project_root_folder or pathlib.Path(".")
        for track in self._set.tracks.load():
            if track.type != "AudioTrack":
                continue
            track_name = track.name or None
            main_sequencer = track.track_root.find("DeviceChain/MainSequencer")
            if main_sequencer is None:
                continue
            for clip_slot in main_sequencer.iter("ClipSlot"):
                audio_clip = clip_slot.find("ClipSlot/Value/AudioClip")
                if audio_clip is not None:
                    refs.append(
                        _parse_audio_clip(audio_clip, track_name, ClipLocation.SESSION, self.version, project_root)
                    )
            sample = main_sequencer.find("Sample")
            if sample is not None:
                for audio_clip in sample.iter("AudioClip"):
                    refs.append(
                        _parse_audio_clip(audio_clip, track_name, ClipLocation.ARRANGEMENT, self.version, project_root)
                    )
        return refs

    # --- MidiNoteEvent shape, one implementation per schema generation ---

    @versioned
    def _note_attributes(self, note: MidiNote, note_id: int) -> dict[str, str]:
        """Pre-11: integer velocities, IsEnabled, and no note ids at all."""
        return {
            "Time": number_value(note.start),
            "Duration": number_value(note.duration),
            "Velocity": str(round(note.velocity)),
            "OffVelocity": str(round(note.off_velocity)),
            "IsEnabled": _boolean(note.enabled),
        }

    @_note_attributes.since((11, 0, 0))  # type: ignore[no-redef]  # @versioned pattern: same name registers the 11+ override
    def _note_attributes(self, note: MidiNote, note_id: int) -> dict[str, str]:
        """11.x: float velocities plus the full expression set and a NoteId."""
        return {
            "Time": number_value(note.start),
            "Duration": number_value(note.duration),
            "Velocity": number_value(note.velocity),
            "VelocityDeviation": number_value(note.velocity_deviation),
            "OffVelocity": number_value(note.off_velocity),
            "Probability": number_value(note.probability),
            "IsEnabled": _boolean(note.enabled),
            "NoteId": str(note_id),
        }

    @_note_attributes.since((12, 0, 0))  # type: ignore[no-redef]  # @versioned pattern: same name registers the 12+ override
    def _note_attributes(self, note: MidiNote, note_id: int) -> dict[str, str]:
        """12.x: the expression attributes appear only when they are not default.

        Both 12.x fixtures carry nothing but Time/Duration/Velocity/
        OffVelocity/NoteId, and every note in them is a default note. Live 11
        named the other three, and this module's reader already defaults them
        away when absent, so a non-default note keeps its meaning by writing
        them back in 11's attribute order rather than losing it.
        """
        attributes = {
            "Time": number_value(note.start),
            "Duration": number_value(note.duration),
            "Velocity": number_value(note.velocity),
        }
        if note.velocity_deviation != 0.0:
            attributes["VelocityDeviation"] = number_value(note.velocity_deviation)
        attributes["OffVelocity"] = number_value(note.off_velocity)
        if note.probability != 1.0:
            attributes["Probability"] = number_value(note.probability)
        if not note.enabled:
            attributes["IsEnabled"] = _boolean(note.enabled)
        attributes["NoteId"] = str(note_id)
        return attributes

    @versioned
    def _key_track_attributes(self, index: int) -> dict[str, str]:
        """Pre-10: KeyTrack carries no Id."""
        return {}

    @_key_track_attributes.since((10, 0, 0))  # type: ignore[no-redef]  # @versioned pattern: same name registers the 10+ override
    def _key_track_attributes(self, index: int) -> dict[str, str]:
        """10.0+: clip-local Id, numbered from 0 across the clip's KeyTracks."""
        return {"Id": str(index)}

    @versioned
    def _sync_note_bookkeeping(self, notes_element: ET.Element, next_note_id: int) -> None:
        """Pre-11: no note ids and no per-note events to keep in step."""

    @_sync_note_bookkeeping.since((11, 0, 0))  # type: ignore[no-redef]  # @versioned pattern: same name registers the 11+ override
    def _sync_note_bookkeeping(self, notes_element: ET.Element, next_note_id: int) -> None:
        """11.0+: advance the clip's id counter and drop now-stale per-note events.

        ``PerNoteEventStore`` entries are keyed by NoteId. Replacing the notes
        makes every one of them point at a note that no longer exists, so they
        go rather than survive as expression data attached to nothing.
        """
        get_element(notes_element, "NoteIdGenerator.NextId").set("Value", str(next_note_id))
        event_lists = get_element(notes_element, "PerNoteEventStore.EventLists")
        for entry in list(event_lists):
            event_lists.remove(entry)
        event_lists.text = None

    # --- writing ---

    def set_notes(self, clip: MidiClipRef, notes: Sequence[MidiNote]) -> None:
        """Replace every note in ``clip``, rebuilding its KeyTracks from scratch."""
        self._write_notes(clip.clip_element, notes)

    def _write_notes(self, clip_element: ET.Element, notes: Sequence[MidiNote]) -> None:
        notes_element = get_element(clip_element, "Notes")
        key_tracks = get_element(notes_element, "KeyTracks")
        paired = _prepare_notes(notes)

        by_pitch: dict[int, list[tuple[MidiNote, int]]] = {}
        for note, note_id in paired:
            by_pitch.setdefault(note.pitch, []).append((note, note_id))

        for existing in list(key_tracks):
            key_tracks.remove(existing)
        for index, pitch in enumerate(sorted(by_pitch)):
            key_track = ET.SubElement(key_tracks, "KeyTrack", self._key_track_attributes(index))
            events = ET.SubElement(key_track, "Notes")
            for note, note_id in sorted(by_pitch[pitch], key=lambda pair: pair[0].start):
                ET.SubElement(events, "MidiNoteEvent", self._note_attributes(note, note_id))
            ET.SubElement(key_track, "MidiKey", {"Value": str(pitch)})

        _indent_key_tracks(clip_element, key_tracks)
        self._sync_note_bookkeeping(notes_element, max((note_id for _, note_id in paired), default=0) + 1)

    def clone_clip(
        self,
        donor: MidiClipRef,
        *,
        slot_index: int,
        name: str,
        notes: Sequence[MidiNote],
        length: float | None = None,
    ) -> MidiClipRef:
        """Duplicate ``donor`` into an empty session slot of its own track.

        The copy keeps the donor's device-facing settings -- groove, launch
        mode, envelopes, follow actions -- and takes a new name, a new set of
        notes, and a length. ``length`` defaults to the donor's loop length.
        Anything the copy owns in the set-global id space is renumbered past
        ``LiveSet/NextPointeeId``, which is then advanced past the copy.

        Two of the donor's flags are placement state rather than a setting and
        are always reset on the copy: a deactivated donor (``Disabled``) would
        clone into a slot that stays silent when launched, and an arrangement
        donor is routinely non-looping (``Loop/LoopOn``), which would clone
        into a session clip that plays once and stops.
        """
        track = self.track_of(donor.clip_element)
        slots = list(track.track_root.iterfind("DeviceChain/MainSequencer/ClipSlotList/ClipSlot"))
        if not 0 <= slot_index < len(slots):
            raise ValueError(f"Track {track.name!r} has {len(slots)} session slots; no slot {slot_index}")
        inner_slot = get_element(slots[slot_index], "ClipSlot")
        value = get_element(inner_slot, "Value")
        if len(value):
            raise ValueError(f"Session slot {slot_index} of track {track.name!r} already holds a clip")

        clone = copy.deepcopy(donor.clip_element)
        shift_indentation(clone, (tab_depth(inner_slot.text) + 1) - (tab_depth(clone.text) - 1))
        renumber_pointee_ids(clone, self._root)

        # A session clip always starts at 0 and is the only clip in its slot.
        clone.set("Time", "0")
        if "Id" in clone.attrib:
            clone.set("Id", "0")
        get_element(clone, "Name").set("Value", name)
        get_element(clone, "Disabled").set("Value", "false")
        get_element(clone, "Loop.LoopOn").set("Value", "true")
        clone_length = donor.length if length is None else length
        if clone_length is None:
            raise ValueError("Donor clip carries no loop markers; pass length explicitly")
        _set_clip_length(clone, clone_length)
        self._write_notes(clone, notes)

        value.text = (inner_slot.text or "") + "\t"
        clone.tail = inner_slot.text
        value.append(clone)
        return _parse_clip(clone, track.name or None, ClipLocation.SESSION, self.version)

    # --- arrangement sections ---------------------------------------------

    def place_clip(
        self,
        donor: MidiClipRef | AudioClipRef,
        *,
        track: AbletonTrack | str,
        at: float,
        length: float | None = None,
        replace: bool = False,
    ) -> MidiClipRef | AudioClipRef:
        """Copy ``donor`` into this set's arrangement as a new event at beat ``at``.

        The donor may sit in a session slot or in an arrangement, in this set
        or in another one of the same major version -- the same rule
        ``Devices.graft_chain`` applies, and for the same reason: clip
        schemas are only known to match within a major version.

        ``length`` is the arrangement span in beats and defaults to the
        donor's own. A MIDI donor lands on a ``MidiTrack``, an audio donor on
        an ``AudioTrack``; the two are shape-identical in the arrangement --
        both are direct children of a track's ``Events`` list carrying the
        same ``Time``/``CurrentStart``/``CurrentEnd``/``Loop`` block -- so
        both are supported by exactly the same mechanics.

        ``Disabled`` is always written false: a deactivated donor would place
        a clip that silently does nothing. Loop state is kept as the donor
        left it, because non-looping arrangement clips are normal.

        The placement is refused when it would overlap an existing clip's
        ``[Time, CurrentEnd)`` span on the target track. ``replace=True``
        deletes those clips outright instead; nothing is ever trimmed.
        """
        target = self._resolve_track(track)
        events = self._require_events(target)
        expected = "MidiTrack" if isinstance(donor, MidiClipRef) else "AudioTrack"
        if target.type != expected:
            raise ValueError(
                f"A {donor.clip_element.tag} donor needs a {expected}, but track {target.name!r} is a {target.type}"
            )
        if donor.version[0] != self.version[0]:
            raise ValueError(
                f"Cannot place a Live {donor.version[0]}.x clip into a Live {self.version[0]}.x set; "
                "clip schemas are only known to match within a major version"
            )

        clone = copy.deepcopy(donor.clip_element)
        sections.move_clip(clone, at, length)
        span = sections.event_span(clone)
        self._clear_span(target, events, span, replace=replace)

        shift_indentation(clone, (tab_depth(events.indent) + 1) - (tab_depth(clone.text) - 1))
        renumber_pointee_ids(clone, self._root)
        get_element(clone, "Disabled").set("Value", "false")
        sections.assign_event_id(events.element, clone)
        sections.insert_in_time_order(events.element, clone, indent=events.indent)

        name = target.name or None
        if isinstance(donor, MidiClipRef):
            return _parse_clip(clone, name, ClipLocation.ARRANGEMENT, self.version)
        project_root = self._set.project_root_folder or pathlib.Path(".")
        return _parse_audio_clip(clone, name, ClipLocation.ARRANGEMENT, self.version, project_root)

    def copy_section(
        self,
        src_start: float,
        src_end: float,
        dest_start: float,
        *,
        track_names: Sequence[str] | None = None,
        mode: sections.SectionMode = sections.SectionMode.REFUSE,
    ) -> sections.SectionCopyReport:
        """Copy a stretch of arrangement -- clips and track automation -- somewhere else.

        Every MIDI and audio clip *fully inside* ``[src_start, src_end)`` on
        every selected track is duplicated at the same offset into the
        destination. A clip crossing either edge is neither copied nor cut in
        half: it is reported as a straddler and left exactly where it is.

        ``track_names`` selects a subset by name and raises on a name two
        tracks share, the same rule ``apply_ops`` resolves selectors by;
        ``None`` means every track that has an arrangement.

        The destination window is ``[dest_start, dest_start + (src_end -
        src_start))`` and is only inspected on tracks that actually receive a
        copy -- a track contributing nothing to the section has nothing to
        collide with. ``SectionMode.REFUSE`` raises listing the collisions;
        ``SectionMode.REPLACE`` deletes the clips fully inside the
        destination first. A clip straddling a *destination* edge is an error
        under both modes, since honouring it would mean trimming.

        Track automation rides along: every envelope event in the source
        range is copied to the same offset, into the same envelope, with the
        envelope's target untouched. See ``sections.copy_envelope_segment``
        for what that does and does not do at the edges. Clip envelopes need
        no special handling -- they are clip-internal and travel inside the
        copied clip.
        """
        if src_start < 0:
            raise ValueError(f"Section start must not be negative, got {src_start}")
        if src_end <= src_start:
            raise ValueError(f"Section end {src_end} must be after its start {src_start}")
        if dest_start < 0:
            raise ValueError(f"Destination start must not be negative, got {dest_start}")

        delta = dest_start - src_start
        dest_end = dest_start + (src_end - src_start)
        skipped: list[SkippedStraddler] = []
        planned: list[tuple[AbletonTrack, sections.ArrangementEvents, list[ET.Element]]] = []

        selected = self._section_tracks(track_names)
        for target in selected:
            events = sections.arrangement_events(target.track_root, target.type)
            if events is None:
                continue
            inside: list[ET.Element] = []
            for clip in events.clips():
                span = sections.event_span(clip)
                boundary = sections.straddled_boundary(span, src_start, src_end)
                if boundary is not None:
                    skipped.append(
                        SkippedStraddler(
                            track_name=target.name,
                            clip_name=sections.clip_name(clip),
                            start=span[0],
                            end=span[1],
                            boundary=boundary,
                        )
                    )
                elif src_start <= span[0] and span[1] <= src_end:
                    inside.append(clip)
            planned.append((target, events, inside))

        collisions: list[str] = []
        for target, events, inside in planned:
            if not inside:
                continue
            for clip in self._span_occupants(events, dest_start, dest_end):
                collisions.append(f"{sections.clip_name(clip)!r} on track {target.name!r} at beat {clip.get('Time')}")
        if collisions and mode is sections.SectionMode.REFUSE:
            raise ValueError(
                f"Destination {number_value(dest_start)}-{number_value(dest_end)} already holds "
                f"{len(collisions)} clip(s): {', '.join(collisions)}"
            )

        # Every clone is taken before anything is deleted, so a source range
        # that overlaps its own destination still copies what it started with.
        clones = [(target, events, [copy.deepcopy(clip) for clip in inside]) for target, events, inside in planned]

        replaced = 0
        copied: list[CopiedPlacement] = []
        for target, events, cloned in clones:
            if cloned:
                doomed = self._span_occupants(events, dest_start, dest_end)
                sections.remove_children(events.element, doomed)
                replaced += len(doomed)
            for clone in cloned:
                start = sections.event_time(clone) + delta
                sections.move_clip(clone, start, None)
                renumber_pointee_ids(clone, self._root)
                get_element(clone, "Disabled").set("Value", "false")
                sections.assign_event_id(events.element, clone)
                sections.insert_in_time_order(events.element, clone, indent=events.indent)
                copied.append(CopiedPlacement(track_name=target.name, clip_name=sections.clip_name(clone), start=start))

        # Automation belongs to the track, not to its clips: a group track
        # holds envelopes and no clips at all, so this walks every selected
        # track rather than only the ones a copy landed on.
        envelope_events = 0
        for target in selected:
            for envelope in sections.track_envelopes(target.track_root):
                envelope_events += sections.copy_envelope_segment(envelope, src_start, src_end, delta)

        return sections.SectionCopyReport(
            dest_start=dest_start,
            copied=tuple(copied),
            skipped=tuple(skipped),
            replaced=replaced,
            envelope_events=envelope_events,
        )

    def remove_section_clips(self, start: float, end: float, *, track_names: Sequence[str]) -> list[str]:
        """Delete the arrangement clips fully inside ``[start, end)`` on named tracks.

        The track list is required rather than defaulted: this is the one
        destructive operation here, and "every track" is not something to
        arrive at by leaving an argument out. Clips crossing either edge are
        left alone and reported in the returned lines.
        """
        if end <= start:
            raise ValueError(f"Section end {end} must be after its start {start}")

        lines: list[str] = []
        for target in self._section_tracks(track_names):
            events = sections.arrangement_events(target.track_root, target.type)
            if events is None:
                continue
            doomed: list[ET.Element] = []
            for clip in events.clips():
                span = sections.event_span(clip)
                boundary = sections.straddled_boundary(span, start, end)
                if boundary is not None:
                    lines.append(
                        f"kept {sections.clip_name(clip)!r} on {target.name!r} "
                        f"({number_value(span[0])}-{number_value(span[1])}): crosses the section {boundary}"
                    )
                elif start <= span[0] and span[1] <= end:
                    doomed.append(clip)
            for clip in doomed:
                lines.append(
                    f"deleted {sections.clip_name(clip)!r} on {target.name!r} "
                    f"at beat {number_value(sections.event_time(clip))}"
                )
            sections.remove_children(events.element, doomed)
        return lines

    def _resolve_track(self, track: AbletonTrack | str) -> AbletonTrack:
        """A track handle or name to one track, raising on a name two tracks share."""
        if not isinstance(track, str):
            return track
        matches = [candidate for candidate in self._set.tracks.load() if candidate.name == track]
        if not matches:
            raise ValueError(f"No track named {track!r}")
        if len(matches) > 1:
            raise ValueError(f"{len(matches)} tracks are named {track!r}; pass the track itself instead")
        return matches[0]

    def _section_tracks(self, track_names: Sequence[str] | None) -> list[AbletonTrack]:
        if track_names is None:
            return self._set.tracks.load()
        return [self._resolve_track(name) for name in track_names]

    def _require_events(self, track: AbletonTrack) -> sections.ArrangementEvents:
        events = sections.arrangement_events(track.track_root, track.type)
        if events is None:
            raise ValueError(f"Track {track.name!r} is a {track.type} and holds no arrangement clips")
        return events

    def _span_occupants(self, events: sections.ArrangementEvents, start: float, end: float) -> list[ET.Element]:
        """Clips inside ``[start, end)``, raising on any that straddle an edge."""
        occupants: list[ET.Element] = []
        for clip in events.clips():
            span = sections.event_span(clip)
            boundary = sections.straddled_boundary(span, start, end)
            if boundary is not None:
                raise ValueError(
                    f"Clip {sections.clip_name(clip)!r} spans {number_value(span[0])}-{number_value(span[1])} and "
                    f"crosses the {boundary} of {number_value(start)}-{number_value(end)}; "
                    "sections are never trimmed, so move or delete it first"
                )
            if start <= span[0] and span[1] <= end:
                occupants.append(clip)
        return occupants

    def _clear_span(
        self,
        track: AbletonTrack,
        events: sections.ArrangementEvents,
        span: tuple[float, float],
        *,
        replace: bool,
    ) -> None:
        occupants = self._span_occupants(events, *span)
        if not occupants:
            return
        if not replace:
            names = ", ".join(f"{sections.clip_name(clip)!r} at beat {clip.get('Time')}" for clip in occupants)
            raise ValueError(
                f"Beats {number_value(span[0])}-{number_value(span[1])} of track {track.name!r} already hold "
                f"{len(occupants)} clip(s): {names}"
            )
        sections.remove_children(events.element, occupants)

    def track_of(self, clip_element: ET.Element) -> AbletonTrack:
        """The track owning a clip element, MIDI or audio, found by identity.

        Identity, not name: duplicate track names are real (a real set can
        carry two tracks both named the same), so this is the only lookup
        ``describe()``/``apply_ops`` can trust to land on the right track.
        """
        for track in self._set.tracks.load():
            if any(candidate is clip_element for candidate in track.track_root.iter(clip_element.tag)):
                return track
        raise ValueError("Clip does not belong to any track in this set")
