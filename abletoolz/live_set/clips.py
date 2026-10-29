"""MIDI clip and note reading and authoring, plus read-only audio clip discovery.

Notes live at ``MidiClip -> Notes -> KeyTracks -> KeyTrack(Id)``, where each
``KeyTrack`` carries the pitch (``MidiKey/@Value``) and its own ``Notes``
list of ``MidiNoteEvent`` elements. Attribute shape drifts by version: 9.x
carries ``IsEnabled`` and integer velocities; 11.0+ adds ``NoteId``,
``Probability``, ``VelocityDeviation`` and float velocities; 12.x keeps
``NoteId`` and writes the three expression attributes only when they leave
their defaults. Reading is tolerant (absent -> sensible default); writing
follows the set's own version exactly.

Clips live in two places, both read through the ``AbletonTrack`` that owns
them: session slots in ``DeviceChain/MainSequencer/ClipSlotList`` and
arrangement ``DeviceChain/MainSequencer/ClipTimeable`` content. Every ref
keeps that track and its slot, so nothing downstream has to search the set to
find where a clip lives. ``GroovePool/Grooves/Groove/Clip`` carries a third,
full ``MidiClip`` copy of whatever clip a groove was last extracted from, but
it belongs to no track and never plays back, so it is not walked.

Two id spaces meet in a clip and must not be confused. ``KeyTrack/@Id`` and
``MidiNoteEvent/@NoteId`` are clip-local: every clip in a set restarts them
at 0 and 1 respectively, and ``Notes/NoteIdGenerator/NextId`` is the
clip's own counter. The set-global space -- owned by ``Pointee``, every
``*AutomationTarget``/``*ModulationTarget`` and ``ControllerTargets.<N>``,
counted by ``LiveSet/NextPointeeId`` -- is what a duplicated clip has to
renumber. A clip envelope only *references* that space
(``EnvelopeTarget/PointeeId`` points at a device parameter outside the clip),
so those references are left alone unless the id they name was renumbered.

``AudioClipRef``/``Clips.audio()`` read the same session/arrangement shape
on ``AudioTrack`` -- same ``ClipSlotList``, same ``Loop`` markers -- but
carry a sample name instead of notes, and there is no note writer for them.

Arrangement placement -- ``place_clip``, ``copy_section``,
``remove_section_clips`` -- treats MIDI and audio clips identically, because
in the arrangement they are identical: both are direct children of a track's
``Events`` list, carrying the same ``Time``/``CurrentStart``/``CurrentEnd``
placement block. ``sections.py`` holds the tree mechanics and the
measurements behind them; this module is the domain layer on top.
"""

from __future__ import annotations

import copy
import dataclasses
import enum
from collections.abc import Callable, Sequence
from xml.etree import ElementTree as ET

from abletoolz.elements import get_element, remove_children
from abletoolz.live_set import sample_ref, sections
from abletoolz.live_set.domain import Domain
from abletoolz.live_set.sections import CopiedPlacement, SkippedStraddler
from abletoolz.live_set.settings import Setting, find, resolve
from abletoolz.live_set.tracks import AbletonTrack
from abletoolz.live_set.xml_edit import number_value, renumber_pointee_ids, shift_indentation, tab_depth
from abletoolz.schema import Attr, Tag, path
from abletoolz.schema.objects import Clip
from abletoolz.schema.versioning import Version, versioned


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
    are default: a 6th element cannot be placed without the 4th and 5th.
    ``extended=False`` (PATTERNS level) never emits it.
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
    if extra or note.off_velocity != 64.0:
        array += [round(note.velocity, 4), round(note.off_velocity, 4)]
    elif note.velocity != 100.0:
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

    Level-agnostic: it reads however many elements are present, so PATTERNS
    (3-5) and FULL (with the trailing dict) arrays are both accepted.
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

    ``clip_element`` is the live handle the write path needs. It stays out of
    ``repr`` and equality, so refs compare on parsed content rather than tree
    identity. ``version`` is kept out the same way; it rides along because a
    clip can be placed into a different set than the one it was read from, and
    the placement has to compare the two. ``track`` is the track the clip was
    read from, also kept out; ``slot`` is the session slot holding it, None in
    the arrangement.
    """

    name: str
    location: ClipLocation
    start_time: float
    length: float | None
    notes: tuple[MidiNote, ...]
    version: Version = dataclasses.field(repr=False, compare=False)
    clip_element: ET.Element = dataclasses.field(repr=False, compare=False)
    track: AbletonTrack = dataclasses.field(repr=False, compare=False)
    slot: int | None = None

    @property
    def track_name(self) -> str | None:
        return self.track.name or None

    def setting(self, member: Clip, **indices: int) -> Setting:
        """One of this clip's settings: ``setting(Clip.LOOP_START)``."""
        return resolve(self.clip_element, member, self.version, indices, label=f"clip {self.name!r}")


