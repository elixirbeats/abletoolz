"""Write a set in an older Live release's schema.

Live opens sets from its own release and older ones and refuses newer ones, so
a set made in Live 12 cannot go to someone on Live 11 until the file says it is
an 11 set and reads like one. What each release writes was measured from real
sets (:mod:`abletoolz.schema.structure`); a save to an older release:

* writes the root attributes that release wrote;
* spells renamed tags and moved settings the way it did;
* fills the attributes its schema puts on every element of a tag where the
  set left them off (Live 12.2 drops a note's default probability; 11 writes it);
* removes everything newer than the release, and the automation that pointed
  into it. Live refuses a whole set over one attribute it does not know
  ("Unknown attribute 'SelectedToolPanel'", measured in Live 11.3.42);
* replaces each device newer than the release with an empty rack of the same
  kind, named after it, so the chain shows what is missing and where.

Nothing here knows any one feature: what is newer comes from the measured
grammar, so a new Live release needs a new census, not new code. Every removal
and replacement is in the report and logged as a warning.

Saving to a newer release is Live's job: it opens the set and saves it.
"""

from __future__ import annotations

import copy
import enum
import logging
import pathlib
import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

from abletoolz.formats import live_document
from abletoolz.live_set.persistence import unclaimed_path
from abletoolz.live_set.tracks import display_name
from abletoolz.schema import devices, objects, structure
from abletoolz.schema.settings import is_templated
from abletoolz.schema.structure import Schema
from abletoolz.schema.versioning import Version

if TYPE_CHECKING:
    from abletoolz.live_set.document import AbletonSet

__all__ = [
    "Added",
    "Moved",
    "NewerTarget",
    "Removed",
    "Replaced",
    "SaveAsReport",
    "SaveTarget",
    "UnknownTarget",
    "convert",
    "resolve_target",
    "write_for",
]

logger = logging.getLogger(__name__)

_TRACK_TAGS = frozenset({"AudioTrack", "MidiTrack", "GroupTrack", "ReturnTrack", "MainTrack", "MasterTrack"})
# What a chain can hold, by its owner; the rest (MIDI tracks, instrument and drum
# rack chains) hold MIDI effects, then the instrument, then audio effects.
_ONE_KIND_CHAINS = {
    "AudioTrack": "audio_effect",
    "ReturnTrack": "audio_effect",
    "GroupTrack": "audio_effect",
    "MainTrack": "audio_effect",
    "MasterTrack": "audio_effect",
    "AudioEffectBranch": "audio_effect",
    "MidiEffectBranch": "midi_effect",
}
_CHAIN_OWNERS = frozenset({*_ONE_KIND_CHAINS, "MidiTrack", "InstrumentBranch", "DrumBranch"})
_RACKS = {
    "audio_effect": ("AudioEffectGroupDevice", "Audio Effect Rack"),
    "instrument": ("InstrumentGroupDevice", "Instrument Rack"),
    "midi_effect": ("MidiEffectGroupDevice", "MIDI Effect Rack"),
}
_PRESET_FOLDER = re.compile(r"^Devices/(Audio Effects|Instruments|MIDI Effects)/", re.IGNORECASE)
_FOLDER_KINDS = {"audio effects": "audio_effect", "instruments": "instrument", "midi effects": "midi_effect"}
# Ids an envelope can point at: a parameter's automation or modulation target.
_TARGET_TAG = re.compile(r"(Automation|Modulation)Target$")


def _version_text(version: Version) -> str:
    return ".".join(str(part) for part in version)


def _folded(tag: str) -> str:
    """A tag as the census counts it: ``ControllerTargets.12`` is ``ControllerTargets.{n}``."""
    stem, dot, index = tag.rpartition(".")
    return f"{stem}.{{n}}" if dot and index.isdigit() else tag


class UnknownTarget(ValueError):
    """No set in the corpus was saved by the release asked for."""


class NewerTarget(ValueError):
    """The release asked for is newer than the set's own."""


