"""Batch write surface: an ops document an LLM can emit, validated whole before any mutation.

``apply_ops`` turns a JSON ops document into calls against the domain writers
of ``clips.py``/``devices.py`` (``set_notes``, ``clone_clip``, ``place_clip``,
``copy_section``, ``remove_section_clips``, ``graft_chain``) and the setting
accessors; it does not touch XML itself.

Each writer owns its own checks: ``prepare_set_notes``, ``prepare_graft_chain``
and the rest refuse what they would refuse anyway and hand back the write
without running it. Every selector is resolved and every op prepared before
any write runs, so a document with one good op and one bad one aborts before
either runs. What an earlier op in the same document *can* change -- whether a
slot is free, whether a destination section is occupied -- stays a check the
write makes when it runs, and a write that fails there puts the whole tree
back the way it was before the first op ran.

Track selectors resolve by identity, not name: duplicate track names are
real, so ``{"name": ...}`` raises on ambiguity rather than guessing, and
``{"id": ...}`` matches the ``Id`` ``describe()`` reports for the track.
Writers take the tracks the selectors resolved to, never a name.
"""

from __future__ import annotations

import enum
import pathlib
from collections.abc import Callable, Sequence
from typing import Annotated, Literal

import pydantic

from abletoolz.live_set.clips import AudioClipRef, ClipLocation, MidiClipRef, NoteArray, decode_note_array
from abletoolz.live_set.document import AbletonSet
from abletoolz.live_set.sections import SectionMode, SkippedStraddler
from abletoolz.live_set.tracks import AbletonTrack
from abletoolz.schema.devices import SETTINGS_OF, Device
from abletoolz.schema.objects import Clip, Mixer, Track, Transport


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


class SetTrackSettingOp(pydantic.BaseModel):
    """Set one track or mixer setting on one track, or on every track.

    ``setting`` names the enum and the member: ``"Track.COLOR"``,
    ``"Mixer.VOLUME"`` (:mod:`abletoolz.schema.objects`). Without ``track``
    it lands on every track that has it, the main track included; ``track``
    may name the main track. ``index`` picks the entry of a setting that lives
    in a list (a send, an automation lane).
    """

    model_config = pydantic.ConfigDict(extra="forbid")
    op: Literal["set_track_setting"]
    setting: str
    value: bool | float | str
    track: TrackSelector | None = None
    index: int | None = None


class SetClipSettingOp(pydantic.BaseModel):
    """Set one setting of one clip: ``{"setting": "LOOP_ON", "value": false}``, a ``Clip`` member name."""

    model_config = pydantic.ConfigDict(extra="forbid")
    op: Literal["set_clip_setting"]
    track: TrackSelector
    clip: ClipSelector
    setting: str
    value: bool | float | str
    index: int | None = None


class SetTransportSettingOp(pydantic.BaseModel):
    """Set one of the set's transport settings: ``{"setting": "LOOP_ON", "value": true}``, a ``Transport`` member."""

    model_config = pydantic.ConfigDict(extra="forbid")
    op: Literal["set_transport_setting"]
    setting: str
    value: bool | float | str


type AnyOp = (
    SetNotesOp
    | CloneClipOp
    | GraftChainOp
    | PlaceClipOp
    | CopySectionOp
    | RemoveSectionClipsOp
    | SetDeviceSettingOp
    | SetTrackSettingOp
    | SetClipSettingOp
    | SetTransportSettingOp
)

type Op = Annotated[AnyOp, pydantic.Field(discriminator="op")]


class OpsDocument(pydantic.BaseModel):
    """Top-level ``{"ops": [...]}`` document; the unit ``apply_ops`` validates and runs."""

    model_config = pydantic.ConfigDict(extra="forbid")
    ops: list[Op]


# --- results ----------------------------------------------------------------
#
# One result model per op, tagged with the same ``op`` string the request
# carried. The caller is a program, which should not have to read English to
# find out what happened: the prose lives in ``console``, and these say the
# same things as fields.


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

    Reported rather than counted: a section can read as emptied while these
    still sound through it.
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


class SetTrackSettingResult(pydantic.BaseModel):
    op: Literal["set_track_setting"] = "set_track_setting"
    setting: str
    value: str
    tracks: list[str]


class SetClipSettingResult(pydantic.BaseModel):
    op: Literal["set_clip_setting"] = "set_clip_setting"
    track: str
    clip: str
    setting: str
    value: str


