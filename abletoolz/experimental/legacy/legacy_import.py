"""Experimental AB1E -> Live 8 XML -> newer schema, without changing AbletonSet.

The source is never saved back. The result is a candidate for native Live
testing, not a claim of equivalent playback. See SaveReport.warnings.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from abletoolz.formats import live_document
from abletoolz.live_set import AbletonSet
from abletoolz.live_set.persistence import save_in_place
from abletoolz.schema.live_schemas import live_schemas
from abletoolz.schema.upgrade import upgrade

from .clip_projection import project_clip
from .legacy_document import ClassType, LegacyDocument, Node
from .legacy_set import midi_clips, unwrap

# This is the XML dialect compared with the independent Live-written witnesses,
# not an inferred creator version for the binary source.
SOURCE_SCHEMA = "8.1_226"
TARGET_SCHEMA = "8.2_260"
_CLASS_NAMES = {"LiveDocument": "LiveSet", "SendTrack": "ReturnTrack", "TimeableEnvelopeParent": "AutomationLane"}
_FIELD_NAMES = {
    "AutomationEnvelopeParent": "AutomationLanes",
    "EnvelopeParents": "AutomationLanes",
    "ModulationChooerParent": "EnvelopeChooser",
    "AudioInputRoutable": "AudioInputRouting",
    "MidiInputRoutable": "MidiInputRouting",
    "AudioOutputRoutable": "AudioOutputRouting",
    "MidiOutputRoutable": "MidiOutputRouting",
    "TrackDevice": "Mixer",
    "MasterDevice": "Mixer",
    "ShowSomething": "IsExpanded",
    "CuePointManager": "Locators",
    "CuePoints": "Locators",
    "NextCueMapping": "NextLocatorMapping",
    "PreviousCueMapping": "PreviousLocatorMapping",
    "SetCueMapping": "SetLocatorMapping",
}
_ATTRIBUTES = {
    "FloatEvent": {"Time", "Value"},
    "BoolEvent": {"Time", "Value"},
    "EnumEvent": {"Time", "Value"},
    "MidiNoteEvent": {"Time", "Duration", "Velocity", "OffVelocity", "IsEnabled"},
    "RelativePathElement": {"Dir"},
    "RemoteablePoint": {"X", "Y"},
    "Scene": {"Value"},
    "TimeableModulationTarget": {"Id"},
    "UserFloatModulationTarget": {"Id"},
}
_INDEX = re.compile(r"\[(\d+)\]")


@dataclass(frozen=True, slots=True)
class SaveReport:
    source: Path
    source_sha256: str
    schema: str
    omitted: tuple[str, ...]
    unresolved: tuple[str, ...]
    upgrade_removed: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ImportedSet:
    live_set: AbletonSet
    report: SaveReport


def _scalar(node: Node) -> str:
    value = unwrap(node).atom
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"Non-finite value at 0x{node.start:x}")
    if isinstance(value, (float, int, str)):
        return str(value)
    raise ValueError(f"Unmapped scalar {node.kind} at 0x{node.start:x}")


class _Projection:
    def __init__(self, document: LegacyDocument) -> None:
        self.schemas = live_schemas()
        self.omitted: list[str] = []
        self.unresolved: list[str] = []
        self.clips = {clip.node.start: clip for clip in midi_clips(document.root)}

    def element(self, name: str, node: Node, path: str) -> ET.Element:
        if node.class_name == "MidiClip":
            projection = project_clip(self.clips[node.start])
            self.omitted.extend(projection.omitted_transient_fields)
            return projection.element
        element = ET.Element(name)
        value = unwrap(node)
        if isinstance(value.kind, int):
            if isinstance(value.atom, bytes):
                element.text = value.atom.hex().upper() or None
            elif isinstance(value.atom, ClassType):
                element.set("Name", _CLASS_NAMES.get(value.atom.name, value.atom.name))
            elif isinstance(value.atom, tuple):
                if len(value.atom) != 4:
                    raise ValueError(f"Unmapped rectangle at {path}")
                element.attrib.update(zip(("Top", "Left", "Bottom", "Right"), map(str, value.atom), strict=True))
            else:
                element.set("Value", _scalar(value))
            return element
        cls = _CLASS_NAMES.get(node.class_name, node.class_name)
        if node.fields and not self.schemas.declares_class(SOURCE_SCHEMA, cls):
            raise ValueError(f"Unmapped class {node.kind} at {path}")
        for field, child in node.fields:
            child_path = f"{path}/{field}"
            if child.class_name in {"RemoteableBang", "ArrangerLaneSelection"} or field in {
                "NextPointeeId",
                "AddTrackFlankBool",
                "AddSceneFlankBool",
            }:
                self.omitted.append(child_path)
                continue
            # The reference preset has different LockId/LockSeal values. Preserve
            # the source metadata explicitly rather than guessing a translation.
            if field in {"ProductId", "ProductIdSignature"}:
                self.unresolved.append(child_path)
                element.append(self.element(field, child, child_path))
                continue
            tag = _INDEX.sub(r".\1", _FIELD_NAMES.get(field, field))
            if not self.schemas.member_type(SOURCE_SCHEMA, cls, tag):
                raise ValueError(f"Unmapped field {cls}/{field} at {child_path}")
            if field in _ATTRIBUTES.get(cls, set()):
                element.set(tag, _scalar(child))
            else:
                element.append(self.element(tag, child, child_path))
        for entry in node.entries:
            cls = _CLASS_NAMES.get(entry.node.class_name, entry.node.class_name)
            item = self.element(cls, entry.node, f"{path}/{cls}[{entry.identifier}]")
            # Old XML lists are positional except objects referenced by persistent
            # track, device, branch and groove IDs. Keep those IDs, including gaps.
            if entry.identifier is not None and (
                name in {"Tracks", "Devices", "Branches", "ReturnBranches", "Grooves"}
            ):
                item.set("Id", str(entry.identifier))
            element.append(item)
        return element


def convert_file(source: Path, destination: Path | None = None) -> ImportedSet:
    """Convert an old binary set to an experimental 8.2-schema XML set and return it as a normal AbletonSet.

    Without ``destination`` the set is converted in place the way ``-s`` saves:
    the original goes to ``abletoolz_backup`` first and ``abletoolz undo`` can
    put it back. A ``destination`` is a new file; one already present is
    refused, including the source and symlinks to it. Uses the shared schema
    upgrader.
    """
    source = source.resolve()
    if destination is not None:
        destination = destination.absolute()
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(destination)
    document = LegacyDocument.read(source)
    projection = _Projection(document)
    root = ET.Element("Ableton", MajorVersion="4", MinorVersion=SOURCE_SCHEMA, Creator="Ableton Live 8.2")
    root.append(projection.element("LiveSet", document.root, "LiveSet"))
    # 8.2's XML omits EffectiveName. Preserve generated track/branch labels in UserName.
    for name in root.iter("Name"):
        user, effective = name.find("UserName"), name.find("EffectiveName")
        if user is not None and effective is not None and not user.get("Value"):
            user.set("Value", effective.get("Value", ""))
    before = {id(element): path for path, element in _xml_paths(root)}
    upgrade(root, TARGET_SCHEMA)
    retained = {id(element) for element in root.iter()}
    removed = tuple(path for identity, path in before.items() if identity not in retained)
    # The full graph keeps its source IDs; a clip must not point at a lost target.
    targets = {
        element.get("Id")
        for element in root.iter()
        if element.tag == "ModulationTarget" or element.tag.startswith("ControllerTargets.")
    }
    dangling = {element.get("Value") for element in root.iter("PointeeId")} - targets - {"-1"}
    if dangling:
        raise ValueError(f"Unmapped modulation references: {sorted(str(value) for value in dangling)}")
    warnings = (
        "Experimental XML projection; only some converted sets have been opened in Live.",
        "Device and mixer values are carried unchanged. Binary normalized values may need XML unit conversion.",
        "Relative file references are unchanged; moving this copy can require relinking.",
        "This 8.2 XML can be parsed/saved, but track APIs expecting EffectiveName "
        "or MasterTrack/DeviceChain need a later layout.",
        "ProductId/ProductIdSignature remain as legacy fields; their XML LockId/LockSeal mapping is unresolved.",
    )
    # shortcut: only the observed Coffee clip/record shapes are mapped; extend
    # with native witnesses before accepting additional legacy dialects.
    ET.indent(root, space="\t")
    encoded = live_document.encode(root)
    reread = live_document.parse(encoded)
    result = AbletonSet.from_root(reread, source if destination is None else destination)
    report = SaveReport(
        source,
        document.sha256,
        TARGET_SCHEMA,
        tuple(projection.omitted),
        tuple(projection.unresolved),
        removed,
        warnings,
    )
    for warning in warnings:
        logging.getLogger(__name__).warning(warning)
    if destination is None:
        save_in_place(source, reread)
    else:
        live_document.write_new(destination, encoded)
    return ImportedSet(result, report)


def _xml_paths(root: ET.Element) -> list[tuple[str, ET.Element]]:
    result: list[tuple[str, ET.Element]] = []
    pending = [(root.tag, root)]
    while pending:
        path, element = pending.pop()
        result.append((path, element))
        pending.extend((f"{path}/{child.tag}[{index}]", child) for index, child in enumerate(element))
    return result