@dataclass(frozen=True, slots=True)
class SaveTarget:
    """One Live release a set can be written for: the newest the corpus holds within what was asked."""

    release: Version
    header: tuple[tuple[str, str], ...]

    @property
    def creator(self) -> str:
        return dict(self.header)["Creator"]

    @property
    def schema(self) -> Schema:
        attributes = dict(self.header)
        count = attributes.get("SchemaChangeCount")
        return attributes["MinorVersion"], None if count is None else int(count)

    @property
    def sets(self) -> int:
        """How many sets of the corpus share this release's schema: the evidence behind every table."""
        return sum(
            structure.SETS[release]
            for release, header in structure.RELEASES.items()
            if SaveTarget(release, header).schema == self.schema
        )


def resolve_target(requested: str) -> SaveTarget:
    """The newest release the corpus holds that matches ``requested`` (``11``, ``11.3`` or ``11.3.42``)."""
    try:
        wanted = tuple(int(part) for part in requested.split("."))
    except ValueError:
        raise UnknownTarget(f"{requested!r} is not a Live version") from None
    if not 1 <= len(wanted) <= 3:
        raise UnknownTarget(f"{requested!r} is not a Live version")
    matching = [release for release in structure.RELEASES if release[: len(wanted)] == wanted]
    if not matching:
        known = sorted(structure.RELEASES)
        below = [release for release in known if release[: len(wanted)] < wanted]
        above = [release for release in known if release[: len(wanted)] > wanted]
        nearest = [_version_text(release) for release in (below[-1:] + above[:1])]
        raise UnknownTarget(
            f"no set from Live {requested} has been measured; the nearest that have are {' and '.join(nearest)}"
        )
    release = max(matching)
    return SaveTarget(release, structure.RELEASES[release])


@dataclass(frozen=True, slots=True)
class Moved:
    """A tag or setting the target spells differently, and how many were respelled."""

    newer: str
    older: str
    count: int


@dataclass(frozen=True, slots=True)
class Added:
    """An attribute the target's schema puts on every element of a tag, filled where it was missing."""

    tag: str
    attribute: str
    value: str
    count: int


@dataclass(frozen=True, slots=True)
class Removed:
    """Something the target release never wrote, taken out: ``Parent/Child`` or ``Tag@attribute``."""

    track: str | None
    what: str
    first_seen: Version
    count: int


@dataclass(frozen=True, slots=True)
class Replaced:
    """A device the target release does not have, now an empty rack named after it."""

    track: str | None
    device: str
    name: str
    rack: str
    first_seen: Version


@dataclass(frozen=True, slots=True)
class SaveAsReport:
    """What a save to an older release changed, and what that release will meet that it does not know."""

    source: Version
    target: SaveTarget
    replaced: tuple[Replaced, ...]
    removed: tuple[Removed, ...]
    orphaned_envelopes: int
    """Automation and clip envelopes dropped because what they pointed at was removed."""
    respelled: tuple[Moved, ...]
    filled: tuple[Added, ...]
    missing: tuple[tuple[str, str, int], ...]
    """``(parent, child, count)``: children the target writes under every such parent and the set lacks."""


def convert(root: ET.Element, source: Version, target: SaveTarget) -> tuple[ET.Element, SaveAsReport]:
    """A copy of ``root`` written for ``target``, and what changed on the way. ``root`` is not touched."""
    if target.release > source:
        raise NewerTarget(
            f"the set is from Live {_version_text(source)}; open it in Live {_version_text(target.release)} "
            "and save it there"
        )
    converted = copy.deepcopy(root)
    # The grammar knows each tag by the spelling of the release that wrote it, so
    # what is newer is found before anything is respelled, sparing what is about
    # to be respelled. A second pass takes what a move left behind (a clip's
    # follow action group, emptied of the settings Live 10 kept on the clip).
    replaced, removed, orphaned = _strip_newer(converted, source, target, _respelled_soon(source, target))
    converted.attrib.clear()
    converted.attrib.update(target.header)
    respelled = _rename_tags(converted, source, target) + _move_settings(converted, source, target)
    _, leftovers, orphaned_after = _strip_newer(converted, source, target, frozenset())
    removed += leftovers
    orphaned += orphaned_after
    filled = _fill_attributes(converted, target)
    report = SaveAsReport(
        source, target, replaced, removed, orphaned, tuple(respelled), filled, _missing_children(converted, target)
    )
    _warn(report)
    return converted, report