class SetTransportSettingResult(pydantic.BaseModel):
    op: Literal["set_transport_setting"] = "set_transport_setting"
    setting: str
    value: str


type OpResult = (
    SetNotesResult
    | CloneClipResult
    | GraftChainResult
    | PlaceClipResult
    | CopySectionResult
    | RemoveSectionClipsResult
    | SetDeviceSettingResult
    | SetTrackSettingResult
    | SetClipSettingResult
    | SetTransportSettingResult
)


def _straddler(straddler: SkippedStraddler) -> StraddlerModel:
    return StraddlerModel(
        track=straddler.track_name,
        clip=straddler.clip_name,
        start=straddler.start,
        end=straddler.end,
        crosses=str(straddler.boundary),
    )


# --- track/clip selector resolution -----------------------------------------


_USE_THE_ID = "use {'id': ...} instead"


def _resolve_track(live_set: AbletonSet, selector: TrackSelector, *, main: bool = False) -> AbletonTrack:
    """A selector to exactly one track, by identity; ``main`` lets the main track answer a name (it has no id)."""
    if isinstance(selector, TrackById):
        return live_set.tracks.by_id(selector.id)
    return live_set.tracks.named(selector.name, main=main, advice=_USE_THE_ID)


def _pick_clip[T: MidiClipRef | AudioClipRef](
    clips: Sequence[T], track: AbletonTrack, selector: ClipSelector, *, kind: str
) -> T:
    """One selector to exactly one clip, out of whatever the caller allows; ``kind`` only names them in errors."""
    if isinstance(selector, ClipBySlot):
        if not track.session_slot(selector.slot).filled:
            raise ValueError(f"Session slot {selector.slot} of track {track.name!r} holds no clip")
        ref = next((clip for clip in clips if clip.slot == selector.slot), None)
        if ref is None:
            raise ValueError(f"Slot {selector.slot} of track {track.name!r} does not hold a {kind} clip")
        return ref
    # describe() prints starts rounded to 4 decimals, so a start is matched at that precision.
    matches = [
        clip
        for clip in clips
        if clip.location is ClipLocation.ARRANGEMENT and round(clip.start_time, 4) == round(selector.start, 4)
    ]
    if not matches:
        raise ValueError(f"No arrangement {kind} clip starts at {selector.start} on track {track.name!r}")
    if len(matches) > 1:
        raise ValueError(f"Multiple arrangement clips start at {selector.start} on track {track.name!r}")
    return matches[0]


def _resolve_clip(live_set: AbletonSet, track: AbletonTrack, selector: ClipSelector) -> MidiClipRef:
    return _pick_clip(live_set.clips.midi(track), track, selector, kind="MIDI")


def _resolve_any_clip(live_set: AbletonSet, track: AbletonTrack, selector: ClipSelector) -> MidiClipRef | AudioClipRef:
    """``_resolve_clip`` for an op that takes whole clips, where an ``AudioClip`` serves as well as a ``MidiClip``."""
    clips: list[MidiClipRef | AudioClipRef] = [*live_set.clips.midi(track), *live_set.clips.audio(track)]
    return _pick_clip(clips, track, selector, kind="readable")


def _resolve_track_names(live_set: AbletonSet, names: Sequence[str]) -> list[AbletonTrack]:
    """Each name to the one track it names; a name that names no track or two is refused."""
    return [live_set.tracks.named(name, advice=_USE_THE_ID) for name in names]


def _open_donor_set(path: str, cache: dict[str, AbletonSet]) -> AbletonSet:
    if path not in cache:
        donor = AbletonSet(pathlib.Path(path))
        if not donor.parse():
            raise ValueError(f"Could not parse donor set {path!r}")
        cache[path] = donor
    return cache[path]


# --- op preparation: resolve everything, mutate nothing ---------------------


def _require_in_set(live_set: AbletonSet, clip: MidiClipRef | AudioClipRef) -> None:
    """Refuse a clip an earlier op in the same document removed: an edit to it would land nowhere."""
    if not any(element is clip.clip_element for element in live_set.root.iter(clip.clip_element.tag)):
        raise ValueError(f"Clip {clip.name!r} on {clip.track.name!r} was removed by an earlier op")