@dataclasses.dataclass(frozen=True, slots=True)
class AudioClipRef:
    """One audio clip found in the set: ``MidiClipRef``'s shape, with ``sample_name`` in place of notes."""

    name: str
    location: ClipLocation
    start_time: float
    length: float | None
    sample_name: str
    version: Version = dataclasses.field(repr=False, compare=False)
    clip_element: ET.Element = dataclasses.field(repr=False, compare=False)
    track: AbletonTrack = dataclasses.field(repr=False, compare=False)
    slot: int | None = None

    @property
    def track_name(self) -> str | None:
        return self.track.name or None

    def setting(self, member: Clip, **indices: int) -> Setting:
        """One of this clip's settings: ``setting(Clip.WARP_MODE)``."""
        return resolve(self.clip_element, member, self.version, indices, label=f"clip {self.name!r}")


def _parse_note(element: ET.Element, pitch: int) -> MidiNote:
    note_id = element.get(Attr.NOTE_ID)
    return MidiNote(
        pitch=pitch,
        start=float(element.get(Attr.TIME, "0")),
        duration=float(element.get(Attr.DURATION, "0")),
        velocity=float(element.get(Attr.VELOCITY, "0")),
        off_velocity=float(element.get(Attr.OFF_VELOCITY, "0")),
        enabled=element.get(Attr.IS_ENABLED, "true") == "true",
        probability=float(element.get(Attr.PROBABILITY, "1")),
        velocity_deviation=float(element.get(Attr.VELOCITY_DEVIATION, "0")),
        note_id=int(note_id) if note_id is not None else None,
    )


def _parse_notes(clip_element: ET.Element) -> list[MidiNote]:
    notes: list[MidiNote] = []
    for key_track in clip_element.iterfind(path(Tag.NOTES, Tag.KEY_TRACKS, Tag.KEY_TRACK)):
        key_element = key_track.find(Tag.MIDI_KEY)
        if key_element is None:
            continue
        pitch_value = key_element.get(Attr.VALUE)
        if pitch_value is None:
            continue
        pitch = int(pitch_value)
        notes.extend(
            _parse_note(note_element, pitch)
            for note_element in key_track.iterfind(path(Tag.NOTES, Tag.MIDI_NOTE_EVENT))
        )
    return notes


def _clip_length(clip_element: ET.Element, version: Version) -> float | None:
    """Loop length in beats, when the clip carries loop markers."""
    loop_start = find(clip_element, Clip.LOOP_START, version)
    loop_end = find(clip_element, Clip.LOOP_END, version)
    if loop_start is None or loop_end is None:
        return None
    return float(loop_end.raw) - float(loop_start.raw)


def _location(slot: int | None) -> ClipLocation:
    return ClipLocation.ARRANGEMENT if slot is None else ClipLocation.SESSION


def _parse_clip(clip_element: ET.Element, track: AbletonTrack, slot: int | None, version: Version) -> MidiClipRef:
    """A MIDI clip on ``track``, in session slot ``slot`` or, for None, in the arrangement."""
    notes = sorted(_parse_notes(clip_element), key=lambda note: (note.start, note.pitch))
    return MidiClipRef(
        name=sections.clip_name(clip_element, version),
        location=_location(slot),
        start_time=float(clip_element.get(Attr.TIME, "0")),
        length=_clip_length(clip_element, version),
        notes=tuple(notes),
        version=version,
        clip_element=clip_element,
        track=track,
        slot=slot,
    )