def write_for(
    live_set: AbletonSet, target: SaveTarget, output_dir: pathlib.Path | None = None
) -> tuple[pathlib.Path, SaveAsReport]:
    """Write a copy of ``live_set`` for ``target`` as ``name [Live X.Y.Z].als``, beside it or in ``output_dir``.

    The set in memory and the file it came from stay as they were.
    """
    converted, report = convert(live_set.root, live_set.version_tuple, target)
    document = live_document.encode(converted)
    folder = live_set.path.parent if output_dir is None else output_dir
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{live_set.path.stem} [Live {_version_text(target.release)}]{live_set.path.suffix}"
    if path.exists():
        path = unclaimed_path(path)
    live_document.write_new(path, document)
    return path, report


def _crosses(since: Version, source: Version, target: SaveTarget) -> bool:
    """Whether a change Live made in ``since`` lies between the target and the set."""
    return target.release < since <= source


def _rename_tags(root: ET.Element, source: Version, target: SaveTarget) -> list[Moved]:
    moved: list[Moved] = []
    for older, (newer, since) in structure.TAG_RENAMES.items():
        if not _crosses(since, source, target):
            continue
        found = list(root.iter(newer))
        for element in found:
            element.tag = older
        if found:
            moved.append(Moved(newer, older, len(found)))
    return moved


def _owner_paths(settings_class: type[enum.StrEnum]) -> list[tuple[str, str]]:
    """``(tag, prefix)``: the elements a settings class is relative to, and the path from them."""
    owners = [(tag.value, "") for tag, cls in devices.SETTINGS_OF.items() if cls is settings_class]
    if settings_class in objects.OWNERS:
        prefix = "DeviceChain/Mixer/" if settings_class is objects.Mixer else ""
        tags = set(objects.OWNERS[settings_class]) | {
            old for old, new in objects.OWNER_SPELLINGS.items() if new in objects.OWNERS[settings_class]
        }
        owners += [(tag, prefix) for tag in sorted(tags)]
    return owners


def _move_settings(root: ET.Element, source: Version, target: SaveTarget) -> list[Moved]:
    """Settings Live moved to another path (a clip's follow actions, Compressor's side-chain EQ), moved back."""
    moved: list[Moved] = []
    for module in (devices, objects):
        for settings_class, table in module.OLDER_PATHS.items():
            for setting, spellings in table.items():
                if is_templated(setting):
                    continue
                for (since, newer), (_, older) in zip(spellings, spellings[1:], strict=False):
                    if not _crosses(since, source, target) or newer == older:
                        continue
                    count = sum(
                        _move(owner, prefix + newer, prefix + older)
                        for tag, prefix in _owner_paths(settings_class)
                        for owner in root.iter(tag)
                    )
                    if count:
                        moved.append(Moved(newer, older, count))
    return moved


def _move(owner: ET.Element, newer: str, older: str) -> int:
    """Move the element at ``newer`` under ``owner`` to ``older``; 1 if one was there."""
    parent_path, _, _ = newer.rpartition("/")
    parent = owner.find(parent_path) if parent_path else owner
    element = owner.find(newer)
    if parent is None or element is None:
        return 0
    parent.remove(element)
    if parent is not owner and len(parent) == 0:
        holder = owner.find(parent_path.rpartition("/")[0]) if "/" in parent_path else owner
        if holder is not None:
            holder.remove(parent)
    destination = owner
    *steps, leaf = older.split("/")
    for step in steps:
        found = destination.find(step)
        destination = found if found is not None else ET.SubElement(destination, step)
    element.tag = leaf
    destination.append(element)
    return 1


