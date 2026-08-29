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

from abletoolz.live_set import sections
from abletoolz.live_set.clips import AudioClipRef, ClipLocation, MidiClipRef, NoteArray, decode_note_array
from abletoolz.live_set.document import AbletonSet
from abletoolz.live_set.sections import SectionMode, SkippedStraddler
from abletoolz.live_set.tracks import AbletonTrack
from abletoolz.misc import get_element
from abletoolz.schema import Tag, path
from abletoolz.schema.devices import SETTINGS_OF, Device


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


class SetDeviceSettingOp(pydantic.BaseModel):
    """Set one setting on every device of a kind, set-wide or on one track.

    ``device`` and ``setting`` are member names from
    :mod:`abletoolz.schema.devices`: ``{"device": "SAMPLER", "setting":
    "VOLUME"}``. ``index`` picks the entry of a setting that lives in a list.
    """

    model_config = pydantic.ConfigDict(extra="forbid")
    op: Literal["set_device_setting"]
    device: str
    setting: str
    value: bool | float | str
    track: TrackSelector | None = None
    index: int | None = None


type Op = Annotated[
    SetNotesOp | CloneClipOp | GraftChainOp | PlaceClipOp | CopySectionOp | RemoveSectionClipsOp | SetDeviceSettingOp,
    pydantic.Field(discriminator="op"),
]

type AnyOp = (
    SetNotesOp | CloneClipOp | GraftChainOp | PlaceClipOp | CopySectionOp | RemoveSectionClipsOp | SetDeviceSettingOp
)


class OpsDocument(pydantic.BaseModel):
    """Top-level ``{"ops": [...]}`` document; the unit ``apply_ops`` validates and runs."""

    model_config = pydantic.ConfigDict(extra="forbid")
    ops: list[Op]


# --- results ----------------------------------------------------------------
#
# One result model per op, tagged with the same ``op`` string the request
# carried. The caller of an ops document is a program -- the whole point of
# this surface -- and a program should not have to read English to find out
# what happened. The prose lives in ``console``; these say the same things as
# fields.


class SetNotesResult(pydantic.BaseModel):
    op: Literal["set_notes"] = "set_notes"
    track: str
    clip: str
    notes: int


class CloneClipResult(pydantic.BaseModel):
    op: Literal["clone_clip"] = "clone_clip"
    track: str
    slot: int
    name: str


class GraftChainResult(pydantic.BaseModel):
    op: Literal["graft_chain"] = "graft_chain"
    target_track: str
    mode: str
    devices: int


class PlaceClipResult(pydantic.BaseModel):
    op: Literal["place_clip"] = "place_clip"
    track: str
    clip: str
    at: float


class PlacementModel(pydantic.BaseModel):
    """A clip that landed somewhere, named by the track it landed on."""

    track: str
    clip: str
    start: float


class StraddlerModel(pydantic.BaseModel):
    """A clip left alone because it crosses a section edge.

    Never silently truncated, which is why it is reported rather than counted:
    a section can read as emptied while these still sound through it.
    """

    track: str
    clip: str
    start: float
    end: float
    crosses: str


class CopySectionResult(pydantic.BaseModel):
    op: Literal["copy_section"] = "copy_section"
    dest_start: float
    copied: list[PlacementModel]
    skipped: list[StraddlerModel]
    replaced: int
    envelope_events: int


class RemoveSectionClipsResult(pydantic.BaseModel):
    op: Literal["remove_section_clips"] = "remove_section_clips"
    removed: list[PlacementModel]
    kept: list[StraddlerModel]


class SetDeviceSettingResult(pydantic.BaseModel):
    op: Literal["set_device_setting"] = "set_device_setting"
    device: str
    setting: str
    value: str
    devices: int


type OpResult = (
    SetNotesResult
    | CloneClipResult
    | GraftChainResult
    | PlaceClipResult
    | CopySectionResult
    | RemoveSectionClipsResult
    | SetDeviceSettingResult
)


def _placement(track: str, clip: str, start: float) -> PlacementModel:
    return PlacementModel(track=track, clip=clip, start=start)


def _straddler(straddler: SkippedStraddler) -> StraddlerModel:
    return StraddlerModel(
        track=straddler.track_name,
        clip=straddler.clip_name,
        start=straddler.start,
        end=straddler.end,
        crosses=str(straddler.boundary),
    )


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
    return sections.session_slots(track.track_root)


def _clip_element_at_slot(track: AbletonTrack, slot_index: int) -> ET.Element:
    """The clip in a slot. This caller needs the slot filled."""
    slot = sections.session_slot(track.track_root, slot_index, track.name)
    if not slot.filled:
        raise ValueError(f"Session slot {slot_index} of track {track.name!r} holds no clip")
    return slot.value[0]


