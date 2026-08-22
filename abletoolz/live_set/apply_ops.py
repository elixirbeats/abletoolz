"""Batch write surface: an ops document an LLM can emit, validated whole before any mutation.

``apply_ops`` sits on top of the domain writers already proven by
``clips.py``/``devices.py`` (``set_notes``, ``clone_clip``, ``place_clip``,
``copy_section``, ``remove_section_clips``, ``graft_chain``); it does not
touch XML itself. Its job is turning a JSON ops document into calls against
that API, resolving every selector up front so a document with one good op
and one bad one aborts before either runs.

Validation covers what an earlier op in the same document cannot change:
selectors resolve, section ranges point forwards, a ``place_clip`` donor and
its target track agree on kind and major version. What an earlier op *can*
change -- whether a slot is free, whether a destination section is occupied
-- stays an execution-time check, the same split ``clone_clip``'s
``slot: null`` already had.

Track selectors resolve by identity, not name: duplicate track names are
real, so ``{"name": ...}`` raises on ambiguity rather than guessing, and
``{"id": ...}`` walks ``Clips.track_of`` the same way ``describe()`` does.
The one exception is ``graft_chain``, whose target lives on
``Devices.graft_chain(donor, target_track_name)`` -- a name-keyed API this
phase does not change. An id selector there is resolved to a track first and
then checked for a same-named twin before being handed off as a string, so
it fails loudly instead of silently grafting onto the wrong one; it still
can't reach a track whose only unique handle is its id.
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable, Sequence
from typing import Annotated, Literal
from xml.etree import ElementTree as ET

import pydantic

from abletoolz.live_set.clips import AudioClipRef, ClipLocation, MidiClipRef, NoteArray, decode_note_array
from abletoolz.live_set.document import AbletonSet
from abletoolz.live_set.sections import SectionMode
from abletoolz.live_set.tracks import AbletonTrack
from abletoolz.misc import get_element


class TrackById(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    id: int


class TrackByName(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    name: str


type TrackSelector = TrackById | TrackByName


class ClipBySlot(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    slot: int


class ClipByStart(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    start: float


type ClipSelector = ClipBySlot | ClipByStart


class SetNotesOp(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    op: Literal["set_notes"]
    track: TrackSelector
    clip: ClipSelector
    notes: list[NoteArray]


class CloneClipOp(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    op: Literal["clone_clip"]
    track: TrackSelector
    donor: ClipSelector
    slot: int | None = None
    name: str
    length: float | None = None
    notes: list[NoteArray]


class GraftChainOp(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    op: Literal["graft_chain"]
    donor_set: str | None = None
    donor_track: TrackSelector
    target_track: TrackSelector
    mode: Literal["append", "replace"] = "append"


class PlaceClipOp(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    op: Literal["place_clip"]
    donor_set: str | None = None
    donor_track: TrackSelector
    donor: ClipSelector
    target_track: TrackSelector
    at: float
    length: float | None = None
    replace: bool = False


class CopySectionOp(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    op: Literal["copy_section"]
    src_start: float
    src_end: float
    dest_start: float
    tracks: list[str] | None = None
    mode: Literal["refuse", "replace"] = "refuse"


class RemoveSectionClipsOp(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    op: Literal["remove_section_clips"]
    start: float
    end: float
    tracks: list[str]


type Op = Annotated[
    SetNotesOp | CloneClipOp | GraftChainOp | PlaceClipOp | CopySectionOp | RemoveSectionClipsOp,
    pydantic.Field(discriminator="op"),
]

type AnyOp = SetNotesOp | CloneClipOp | GraftChainOp | PlaceClipOp | CopySectionOp | RemoveSectionClipsOp


class OpsDocument(pydantic.BaseModel):
    """Top-level ``{"ops": [...]}`` document; the unit ``apply_ops`` validates and runs."""

    model_config = pydantic.ConfigDict(extra="forbid")
    ops: list[Op]


# --- track/clip selector resolution -----------------------------------------


def _resolve_track(live_set: AbletonSet, selector: TrackSelector) -> AbletonTrack:
    """A selector to exactly one regular track (not the main track), by identity."""
    tracks = live_set.tracks.load()
    if isinstance(selector, TrackById):
        for track in tracks:
            if track.id is not None and int(track.id) == selector.id:
                return track
        raise ValueError(f"No track with id {selector.id}")
    matches = [track for track in tracks if track.name == selector.name]
    if not matches:
        raise ValueError(f"No track named {selector.name!r}")
    if len(matches) > 1:
        raise ValueError(f"{len(matches)} tracks are named {selector.name!r}; use {{'id': ...}} instead")
    return matches[0]


def _resolve_track_name(live_set: AbletonSet, selector: TrackSelector) -> str:
    """Selector -> a name safe to hand to ``Devices.graft_chain``, which is name-keyed.

    An id selector is resolved to a track and then checked for a same-named
    twin: ``graft_chain`` has no id-based entry point, so an id that only
    disambiguates by number -- not by name -- still can't be aimed safely.
    """
    if isinstance(selector, TrackByName):
        matches = [track for track in live_set.tracks.load() if track.name == selector.name]
        if len(matches) > 1:
            raise ValueError(f"{len(matches)} tracks are named {selector.name!r}; graft_chain resolves by name")
        if matches:
            return selector.name
        main_name = live_set.devices.inventory()[-1].track_name
        if main_name == selector.name:
            return selector.name
        raise ValueError(f"No track named {selector.name!r}")
    track = _resolve_track(live_set, selector)
    twins = [candidate for candidate in live_set.tracks.load() if candidate.name == track.name]
    if len(twins) > 1:
        raise ValueError(
            f"{len(twins)} tracks share the name {track.name!r} as track id {selector.id}; "
            "graft_chain resolves donor/target by name, so this id cannot be aimed unambiguously"
        )
    return track.name


def _session_slots(track: AbletonTrack) -> list[ET.Element]:
    return list(track.track_root.iterfind("DeviceChain/MainSequencer/ClipSlotList/ClipSlot"))


def _clip_element_at_slot(track: AbletonTrack, slot_index: int) -> ET.Element:
    slots = _session_slots(track)
    if not 0 <= slot_index < len(slots):
        raise ValueError(f"Track {track.name!r} has {len(slots)} session slots; no slot {slot_index}")
    value = get_element(slots[slot_index], "ClipSlot.Value")
    if not len(value):
        raise ValueError(f"Session slot {slot_index} of track {track.name!r} holds no clip")
    return value[0]


def _first_empty_slot(track: AbletonTrack) -> int:
    for index, slot in enumerate(_session_slots(track)):
        value = get_element(slot, "ClipSlot.Value")
        if not len(value):
            return index
    raise ValueError(f"Track {track.name!r} has no empty session slot")


def _track_midi_clips(live_set: AbletonSet, track: AbletonTrack) -> list[MidiClipRef]:
    return [clip for clip in live_set.clips.midi() if live_set.clips.track_of(clip.clip_element) is track]


def _resolve_clip(live_set: AbletonSet, track: AbletonTrack, selector: ClipSelector) -> MidiClipRef:
    clips = _track_midi_clips(live_set, track)
    if isinstance(selector, ClipBySlot):
        clip_element = _clip_element_at_slot(track, selector.slot)
        ref = next((clip for clip in clips if clip.clip_element is clip_element), None)
        if ref is None:
            raise ValueError(f"Slot {selector.slot} of track {track.name!r} does not hold a MIDI clip")
        return ref
    matches = [
        clip for clip in clips if clip.location is ClipLocation.ARRANGEMENT and clip.start_time == selector.start
    ]
    if not matches:
        raise ValueError(f"No arrangement MIDI clip starts at {selector.start} on track {track.name!r}")
    if len(matches) > 1:
        raise ValueError(f"Multiple arrangement clips start at {selector.start} on track {track.name!r}")
    return matches[0]


def _resolve_any_clip(live_set: AbletonSet, track: AbletonTrack, selector: ClipSelector) -> MidiClipRef | AudioClipRef:
    """The same selector rules as ``_resolve_clip``, but a donor may be audio.

    ``place_clip`` copies whole clips rather than notes, so an ``AudioClip``
    is as good a donor as a ``MidiClip`` and both are looked up here.
    """
    every: list[MidiClipRef | AudioClipRef] = [*live_set.clips.midi(), *live_set.clips.audio()]
    clips = [clip for clip in every if live_set.clips.track_of(clip.clip_element) is track]
    if isinstance(selector, ClipBySlot):
        clip_element = _clip_element_at_slot(track, selector.slot)
        ref = next((clip for clip in clips if clip.clip_element is clip_element), None)
        if ref is None:
            raise ValueError(f"Slot {selector.slot} of track {track.name!r} does not hold a readable clip")
        return ref
    matches = [
        clip for clip in clips if clip.location is ClipLocation.ARRANGEMENT and clip.start_time == selector.start
    ]
    if not matches:
        raise ValueError(f"No arrangement clip starts at {selector.start} on track {track.name!r}")
    if len(matches) > 1:
        raise ValueError(f"Multiple arrangement clips start at {selector.start} on track {track.name!r}")
    return matches[0]


def _resolve_track_names(live_set: AbletonSet, names: Sequence[str] | None) -> None:
    """Fail now on a name that names no track or two, before anything is written."""
    for name in names or ():
        _resolve_track(live_set, TrackByName(name=name))


def _open_donor_set(path: str, cache: dict[str, AbletonSet]) -> AbletonSet:
    if path not in cache:
        donor = AbletonSet(pathlib.Path(path))
        if not donor.parse():
            raise ValueError(f"Could not parse donor set {path!r}")
        cache[path] = donor
    return cache[path]


# --- op preparation: resolve everything, mutate nothing ---------------------


def _prepare_set_notes(live_set: AbletonSet, op: SetNotesOp) -> Callable[[], str]:
    track = _resolve_track(live_set, op.track)
    clip = _resolve_clip(live_set, track, op.clip)
    notes = [decode_note_array(row) for row in op.notes]

    def run() -> str:
        live_set.clips.set_notes(clip, notes)
        return f"set_notes: wrote {len(notes)} note(s) to {clip.name!r} on track {track.name!r}"

    return run


def _prepare_clone_clip(live_set: AbletonSet, op: CloneClipOp) -> Callable[[], str]:
    track = _resolve_track(live_set, op.track)
    donor = _resolve_clip(live_set, track, op.donor)
    notes = [decode_note_array(row) for row in op.notes]
    slot_count = len(_session_slots(track))
    if op.slot is not None and not 0 <= op.slot < slot_count:
        raise ValueError(f"Track {track.name!r} has {slot_count} session slots; no slot {op.slot}")

    def run() -> str:
        slot = _first_empty_slot(track) if op.slot is None else op.slot
        live_set.clips.clone_clip(donor, slot_index=slot, name=op.name, notes=notes, length=op.length)
        return f"clone_clip: cloned into slot {slot} of track {track.name!r} as {op.name!r}"

    return run


def _prepare_graft_chain(
    live_set: AbletonSet, op: GraftChainOp, donor_sets: dict[str, AbletonSet]
) -> Callable[[], str]:
    donor_set = live_set if op.donor_set is None else _open_donor_set(op.donor_set, donor_sets)
    donor_name = _resolve_track_name(donor_set, op.donor_track)
    donor_chain = next((chain for chain in donor_set.devices.inventory() if chain.track_name == donor_name), None)
    if donor_chain is None:
        raise ValueError(f"No device chain for track {donor_name!r}")
    target_name = _resolve_track_name(live_set, op.target_track)

    def run() -> str:
        grafted = live_set.devices.graft_chain(donor_chain, target_name, mode=op.mode)
        return f"graft_chain: grafted {len(grafted)} device(s) onto track {target_name!r} ({op.mode})"

    return run


def _prepare_place_clip(live_set: AbletonSet, op: PlaceClipOp, donor_sets: dict[str, AbletonSet]) -> Callable[[], str]:
    donor_set = live_set if op.donor_set is None else _open_donor_set(op.donor_set, donor_sets)
    donor_track = _resolve_track(donor_set, op.donor_track)
    donor = _resolve_any_clip(donor_set, donor_track, op.donor)
    target = _resolve_track(live_set, op.target_track)
    expected = "MidiTrack" if isinstance(donor, MidiClipRef) else "AudioTrack"
    if target.type != expected:
        raise ValueError(f"place_clip needs a {expected} for this donor; track {target.name!r} is a {target.type}")
    if donor.version[0] != live_set.version_tuple[0]:
        raise ValueError(
            f"place_clip donor is from Live {donor.version[0]}.x but the set is Live {live_set.version_tuple[0]}.x"
        )

    def run() -> str:
        placed = live_set.clips.place_clip(donor, track=target, at=op.at, length=op.length, replace=op.replace)
        return f"place_clip: placed {placed.name!r} on track {target.name!r} at beat {op.at}"

    return run


def _prepare_copy_section(live_set: AbletonSet, op: CopySectionOp) -> Callable[[], str]:
    _resolve_track_names(live_set, op.tracks)
    if op.src_end <= op.src_start:
        raise ValueError(f"copy_section end {op.src_end} must be after its start {op.src_start}")
    if op.src_start < 0 or op.dest_start < 0:
        raise ValueError("copy_section positions must not be negative")

    def run() -> str:
        report = live_set.clips.copy_section(
            op.src_start,
            op.src_end,
            op.dest_start,
            track_names=op.tracks,
            mode=SectionMode(op.mode),
        )
        return f"copy_section: {'; '.join(report.lines())}"

    return run


def _prepare_remove_section_clips(live_set: AbletonSet, op: RemoveSectionClipsOp) -> Callable[[], str]:
    _resolve_track_names(live_set, op.tracks)
    if op.end <= op.start:
        raise ValueError(f"remove_section_clips end {op.end} must be after its start {op.start}")

    def run() -> str:
        lines = live_set.clips.remove_section_clips(op.start, op.end, track_names=op.tracks)
        return f"remove_section_clips: {'; '.join(lines) if lines else 'nothing to delete'}"

    return run


def _prepare(live_set: AbletonSet, op: AnyOp, donor_sets: dict[str, AbletonSet]) -> Callable[[], str]:
    if isinstance(op, SetNotesOp):
        return _prepare_set_notes(live_set, op)
    if isinstance(op, CloneClipOp):
        return _prepare_clone_clip(live_set, op)
    if isinstance(op, PlaceClipOp):
        return _prepare_place_clip(live_set, op, donor_sets)
    if isinstance(op, CopySectionOp):
        return _prepare_copy_section(live_set, op)
    if isinstance(op, RemoveSectionClipsOp):
        return _prepare_remove_section_clips(live_set, op)
    return _prepare_graft_chain(live_set, op, donor_sets)


def apply_ops(live_set: AbletonSet, ops: Sequence[AnyOp]) -> list[str]:
    """Validate every op, then execute them in order. Raises before any mutation.

    Validation resolves every selector, checks every slot exists, and parses
    every donor set -- the whole document, before the first XML edit. The
    caller saves; this only mutates the in-memory tree.
    """
    donor_sets: dict[str, AbletonSet] = {}
    prepared = [_prepare(live_set, op, donor_sets) for op in ops]
    return [action() for action in prepared]