def _fill_attributes(root: ET.Element, target: SaveTarget) -> tuple[Added, ...]:
    required = structure.REQUIRED_ATTRIBUTES.get(target.schema, {})
    orders = structure.ATTRIBUTE_ORDER.get(target.schema, {})
    filled: Counter[tuple[str, str]] = Counter()
    for element in root.iter():
        tag = _folded(element.tag)
        names = required.get(tag)
        if names is None:
            continue
        absent = [name for name in names if name not in element.attrib]
        if not absent:
            continue
        values = dict(element.attrib)
        for name in absent:
            values[name] = structure.FILL_VALUES[tag][name]
            filled[tag, name] += 1
        order = orders.get(tag, ())
        element.attrib.clear()
        element.attrib.update({name: values.pop(name) for name in order if name in values} | values)
    return tuple(
        Added(tag, name, structure.FILL_VALUES[tag][name], count) for (tag, name), count in sorted(filled.items())
    )


def _respelled_soon(source: Version, target: SaveTarget) -> frozenset[tuple[str | None, str]]:
    """``(parent, child)`` pairs the renames and moves will respell; ``None`` for a tag renamed anywhere."""
    spared: set[tuple[str | None, str]] = {
        (None, newer) for _, (newer, since) in structure.TAG_RENAMES.items() if _crosses(since, source, target)
    }
    for module in (devices, objects):
        for settings_class, table in module.OLDER_PATHS.items():
            for spellings in table.values():
                for (since, newer), (_, older) in zip(spellings, spellings[1:], strict=False):
                    if newer == older or not _crosses(since, source, target):
                        continue
                    for tag, prefix in _owner_paths(settings_class):
                        *steps, _ = (prefix + newer).split("/")
                        parent = prefix.rstrip("/").rpartition("/")[2] or tag
                        spared.add(
                            (parent, (prefix + newer).split("/")[len(prefix.rstrip("/").split("/")) if prefix else 0])
                        )
    return frozenset(spared)


def _strip_newer(
    root: ET.Element, source: Version, target: SaveTarget, spared: frozenset[tuple[str | None, str]]
) -> tuple[tuple[Replaced, ...], tuple[Removed, ...], int]:
    """Remove what ``target`` never wrote and put a named rack where each newer device was."""
    replaced: list[Replaced] = []
    removed: Counter[tuple[str | None, str, Version]] = Counter()
    gone_ids: set[str] = set()
    # A renamed element arrived with everything in it, so the grammar sees nothing in it
    # as newer; what its old spelling held is what the target knows.
    renamed = {
        newer: (since, *structure.RENAMED_HELD.get(older, ({}, {})))
        for older, (newer, since) in structure.TAG_RENAMES.items()
        if _crosses(since, source, target)
    }

    def newer_than_target(tag: str, name: str, attribute: bool) -> Version | None:
        if tag in renamed:
            since, children, attributes = renamed[tag]
            first = (attributes if attribute else children).get(name)
            return since if first is None else first if first > target.release else None
        first = (structure.NEWER_ATTRIBUTES if attribute else structure.NEWER_CHILDREN).get(tag, {}).get(name)
        return first if first is not None and first > target.release else None

    def walk(element: ET.Element, track: str | None, chain: str) -> None:
        tag = _folded(element.tag)
        if element.tag in _TRACK_TAGS:
            track = display_name(element, source) or track
        if element.tag in _CHAIN_OWNERS:
            chain = element.tag
        for name in list(element.attrib):
            first = newer_than_target(tag, name, attribute=True)
            if first is not None:
                del element.attrib[name]
                removed[track, f"{tag}@{name}", first] += 1
        children = list(element)
        for index, child in enumerate(children):
            child_tag = _folded(child.tag)
            first = newer_than_target(tag, child_tag, attribute=False)
            if first is None or (None, child.tag) in spared or (tag, child.tag) in spared:
                walk(child, track, chain)
                continue
            gone_ids.update(_target_ids(child))
            if element.tag == "Devices":
                rack = _placeholder(child, _kind(child, children, index, chain), first, source, target)
                element[index] = rack
                replaced.append(Replaced(track, child.tag, _device_name(child), rack.tag, first))
            else:
                element.remove(child)
                removed[track, f"{tag}/{child_tag}", first] += 1

    walk(root, None, "")
    return (
        tuple(replaced),
        tuple(Removed(track, what, first, count) for (track, what, first), count in removed.items()),
        _drop_orphaned_envelopes(root, gone_ids),
    )