def _first_empty_slot(track: AbletonTrack) -> int:
    for index, slot in enumerate(_session_slots(track)):
        value = get_element(slot, path(Tag.CLIP_SLOT, Tag.VALUE))
        if not len(value):
            return index
    raise ValueError(f"Track {track.name!r} has no empty session slot")


def _track_midi_clips(live_set: AbletonSet, track: AbletonTrack) -> list[MidiClipRef]:
    return [clip for clip in live_set.clips.midi() if live_set.clips.track_of(clip.clip_element) is track]


def _pick_clip[T: MidiClipRef | AudioClipRef](
    clips: Sequence[T], track: AbletonTrack, selector: ClipSelector, *, kind: str
) -> T:
    """One selector to exactly one clip, out of whatever the caller allows.

    ``kind`` only names the clips in the error text; the selector rules are
    the same either way, which is the point of having one implementation.
    """
    if isinstance(selector, ClipBySlot):
        clip_element = _clip_element_at_slot(track, selector.slot)
        ref = next((clip for clip in clips if clip.clip_element is clip_element), None)
        if ref is None:
            raise ValueError(f"Slot {selector.slot} of track {track.name!r} does not hold a {kind} clip")
        return ref
    matches = [
        clip for clip in clips if clip.location is ClipLocation.ARRANGEMENT and clip.start_time == selector.start
    ]
    if not matches:
        raise ValueError(f"No arrangement {kind} clip starts at {selector.start} on track {track.name!r}")
    if len(matches) > 1:
        raise ValueError(f"Multiple arrangement clips start at {selector.start} on track {track.name!r}")
    return matches[0]


def _resolve_clip(live_set: AbletonSet, track: AbletonTrack, selector: ClipSelector) -> MidiClipRef:
    return _pick_clip(_track_midi_clips(live_set, track), track, selector, kind="MIDI")


def _resolve_any_clip(live_set: AbletonSet, track: AbletonTrack, selector: ClipSelector) -> MidiClipRef | AudioClipRef:
    """The same selector rules as ``_resolve_clip``, but a donor may be audio.

    ``place_clip`` copies whole clips rather than notes, so an ``AudioClip``
    is as good a donor as a ``MidiClip`` and both are looked up here.
    """
    every: list[MidiClipRef | AudioClipRef] = [*live_set.clips.midi(), *live_set.clips.audio()]
    clips = [clip for clip in every if live_set.clips.track_of(clip.clip_element) is track]
    return _pick_clip(clips, track, selector, kind="readable")


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


def _prepare_set_notes(live_set: AbletonSet, op: SetNotesOp) -> Callable[[], OpResult]:
    track = _resolve_track(live_set, op.track)
    clip = _resolve_clip(live_set, track, op.clip)
    notes = [decode_note_array(row) for row in op.notes]

    def run() -> OpResult:
        live_set.clips.set_notes(clip, notes)
        return SetNotesResult(track=track.name, clip=clip.name, notes=len(notes))

    return run


def _prepare_clone_clip(live_set: AbletonSet, op: CloneClipOp) -> Callable[[], OpResult]:
    track = _resolve_track(live_set, op.track)
    donor = _resolve_clip(live_set, track, op.donor)
    notes = [decode_note_array(row) for row in op.notes]
    slot_count = len(_session_slots(track))
    if op.slot is not None and not 0 <= op.slot < slot_count:
        raise ValueError(f"Track {track.name!r} has {slot_count} session slots; no slot {op.slot}")

    def run() -> OpResult:
        slot = _first_empty_slot(track) if op.slot is None else op.slot
        live_set.clips.clone_clip(donor, slot_index=slot, name=op.name, notes=notes, length=op.length)
        return CloneClipResult(track=track.name, slot=slot, name=op.name)

    return run


def _prepare_graft_chain(
    live_set: AbletonSet, op: GraftChainOp, donor_sets: dict[str, AbletonSet]
) -> Callable[[], OpResult]:
    donor_set = live_set if op.donor_set is None else _open_donor_set(op.donor_set, donor_sets)
    donor_name = _resolve_track_name(donor_set, op.donor_track)
    donor_chain = next((chain for chain in donor_set.devices.inventory() if chain.track_name == donor_name), None)
    if donor_chain is None:
        raise ValueError(f"No device chain for track {donor_name!r}")
    target_name = _resolve_track_name(live_set, op.target_track)

    def run() -> OpResult:
        grafted = live_set.devices.graft_chain(donor_chain, target_name, mode=op.mode)
        return GraftChainResult(target_track=target_name, mode=op.mode, devices=len(grafted))

    return run