def _prepare_set_notes(live_set: AbletonSet, op: SetNotesOp) -> Callable[[], OpResult]:
    track = _resolve_track(live_set, op.track)
    clip = _resolve_clip(live_set, track, op.clip)
    notes = [decode_note_array(row) for row in op.notes]
    write = live_set.clips.prepare_set_notes(clip, notes)

    def run() -> OpResult:
        _require_in_set(live_set, clip)
        write()
        return SetNotesResult(track=track.name, clip=clip.name, notes=len(notes))

    return run


def _prepare_clone_clip(live_set: AbletonSet, op: CloneClipOp) -> Callable[[], OpResult]:
    track = _resolve_track(live_set, op.track)
    donor = _resolve_clip(live_set, track, op.donor)
    notes = [decode_note_array(row) for row in op.notes]
    clone = live_set.clips.prepare_clone_clip(donor, slot_index=op.slot, name=op.name, notes=notes, length=op.length)

    def run() -> OpResult:
        _require_in_set(live_set, donor)
        cloned = clone()
        assert cloned.slot is not None  # a clone always lands in a session slot
        return CloneClipResult(track=track.name, slot=cloned.slot, name=op.name)

    return run


def _prepare_graft_chain(
    live_set: AbletonSet, op: GraftChainOp, donor_sets: dict[str, AbletonSet]
) -> Callable[[], OpResult]:
    donor_set = live_set if op.donor_set is None else _open_donor_set(op.donor_set, donor_sets)
    donor = donor_set.devices.chain(_resolve_track(donor_set, op.donor_track, main=True))
    target = _resolve_track(live_set, op.target_track, main=True)
    graft = live_set.devices.prepare_graft_chain(donor, target, mode=op.mode)

    def run() -> OpResult:
        grafted = graft()
        return GraftChainResult(target_track=target.name, mode=op.mode, devices=len(grafted))

    return run


def _prepare_place_clip(
    live_set: AbletonSet, op: PlaceClipOp, donor_sets: dict[str, AbletonSet]
) -> Callable[[], OpResult]:
    donor_set = live_set if op.donor_set is None else _open_donor_set(op.donor_set, donor_sets)
    donor_track = _resolve_track(donor_set, op.donor_track)
    donor = _resolve_any_clip(donor_set, donor_track, op.donor)
    target = _resolve_track(live_set, op.target_track)
    place = live_set.clips.prepare_place_clip(donor, track=target, at=op.at, length=op.length, replace=op.replace)

    def run() -> OpResult:
        if donor_set is live_set:
            _require_in_set(live_set, donor)
        placed = place()
        return PlaceClipResult(track=target.name, clip=placed.name, at=op.at)

    return run


def _prepare_copy_section(live_set: AbletonSet, op: CopySectionOp) -> Callable[[], OpResult]:
    tracks = None if op.tracks is None else _resolve_track_names(live_set, op.tracks)
    copy = live_set.clips.prepare_copy_section(
        op.src_start, op.src_end, op.dest_start, tracks=tracks, mode=SectionMode(op.mode)
    )

    def run() -> OpResult:
        report = copy()
        return CopySectionResult(
            dest_start=report.dest_start,
            copied=[
                PlacementModel(track=place.track_name, clip=place.clip_name, start=place.start)
                for place in report.copied
            ],
            skipped=[_straddler(straddler) for straddler in report.skipped],
            replaced=report.replaced,
            envelope_events=report.envelope_events,
        )

    return run