def _target_ids(element: ET.Element) -> set[str]:
    return {
        found.get("Id", "")
        for found in element.iter()
        if found.get("Id") is not None and (_TARGET_TAG.search(found.tag) or found.tag == "Pointee")
    }


def _drop_orphaned_envelopes(root: ET.Element, gone_ids: set[str]) -> int:
    """Remove every envelope whose target was removed with a newer element; Live would find it pointing at nothing."""
    if not gone_ids:
        return 0
    dropped = 0
    for holder in root.iter():
        for envelope in list(holder):
            pointee = envelope.find("EnvelopeTarget/PointeeId")
            if pointee is not None and pointee.get("Value") in gone_ids:
                holder.remove(envelope)
                dropped += 1
    return dropped


def _preset_kind(device: ET.Element) -> str | None:
    """The browser folder of the preset the device was last loaded from, when Live recorded one."""
    path = device.find("LastPresetRef//RelativePath")
    match = None if path is None else _PRESET_FOLDER.match(path.get("Value", ""))
    return None if match is None else _FOLDER_KINDS[match[1].lower()]


def _known_kind(device: ET.Element) -> str | None:
    return _preset_kind(device) or structure.DEVICE_KINDS.get(device.tag)


def _kind(device: ET.Element, chain: list[ET.Element], index: int, owner: str) -> str:
    """Audio effect, MIDI effect or instrument: what the device says, what the census measured, or where it sits."""
    known = _known_kind(device) or _ONE_KIND_CHAINS.get(owner)
    if known is not None:
        return known
    kinds = [_known_kind(other) for other in chain]
    if "instrument" in kinds:
        return "midi_effect" if index < kinds.index("instrument") else "audio_effect"
    return "audio_effect"


def _device_name(device: ET.Element) -> str:
    """The name the device shows: the user's, else Live's own for the device type."""
    user = device.find("UserName")
    if user is not None and user.get("Value"):
        return user.get("Value", "")
    try:
        member = devices.Device(device.tag).name
    except ValueError:
        return device.tag
    return member.replace("_", " ").title()


def _placeholder(device: ET.Element, kind: str, first: Version, source: Version, target: SaveTarget) -> ET.Element:
    """An empty rack of the device's kind in its place, its name and info text saying what was there."""
    tag, _ = _RACKS[kind]
    name = _device_name(device)
    rack = ET.Element(tag, {"Id": device.get("Id", "0")})
    ET.SubElement(rack, "UserName", {"Value": f"Missing: {name}"})
    ET.SubElement(
        rack,
        "Annotation",
        {
            "Value": f"{name} ({device.tag}) was here. It needs Live {_version_text(first)}; this copy of a "
            f"Live {_version_text(source)} set was written for Live {_version_text(target.release)}."
        },
    )
    return rack


def _missing_children(root: ET.Element, target: SaveTarget) -> tuple[tuple[str, str, int], ...]:
    """Children the target's schema writes under every element of a tag that are absent here."""
    required = structure.REQUIRED_CHILDREN.get(target.schema, {})
    missing: Counter[tuple[str, str]] = Counter()
    for element in root.iter():
        tag = _folded(element.tag)
        wanted = required.get(tag)
        if wanted:
            present = {_folded(child.tag) for child in element}
            for child_tag in wanted:
                if child_tag not in present:
                    missing[tag, child_tag] += 1
    return tuple((parent, child, count) for (parent, child), count in sorted(missing.items()))


def _warn(report: SaveAsReport) -> None:
    release = report.target.creator
    for device in report.replaced:
        _, label = next(rack for rack in _RACKS.values() if rack[0] == device.rack)
        logger.warning(
            "%s has no %s: '%s' on %s is now an empty %s named 'Missing: %s'",
            release,
            device.device,
            device.name,
            device.track or "the set",
            label,
            device.name,
        )
    # Which track each one was on is in the report; once per kind is enough to read.
    kinds = sorted({item.what for item in report.removed})
    if kinds:
        logger.warning("%s never wrote these, so they were removed: %s", release, ", ".join(kinds))
    if report.orphaned_envelopes:
        logger.warning("removed %d envelopes that pointed at removed parameters", report.orphaned_envelopes)