def _prepare_place_clip(
    live_set: AbletonSet, op: PlaceClipOp, donor_sets: dict[str, AbletonSet]
) -> Callable[[], OpResult]:
    donor_set = live_set if op.donor_set is None else _open_donor_set(op.donor_set, donor_sets)
    donor_track = _resolve_track(donor_set, op.donor_track)
    donor = _resolve_any_clip(donor_set, donor_track, op.donor)
    target = _resolve_track(live_set, op.target_track)
    expected = Tag.MIDI_TRACK if isinstance(donor, MidiClipRef) else Tag.AUDIO_TRACK
    if target.type != expected:
        raise ValueError(f"place_clip needs a {expected} for this donor; track {target.name!r} is a {target.type}")
    if donor.version[0] != live_set.version_tuple[0]:
        raise ValueError(
            f"place_clip donor is from Live {donor.version[0]}.x but the set is Live {live_set.version_tuple[0]}.x"
        )

    def run() -> OpResult:
        placed = live_set.clips.place_clip(donor, track=target, at=op.at, length=op.length, replace=op.replace)
        return PlaceClipResult(track=target.name, clip=placed.name, at=op.at)

    return run


def _prepare_copy_section(live_set: AbletonSet, op: CopySectionOp) -> Callable[[], OpResult]:
    _resolve_track_names(live_set, op.tracks)
    if op.src_end <= op.src_start:
        raise ValueError(f"copy_section end {op.src_end} must be after its start {op.src_start}")
    if op.src_start < 0 or op.dest_start < 0:
        raise ValueError("copy_section positions must not be negative")

    def run() -> OpResult:
        report = live_set.clips.copy_section(
            op.src_start,
            op.src_end,
            op.dest_start,
            track_names=op.tracks,
            mode=SectionMode(op.mode),
        )
        return CopySectionResult(
            dest_start=report.dest_start,
            copied=[_placement(place.track_name, place.clip_name, place.start) for place in report.copied],
            skipped=[_straddler(straddler) for straddler in report.skipped],
            replaced=report.replaced,
            envelope_events=report.envelope_events,
        )

    return run


def _prepare_remove_section_clips(live_set: AbletonSet, op: RemoveSectionClipsOp) -> Callable[[], OpResult]:
    _resolve_track_names(live_set, op.tracks)
    if op.end <= op.start:
        raise ValueError(f"remove_section_clips end {op.end} must be after its start {op.start}")

    def run() -> OpResult:
        report = live_set.clips.remove_section_clips(op.start, op.end, track_names=op.tracks)
        return RemoveSectionClipsResult(
            removed=[_placement(clip.track_name, clip.clip_name, clip.start) for clip in report.removed],
            kept=[_straddler(straddler) for straddler in report.kept],
        )

    return run


def _prepare_set_device_setting(live_set: AbletonSet, op: SetDeviceSettingOp) -> Callable[[], OpResult]:
    if op.device not in Device.__members__:
        raise ValueError(f"set_device_setting: no device {op.device!r}; see abletoolz.schema.devices.Device")
    device = Device[op.device]
    if device not in SETTINGS_OF:
        raise ValueError(f"set_device_setting: {op.device} is hosted; its state is the plugin's own")
    settings = SETTINGS_OF[device]
    if op.setting not in settings.__members__:
        raise ValueError(f"set_device_setting: {settings.__name__} has no setting {op.setting!r}")
    setting = settings[op.setting]
    track_name = None if op.track is None else _resolve_track_name(live_set, op.track)
    indices = {} if op.index is None else {"index": op.index}
    # Resolving every target and encoding the value now is what makes a bad op fail before any edit.
    targets = [ref.setting(setting, **indices) for ref in live_set.devices.walk(device, track_name=track_name)]
    if not targets:
        where = "this set" if track_name is None else f"track {track_name!r}"
        raise ValueError(f"set_device_setting: no {op.device} in {where}")
    text = targets[0].encode(op.value)

    def run() -> OpResult:
        for target in targets:
            target.set(op.value)
        return SetDeviceSettingResult(device=op.device, setting=op.setting, value=text, devices=len(targets))

    return run


def _prepare(live_set: AbletonSet, op: AnyOp, donor_sets: dict[str, AbletonSet]) -> Callable[[], OpResult]:
    if isinstance(op, SetDeviceSettingOp):
        return _prepare_set_device_setting(live_set, op)
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


def apply_ops(live_set: AbletonSet, ops: Sequence[AnyOp]) -> list[OpResult]:
    """Validate every op, then execute them in order. Raises before any mutation.

    Validation resolves every selector, checks every slot exists, and parses
    every donor set -- the whole document, before the first XML edit. The
    caller saves; this only mutates the in-memory tree.

    One result per op, in request order, each tagged with the op that produced
    it. Nothing here returns prose: the caller is a program.
    """
    donor_sets: dict[str, AbletonSet] = {}
    prepared = [_prepare(live_set, op, donor_sets) for op in ops]
    return [action() for action in prepared]