def _prepare_remove_section_clips(live_set: AbletonSet, op: RemoveSectionClipsOp) -> Callable[[], OpResult]:
    remove = live_set.clips.prepare_remove_section_clips(
        op.start, op.end, tracks=_resolve_track_names(live_set, op.tracks)
    )

    def run() -> OpResult:
        report = remove()
        return RemoveSectionClipsResult(
            removed=[
                PlacementModel(track=clip.track_name, clip=clip.clip_name, start=clip.start) for clip in report.removed
            ],
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
    track = None if op.track is None else _resolve_track(live_set, op.track, main=True)
    indices = {} if op.index is None else {"index": op.index}
    # Resolving every target and encoding the value now is what makes a bad op fail before any edit.
    targets = [ref.setting(setting, **indices) for ref in live_set.devices.walk(device, track=track)]
    if not targets:
        where = "this set" if track is None else f"track {track.name!r}"
        raise ValueError(f"set_device_setting: no {op.device} in {where}")
    text = targets[0].encode(op.value)

    def run() -> OpResult:
        for target in targets:
            target.set(op.value)
        return SetDeviceSettingResult(device=op.device, setting=op.setting, value=text, devices=len(targets))

    return run


def _member[E: enum.StrEnum](op: str, text: str, settings_class: type[E]) -> E:
    if text not in settings_class.__members__:
        raise ValueError(f"{op}: {settings_class.__name__} has no setting {text!r}")
    return settings_class[text]


def _track_member(text: str) -> Track | Mixer:
    """``"Track.COLOR"`` or ``"Mixer.VOLUME"`` to its member."""
    class_name, _, name = text.partition(".")
    classes: dict[str, type[Track] | type[Mixer]] = {"Track": Track, "Mixer": Mixer}
    if class_name not in classes or not name:
        raise ValueError(f"set_track_setting: name the enum and the member, e.g. 'Mixer.VOLUME', not {text!r}")
    return _member("set_track_setting", name, classes[class_name])


def _prepare_set_track_setting(live_set: AbletonSet, op: SetTrackSettingOp) -> Callable[[], OpResult]:
    member = _track_member(op.setting)
    indices = {} if op.index is None else {"index": op.index}
    if op.track is not None:
        track = _resolve_track(live_set, op.track, main=True)
        targets = [(track, track.setting(member, **indices))]
    else:
        found = [(track, track.find_setting(member, **indices)) for track in live_set.tracks.walk()]
        targets = [(track, setting) for track, setting in found if setting is not None]
        if not targets:
            raise ValueError(f"set_track_setting: no track in this set has {op.setting}")
    text = targets[0][1].encode(op.value)

    def run() -> OpResult:
        for _, setting in targets:
            setting.set(op.value)
        return SetTrackSettingResult(setting=op.setting, value=text, tracks=[track.name for track, _ in targets])

    return run


def _prepare_set_clip_setting(live_set: AbletonSet, op: SetClipSettingOp) -> Callable[[], OpResult]:
    member = _member("set_clip_setting", op.setting, Clip)
    track = _resolve_track(live_set, op.track)
    clip = _resolve_any_clip(live_set, track, op.clip)
    setting = clip.setting(member, **({} if op.index is None else {"index": op.index}))
    text = setting.encode(op.value)

    def run() -> OpResult:
        _require_in_set(live_set, clip)
        setting.set(op.value)
        return SetClipSettingResult(track=track.name, clip=clip.name, setting=op.setting, value=text)

    return run


def _prepare_set_transport_setting(live_set: AbletonSet, op: SetTransportSettingOp) -> Callable[[], OpResult]:
    setting = live_set.transport.setting(_member("set_transport_setting", op.setting, Transport))
    text = setting.encode(op.value)

    def run() -> OpResult:
        setting.set(op.value)
        return SetTransportSettingResult(setting=op.setting, value=text)

    return run


def _prepare(live_set: AbletonSet, op: AnyOp, donor_sets: dict[str, AbletonSet]) -> Callable[[], OpResult]:
    if isinstance(op, SetDeviceSettingOp):
        return _prepare_set_device_setting(live_set, op)
    if isinstance(op, SetTrackSettingOp):
        return _prepare_set_track_setting(live_set, op)
    if isinstance(op, SetClipSettingOp):
        return _prepare_set_clip_setting(live_set, op)
    if isinstance(op, SetTransportSettingOp):
        return _prepare_set_transport_setting(live_set, op)
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
    """Validate every op, then execute them in order. Raises with the tree unchanged.

    Validation resolves every selector, parses every donor set and prepares
    every write, running that writer's checks, before the first XML edit. A
    check only a run can make (a slot an earlier op filled) restores the tree
    before it raises. The caller saves; this only mutates the in-memory tree.
    Returns one result per op, in request order.
    """
    donor_sets: dict[str, AbletonSet] = {}
    prepared = [_prepare(live_set, op, donor_sets) for op in ops]
    # Each element keeps its identity on restore, so refs and cached tracks stay valid after a failed run.
    snapshot = [
        (element, dict(element.attrib), element.text, element.tail, list(element)) for element in live_set.root.iter()
    ]
    try:
        return [action() for action in prepared]
    except BaseException:
        for element, attrib, text, tail, children in snapshot:
            element.attrib = attrib
            element.text, element.tail = text, tail
            element[:] = children
        raise
