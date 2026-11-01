"""Read musical facts without discarding the source document needed for migration."""

from __future__ import annotations

import math
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from .inspect_document import walk
from .legacy_document import Node


def unwrap(node: Node) -> Node:
    while len(node.fields) == 1 and node.fields[0][0] == "Value":
        node = node.fields[0][1]
    return node


def number(node: Node) -> float:
    value = unwrap(node).atom
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError(f"Expected a finite number at 0x{node.start:x}")
    return float(value)


def integer(node: Node) -> int:
    value = unwrap(node).atom
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Expected an integer at 0x{node.start:x}")
    return value


def text(node: Node) -> str:
    value = unwrap(node).atom
    if not isinstance(value, str):
        raise ValueError(f"Expected text at 0x{node.start:x}")
    return value


def boolean(node: Node) -> bool:
    value = unwrap(node).atom
    if not isinstance(value, bool):
        raise ValueError(f"Expected boolean at 0x{node.start:x}")
    return value


@dataclass(frozen=True, slots=True)
class Note:
    pitch: int
    time: float
    duration: float
    velocity: float
    off_velocity: float
    enabled: bool
    source_offset: int


@dataclass(frozen=True, slots=True)
class Clip:
    path: str
    name: str
    location: Literal["session", "arrangement", "groove", "unclassified"]
    track_id: int | None
    slot_id: int | None
    groove_id: int
    notes: tuple[Note, ...]
    node: Node


def notes(clip: Node) -> tuple[Note, ...]:
    result: list[Note] = []
    for entry in clip.field("Notes").field("KeyTracks").entries:
        key = entry.node
        pitch = integer(key.field("MidiKey"))
        if not 0 <= pitch <= 127:
            raise ValueError(f"Invalid MIDI pitch at 0x{key.start:x}")
        for item in key.field("Notes").entries:
            note = item.node
            if note.class_name != "MidiNoteEvent":
                raise ValueError(f"Unknown note event {note.class_name}")
            result.append(
                Note(
                    pitch,
                    number(note.field("Time")),
                    number(note.field("Duration")),
                    number(note.field("Velocity")),
                    number(note.field("OffVelocity")),
                    boolean(note.field("IsEnabled")),
                    note.start,
                )
            )
    return tuple(result)


def midi_clips(root: Node) -> Iterator[Clip]:
    for path, node in walk(root):
        if node.class_name != "MidiClip":
            continue
        track = re.search(r"/Tracks/[^/]+\[(\d+)\]/", path)
        slot = re.search(r"/MainSequencer/ClipSlotList/ClipSlot\[(\d+)\]/", path)
        location: Literal["session", "arrangement", "groove", "unclassified"] = "unclassified"
        if path.startswith("LiveDocument/GroovePool/"):
            location = "groove"
        elif slot:
            location = "session"
        elif "/MainSequencer/ClipTimeable/" in path:
            location = "arrangement"
        yield Clip(
            path,
            text(node.field("Name")),
            location,
            int(track[1]) if track else None,
            int(slot[1]) if slot else None,
            integer(node.field("GrooveSettings").field("GrooveId")),
            notes(node),
            node,
        )
