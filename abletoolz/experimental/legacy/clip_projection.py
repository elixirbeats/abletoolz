"""Experimental clip fragments in the observed Live 8.2.1 XML shape.

These are migration intermediates, not standalone .als/.alc files. Groove and
modulation IDs still belong to the source document. No set writer is exposed.
"""

from __future__ import annotations

from dataclasses import dataclass
from xml.etree import ElementTree as ET

from .legacy_document import ClassType, Node
from .legacy_set import Clip, number, unwrap


@dataclass(frozen=True, slots=True)
class ClipProjection:
    element: ET.Element
    omitted_transient_fields: tuple[str, ...]


# Only this clip revision and these fields have been compared with the XML witness.
_CLIP_FIELDS = frozenset(
    """Time CurrentStart CurrentEnd Loop Name Annotation ColorIndex LaunchMode
LaunchQuantisation TimeSignature ModulationList ScrollerTimePreserver TimeSelection Legato Ram GrooveSettings
Disabled VelocityAmount FollowTime FollowActionA FollowActionB FollowChanceA FollowChanceB Grid FreezeStart FreezeEnd
StepForwardBang StepBackwardBang Notes BankSelectCoarse BankSelectFine ProgramChange NoteEditorFoldInZoom
NoteEditorFoldInScroll NoteEditorFoldOutZoom NoteEditorFoldOutScroll""".split()
)
_TRANSIENT = frozenset(("StepForwardBang", "StepBackwardBang", "UndoWatch"))


def value_string(node: Node) -> str:
    value = unwrap(node).atom
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (float, int, str)):
        return str(value)
    raise ValueError(f"Not an XML scalar: {node.class_name} at 0x{node.start:x}")


def _element(name: str, node: Node, omitted: list[str], path: str) -> ET.Element:
    element = ET.Element(name)
    if isinstance(unwrap(node).kind, int):
        element.set("Value", value_string(node))
        return element
    if node.class_name == "RemoteableTimeSignature":
        numerator = number(node.field("Numerator"))
        denominator = number(node.field("Denominator"))
        if (numerator, denominator) != (4.0, 2.0):
            raise ValueError("Only the witnessed legacy 4/4 signature has a conversion rule")
        ET.SubElement(element, "Numerator", Value="4")
        ET.SubElement(element, "Denominator", Value="4")
        ET.SubElement(element, "Time", Value=value_string(node.field("Time")))
        return element
    if node.class_name in ("MidiNoteEvent", "FloatEvent", "BoolEvent", "EnumEvent"):
        for field, child in node.fields:
            element.set(field, value_string(child))
        return element
    for field, child in node.fields:
        if field in _TRANSIENT:
            omitted.append(f"{path}/{field}")
            continue
        element.append(_element(field, child, omitted, f"{path}/{field}"))
    for entry in node.entries:
        element.append(
            _element(entry.node.class_name, entry.node, omitted, f"{path}/{entry.node.class_name}[{entry.identifier}]")
        )
    return element


def project_clip(clip: Clip) -> ClipProjection:
    if clip.node.kind != ClassType(9, "MidiClip"):
        raise ValueError(f"Unmapped clip revision {clip.node.kind}")
    fields = {name for name, _ in clip.node.fields}
    if fields != _CLIP_FIELDS:
        raise ValueError(f"Unmapped clip fields: {fields ^ _CLIP_FIELDS}")
    omitted: list[str] = []
    root = ET.Element("MidiClip", Time=value_string(clip.node.field("Time")))
    for name, node in clip.node.fields:
        if name == "Time":
            continue
        if name in _TRANSIENT:
            omitted.append(f"{clip.path}/{name}")
            continue
        root.append(_element(name, node, omitted, f"{clip.path}/{name}"))
    return ClipProjection(root, tuple(omitted))