def _parse_audio_clip(
    clip_element: ET.Element, track: AbletonTrack, slot: int | None, version: Version
) -> AudioClipRef:
    """An audio clip on ``track``; its sample is read for its name alone, so no project folder is needed."""
    return AudioClipRef(
        name=sections.clip_name(clip_element, version),
        location=_location(slot),
        start_time=float(clip_element.get(Attr.TIME, "0")),
        length=_clip_length(clip_element, version),
        sample_name=sample_ref.SampleRef(get_element(clip_element, Tag.SAMPLE_REF), version).name,
        version=version,
        clip_element=clip_element,
        track=track,
        slot=slot,
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
        if not note.duration > 0:
            raise ValueError(f"Duration {note.duration} of the note at beat {note.start} must be positive")
        if not 1 <= note.velocity <= 127:
            raise ValueError(f"Velocity {note.velocity} is outside MIDI range 1-127")
        if not 0 <= note.off_velocity <= 127:
            raise ValueError(f"Off velocity {note.off_velocity} is outside MIDI range 0-127")
        if not 0 <= note.probability <= 1:
            raise ValueError(f"Probability {note.probability} is outside 0-1")
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


class Clips(Domain):
    """MIDI clips and notes of one set: reading them, and authoring new ones."""

    def midi(self, track: AbletonTrack | None = None) -> list[MidiClipRef]:
        """Every MIDI clip in the set, or only ``track``'s, session and arrangement, with its notes."""
        refs: list[MidiClipRef] = []
        tracks = self._set.tracks.load() if track is None else [track]
        for track in tracks:
            if track.type != Tag.MIDI_TRACK:
                continue
            for slot, clip in track.session_clips():
                if clip.tag == Tag.MIDI_CLIP:
                    refs.append(_parse_clip(clip, track, slot, self.version))
            for midi_clip in track.clips_arrangement():
                refs.append(_parse_clip(midi_clip, track, None, self.version))
        return refs

    def audio(self, track: AbletonTrack | None = None) -> list[AudioClipRef]:
        """Every audio clip in the set, or only ``track``'s, session and arrangement, with its sample name.

        Session slots come from ``AbletonTrack.session_clips``, which reads
        ``DeviceChain/MainSequencer`` only: every track type also carries a
        ``FreezeSequencer`` with its own ``ClipSlotList``, and a frozen track
        keeps its rendered ``AudioClip`` there, which the tag alone cannot tell
        apart from a real clip.

        Arrangement clips come from ``sections.arrangement_events``, the same
        list the writers edit, which scopes them for the same reason.
        """
        refs: list[AudioClipRef] = []
        tracks = self._set.tracks.load() if track is None else [track]
        for track in tracks:
            if track.type != Tag.AUDIO_TRACK:
                continue
            for slot, clip in track.session_clips():
                if clip.tag == Tag.AUDIO_CLIP:
                    refs.append(_parse_audio_clip(clip, track, slot, self.version))
            for audio_clip in track.clips_arrangement():
                refs.append(_parse_audio_clip(audio_clip, track, None, self.version))
        return refs

    # --- MidiNoteEvent shape, one implementation per schema generation ---

    @versioned
    def _note_attributes(self, note: MidiNote, note_id: int) -> dict[str, str]:
        """Pre-11: integer velocities, IsEnabled, and no note ids at all."""
        return {
            Attr.TIME: number_value(note.start),
            Attr.DURATION: number_value(note.duration),
            Attr.VELOCITY: str(round(note.velocity)),
            Attr.OFF_VELOCITY: str(round(note.off_velocity)),
            Attr.IS_ENABLED: _boolean(note.enabled),
        }

    @_note_attributes.since((11, 0, 0))  # type: ignore[no-redef]  # @versioned pattern: same name registers the 11+ override
    def _note_attributes(self, note: MidiNote, note_id: int) -> dict[str, str]:
        """11.x: float velocities plus the full expression set and a NoteId."""
        return {
            Attr.TIME: number_value(note.start),
            Attr.DURATION: number_value(note.duration),
            Attr.VELOCITY: number_value(note.velocity),
            Attr.VELOCITY_DEVIATION: number_value(note.velocity_deviation),
            Attr.OFF_VELOCITY: number_value(note.off_velocity),
            Attr.PROBABILITY: number_value(note.probability),
            Attr.IS_ENABLED: _boolean(note.enabled),
            Attr.NOTE_ID: str(note_id),
        }

    @_note_attributes.since((12, 0, 0))  # type: ignore[no-redef]  # @versioned pattern: same name registers the 12+ override
    def _note_attributes(self, note: MidiNote, note_id: int) -> dict[str, str]:
        """12.x: the expression attributes appear only when they are not default.

        Both 12.x fixtures carry nothing but Time/Duration/Velocity/
        OffVelocity/NoteId, and every note in them is a default note. The
        reader defaults the other three when absent, so a non-default note
        writes them back, in Live 11's attribute order, rather than lose them.
        """
        attributes = {
            Attr.TIME: number_value(note.start),
            Attr.DURATION: number_value(note.duration),
            Attr.VELOCITY: number_value(note.velocity),
        }
        if note.velocity_deviation != 0.0:
            attributes[Attr.VELOCITY_DEVIATION] = number_value(note.velocity_deviation)
        attributes[Attr.OFF_VELOCITY] = number_value(note.off_velocity)
        if note.probability != 1.0:
            attributes[Attr.PROBABILITY] = number_value(note.probability)
        if not note.enabled:
            attributes[Attr.IS_ENABLED] = _boolean(note.enabled)
        attributes[Attr.NOTE_ID] = str(note_id)
        return attributes

    @versioned
    def _key_track_attributes(self, index: int) -> dict[str, str]:
        """Pre-10: KeyTrack carries no Id."""
        return {}

    @_key_track_attributes.since((10, 0, 0))  # type: ignore[no-redef]  # @versioned pattern: same name registers the 10+ override
    def _key_track_attributes(self, index: int) -> dict[str, str]:
        """10.0+: clip-local Id, numbered from 0 across the clip's KeyTracks."""
        return {Attr.ID: str(index)}

    @versioned
    def _sync_note_bookkeeping(self, notes_element: ET.Element, next_note_id: int) -> None:
        """Pre-11: no note ids and no per-note events to keep in step."""

    @_sync_note_bookkeeping.since((11, 0, 0))  # type: ignore[no-redef]  # @versioned pattern: same name registers the 11+ override
    def _sync_note_bookkeeping(self, notes_element: ET.Element, next_note_id: int) -> None:
        """11.0+: advance the clip's id counter and drop now-stale per-note events.

        ``PerNoteEventStore`` entries are keyed by NoteId, so replacing the
        notes leaves every one of them pointing at a note that no longer exists.
        """
        get_element(notes_element, path(Tag.NOTE_ID_GENERATOR, Tag.NEXT_ID)).set(Attr.VALUE, str(next_note_id))
        event_lists = get_element(notes_element, path(Tag.PER_NOTE_EVENT_STORE, Tag.EVENT_LISTS))
        del event_lists[:]
        event_lists.text = None

    # --- writing ---

    def prepare_set_notes(self, clip: MidiClipRef, notes: Sequence[MidiNote]) -> Callable[[], None]:
        """Check ``notes`` for ``clip`` and return the write without running it; see :meth:`set_notes`."""
        paired = _prepare_notes(notes)
        return lambda: self._write_notes(clip.clip_element, paired)

    def set_notes(self, clip: MidiClipRef, notes: Sequence[MidiNote]) -> None:
        """Replace every note in ``clip``, rebuilding its KeyTracks from scratch."""
        self.prepare_set_notes(clip, notes)()

    def _write_notes(self, clip_element: ET.Element, paired: Sequence[tuple[MidiNote, int]]) -> None:
        """Write notes ``_prepare_notes`` checked and numbered."""
        notes_element = get_element(clip_element, Tag.NOTES)
        key_tracks = get_element(notes_element, Tag.KEY_TRACKS)

        by_pitch: dict[int, list[tuple[MidiNote, int]]] = {}
        for note, note_id in paired:
            by_pitch.setdefault(note.pitch, []).append((note, note_id))

        del key_tracks[:]
        for index, pitch in enumerate(sorted(by_pitch)):
            key_track = ET.SubElement(key_tracks, Tag.KEY_TRACK, self._key_track_attributes(index))
            events = ET.SubElement(key_track, Tag.NOTES)
            for note, note_id in sorted(by_pitch[pitch], key=lambda pair: pair[0].start):
                ET.SubElement(events, Tag.MIDI_NOTE_EVENT, self._note_attributes(note, note_id))
            ET.SubElement(key_track, Tag.MIDI_KEY, {Attr.VALUE: str(pitch)})

        _indent_key_tracks(clip_element, key_tracks)
        self._sync_note_bookkeeping(notes_element, max((note_id for _, note_id in paired), default=0) + 1)

    def prepare_clone_clip(
        self,
        donor: MidiClipRef,
        *,
        slot_index: int | None,
        name: str,
        notes: Sequence[MidiNote],
        length: float | None = None,
    ) -> Callable[[], MidiClipRef]:
        """Check a clone of ``donor`` and return it without running it; see :meth:`clone_clip`.

        Whether the slot is still empty is checked when the clone runs, since
        another write can fill it in between.
        """
        track = donor.track
        if slot_index is not None:
            track.session_slot(slot_index)
        clone_length = donor.length if length is None else length
        if clone_length is None:
            raise ValueError("Donor clip carries no loop markers; pass length explicitly")
        if clone_length <= 0:
            raise ValueError(f"Clip length must be positive, got {clone_length}")
        paired = _prepare_notes(notes)

        def run() -> MidiClipRef:
            index = track.first_empty_slot() if slot_index is None else slot_index
            slot = track.session_slot(index)
            if slot.filled:
                raise ValueError(f"Session slot {index} of track {track.name!r} already holds a clip")
            value = slot.value

            clone = copy.deepcopy(donor.clip_element)
            shift_indentation(clone, (tab_depth(slot.holder.text) + 1) - (tab_depth(clone.text) - 1))
            renumber_pointee_ids(clone, self._root)

            # A session clip always starts at 0 and is the only clip in its slot.
            clone.set(Attr.TIME, "0")
            if Attr.ID in clone.attrib:
                clone.set(Attr.ID, "0")
            resolve(clone, Clip.NAME, self.version).set(name)
            resolve(clone, Clip.DISABLED, self.version).set(False)
            resolve(clone, Clip.LOOP_ON, self.version).set(True)
            sections.set_clip_length(clone, clone_length, self.version)
            self._write_notes(clone, paired)

            value.text = (slot.holder.text or "") + "\t"
            clone.tail = slot.holder.text
            value.append(clone)
            return _parse_clip(clone, track, index, self.version)

        return run

    def clone_clip(
        self,
        donor: MidiClipRef,
        *,
        slot_index: int | None,
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

        Two of the donor's flags are placement state and are always reset on
        the copy: a deactivated donor (``Disabled``) would clone into a slot
        that stays silent when launched, and a non-looping arrangement donor
        (``Loop/LoopOn``) into a session clip that plays once and stops.

        ``slot_index`` None takes the track's first empty slot.
        """
        return self.prepare_clone_clip(donor, slot_index=slot_index, name=name, notes=notes, length=length)()

    # --- arrangement sections ---------------------------------------------

    def prepare_place_clip(
        self,
        donor: MidiClipRef | AudioClipRef,
        *,
        track: AbletonTrack,
        at: float,
        length: float | None = None,
        replace: bool = False,
    ) -> Callable[[], MidiClipRef | AudioClipRef]:
        """Check a placement of ``donor`` on ``track`` and return it without running it; see :meth:`place_clip`.

        Whether the span is free is checked when the placement runs, since
        another write can fill or clear it in between.
        """
        events = sections.arrangement_events(track.track_root, track.type, track.version)
        if events is None:
            raise ValueError(f"Track {track.name!r} is a {track.type} and holds no arrangement clips")
        expected = Tag.MIDI_TRACK if isinstance(donor, MidiClipRef) else Tag.AUDIO_TRACK
        if track.type != expected:
            raise ValueError(
                f"A {donor.clip_element.tag} donor needs a {expected}, but track {track.name!r} is a {track.type}"
            )
        if donor.version[0] != self.version[0]:
            raise ValueError(
                f"Cannot place a Live {donor.version[0]}.x clip into a Live {self.version[0]}.x set; "
                "clip schemas are only known to match within a major version"
            )
        if length is not None and length <= 0:
            raise ValueError(f"Clip length must be positive, got {length}")

        def run() -> MidiClipRef | AudioClipRef:
            clone = copy.deepcopy(donor.clip_element)
            sections.move_clip(clone, at, length, self.version)
            span = sections.event_span(clone, self.version)
            sections.clear_span(events, span, replace=replace, track_name=track.name)

            shift_indentation(clone, (tab_depth(events.indent) + 1) - (tab_depth(clone.text) - 1))
            renumber_pointee_ids(clone, self._root)
            resolve(clone, Clip.DISABLED, self.version).set(False)
            sections.assign_event_id(events.element, clone)
            sections.insert_in_time_order(events.element, clone, indent=events.indent)

            if isinstance(donor, MidiClipRef):
                return _parse_clip(clone, track, None, self.version)
            return _parse_audio_clip(clone, track, None, self.version)

        return run

    def place_clip(
        self,
        donor: MidiClipRef | AudioClipRef,
        *,
        track: AbletonTrack,
        at: float,
        length: float | None = None,
        replace: bool = False,
    ) -> MidiClipRef | AudioClipRef:
        """Copy ``donor`` into this set's arrangement as a new event at beat ``at``.

        The donor may sit in a session slot or in an arrangement, in this set
        or in another one of the same major version, the rule
        ``Devices.graft_chain`` applies too: clip schemas are only known to
        match within a major version.

        ``length`` is the arrangement span in beats and defaults to the
        donor's own. A MIDI donor lands on a ``MidiTrack``, an audio donor on
        an ``AudioTrack``, by the same mechanics.

        ``Disabled`` is always written false: a deactivated donor would place
        a clip that silently does nothing. Loop state is kept as the donor
        left it, because non-looping arrangement clips are normal.

        The placement is refused when it would overlap an existing clip's
        ``[Time, CurrentEnd)`` span on the target track. ``replace=True``
        deletes those clips outright instead; nothing is ever trimmed.
        """
        return self.prepare_place_clip(donor, track=track, at=at, length=length, replace=replace)()

    def _split_at_edges(
        self, target: AbletonTrack, events: sections.ArrangementEvents, start: float, end: float
    ) -> tuple[list[ET.Element], list[SkippedStraddler]]:
        """A track's arrangement clips fully inside ``[start, end)``, and those crossing either edge."""
        inside: list[ET.Element] = []
        straddlers: list[SkippedStraddler] = []
        for clip in events.clips():
            span = sections.event_span(clip, self.version)
            boundary = sections.straddled_boundary(span, start, end)
            if boundary is not None:
                straddlers.append(
                    SkippedStraddler(
                        track_name=target.name,
                        clip_name=sections.clip_name(clip, self.version),
                        start=span[0],
                        end=span[1],
                        boundary=boundary,
                    )
                )
            elif start <= span[0] and span[1] <= end:
                inside.append(clip)
        return inside, straddlers

    def prepare_copy_section(
        self,
        src_start: float,
        src_end: float,
        dest_start: float,
        *,
        tracks: Sequence[AbletonTrack] | None = None,
        mode: sections.SectionMode = sections.SectionMode.REFUSE,
    ) -> Callable[[], sections.SectionCopyReport]:
        """Check a section copy and return it without running it; see :meth:`copy_section`.

        What the destination holds is checked when the copy runs, since
        another write can fill or clear it in between.
        """
        if src_start < 0:
            raise ValueError(f"Section start must not be negative, got {src_start}")
        if src_end <= src_start:
            raise ValueError(f"Section end {src_end} must be after its start {src_start}")
        if dest_start < 0:
            raise ValueError(f"Destination start must not be negative, got {dest_start}")
        selected = self._set.tracks.load() if tracks is None else list(tracks)

        def run() -> sections.SectionCopyReport:
            delta = dest_start - src_start
            dest_end = dest_start + (src_end - src_start)
            skipped: list[SkippedStraddler] = []
            planned: list[tuple[AbletonTrack, sections.ArrangementEvents, list[ET.Element]]] = []

            for target in selected:
                events = sections.arrangement_events(target.track_root, target.type, target.version)
                if events is None:
                    continue
                inside, straddlers = self._split_at_edges(target, events, src_start, src_end)
                skipped.extend(straddlers)
                planned.append((target, events, inside))

            collisions: list[str] = []
            for target, events, inside in planned:
                if not inside:
                    continue
                for clip in sections.span_occupants(events, dest_start, dest_end):
                    name = sections.clip_name(clip, self.version)
                    collisions.append(f"{name!r} on track {target.name!r} at beat {clip.get(Attr.TIME)}")
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
                    doomed = sections.span_occupants(events, dest_start, dest_end)
                    remove_children(events.element, doomed)
                    replaced += len(doomed)
                for clone in cloned:
                    start = sections.event_time(clone) + delta
                    sections.move_clip(clone, start, None, self.version)
                    renumber_pointee_ids(clone, self._root)
                    resolve(clone, Clip.DISABLED, self.version).set(False)
                    sections.assign_event_id(events.element, clone)
                    sections.insert_in_time_order(events.element, clone, indent=events.indent)
                    copied.append(
                        CopiedPlacement(
                            track_name=target.name, clip_name=sections.clip_name(clone, self.version), start=start
                        )
                    )

            # Automation belongs to the track, not to its clips: a group track
            # holds envelopes and no clips at all, so this walks every selected
            # track rather than only the ones a copy landed on.
            envelope_events = 0
            for target in selected:
                for envelope in sections.track_envelopes(target.track_root):
                    envelope_events += sections.copy_envelope_segment(
                        envelope, src_start, src_end, delta, replace=mode is sections.SectionMode.REPLACE
                    )

            return sections.SectionCopyReport(
                dest_start=dest_start,
                copied=tuple(copied),
                skipped=tuple(skipped),
                replaced=replaced,
                envelope_events=envelope_events,
            )

        return run

    def copy_section(
        self,
        src_start: float,
        src_end: float,
        dest_start: float,
        *,
        tracks: Sequence[AbletonTrack] | None = None,
        mode: sections.SectionMode = sections.SectionMode.REFUSE,
    ) -> sections.SectionCopyReport:
        """Copy a stretch of arrangement -- clips and track automation -- somewhere else.

        Every MIDI and audio clip *fully inside* ``[src_start, src_end)`` on
        every selected track is duplicated at the same offset into the
        destination. A clip crossing either edge is neither copied nor cut in
        half: it is reported as a straddler and left exactly where it is.

        ``tracks`` selects a subset; ``None`` means every track that has an
        arrangement.

        The destination window is ``[dest_start, dest_start + (src_end -
        src_start))`` and is only inspected on tracks that receive a copy.
        ``SectionMode.REFUSE`` raises listing the collisions;
        ``SectionMode.REPLACE`` deletes the clips fully inside the
        destination first. A clip straddling a *destination* edge is an error
        under both modes, since honouring it would mean trimming.

        Track automation rides along: every envelope event in the source
        range is copied to the same offset, into the same envelope, with the
        envelope's target untouched (``sections.copy_envelope_segment`` covers
        the edges); under ``SectionMode.REPLACE`` the events already in the
        destination are deleted first. Clip envelopes travel inside the copied clip.
        """
        return self.prepare_copy_section(src_start, src_end, dest_start, tracks=tracks, mode=mode)()

    def prepare_remove_section_clips(
        self, start: float, end: float, *, tracks: Sequence[AbletonTrack]
    ) -> Callable[[], sections.SectionRemoveReport]:
        """Check a section removal and return it without running it; see :meth:`remove_section_clips`."""
        if end <= start:
            raise ValueError(f"Section end {end} must be after its start {start}")

        def run() -> sections.SectionRemoveReport:
            removed: list[sections.RemovedClip] = []
            kept: list[SkippedStraddler] = []
            for target in tracks:
                events = sections.arrangement_events(target.track_root, target.type, target.version)
                if events is None:
                    continue
                doomed, straddlers = self._split_at_edges(target, events, start, end)
                kept.extend(straddlers)
                removed.extend(
                    sections.RemovedClip(
                        track_name=target.name,
                        clip_name=sections.clip_name(clip, self.version),
                        start=sections.event_time(clip),
                    )
                    for clip in doomed
                )
                remove_children(events.element, doomed)
            return sections.SectionRemoveReport(removed=tuple(removed), kept=tuple(kept))

        return run

    def remove_section_clips(
        self, start: float, end: float, *, tracks: Sequence[AbletonTrack]
    ) -> sections.SectionRemoveReport:
        """Delete the arrangement clips fully inside ``[start, end)`` on ``tracks``.

        The track list is required: this is the one destructive operation
        here, and "every track" should not follow from a left-out argument.
        Clips crossing either edge are left alone and named in the report's
        ``kept``, never truncated.
        """
        return self.prepare_remove_section_clips(start, end, tracks=tracks)()
