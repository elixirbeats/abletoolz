"""Arrangement-section mechanics: where clips live on a timeline and how to move them.

A track's arrangement clips are direct children of one ``Events`` element --
``DeviceChain/MainSequencer/ClipTimeable/ArrangerAutomation/Events`` on a
``MidiTrack``, ``DeviceChain/MainSequencer/Sample/ArrangerAutomation/Events``
on an ``AudioTrack``. Measured across the whole fixture corpus plus a real
12.4.5b9 set (404 arrangement events in total):

* ``Time`` and ``CurrentStart`` always agree, to the digit, on every event.
  ``CurrentEnd`` closes the span, so an event occupies ``[Time, CurrentEnd)``
  in arrangement beats.
* ``Events`` children are always in non-decreasing ``Time`` order, in every
  version, even where the ``Id`` attributes are not (11.0.12's MIDI track
  reads ids 0, 9, 5, 10, 11, 13 in time order; the real 12.4.5b9 set counts
  down 77, 76, 75 as time goes up).
* ``Id`` is list-local and sparse -- unique within one track's ``Events``,
  starting anywhere (0 in 10.0.x/11.x, 1 in 10.1.3, 2 in the real set), and
  absent entirely before Live 10. Handing out ``max + 1`` is the only rule
  the corpus supports.
* Loop markers are clip-internal time, independent of where the event sits:
  11.0.0 carries two slices of one recording whose ``LoopStart`` values
  (0.154 and 784.154) bear no fixed relation to their ``Time`` (96 and 784).
  Moving an event therefore leaves them alone.
* Every ``LoopOn="false"`` event in the corpus satisfies
  ``LoopEnd - LoopStart == CurrentEnd - CurrentStart`` exactly, with zero
  exceptions, so resizing a non-looping event has to carry ``LoopEnd`` with
  it or the invariant breaks.

Track automation lives beside the clips, at
``<Track>/AutomationEnvelopes/Envelopes/AutomationEnvelope``: an
``EnvelopeTarget/PointeeId`` naming the automated parameter, and an
``Automation/Events`` list of ``FloatEvent``/``BoolEvent``/``EnumEvent``.
Every one of the 4400+ automation events in the corpus is a leaf element
carrying nothing but ``Id``, ``Time`` and ``Value`` (``Time`` and ``Value``
only, before Live 10) -- no curve, bezier or shape attributes exist to
preserve. The first event of every envelope sits at ``Time="-63072000"``,
Live's "value before the timeline starts" sentinel, which no real section
range can reach.

Two automation containers are deliberately out of scope, both flagged rather
than guessed at. Live 9 has no ``AutomationEnvelopes`` element at all and
keeps automation inline under each parameter's own ``ArrangerAutomation``;
10.1.3 still keeps mixer-send automation there while everything else has
moved. Modern sets (11+, and the real 12.4.5b9 set measured here) put all of
it in ``AutomationEnvelopes``, which is what this module reads.

Indentation is handled a seam at a time rather than by re-tailing a whole
list: 10.1.3 writes 57 automation events on a single line, and rewriting
every sibling's tail would reformat a region nothing asked to touch.
"""

from __future__ import annotations

import copy
import dataclasses
import enum
from collections.abc import Sequence
from xml.etree import ElementTree as ET

from abletoolz.live_set.xml_edit import number_value

# Where each track type keeps its arrangement clips, and what the clips are called.
ARRANGEMENT_EVENTS_PATH = {
    "MidiTrack": ("DeviceChain/MainSequencer/ClipTimeable/ArrangerAutomation", "MidiClip"),
    "AudioTrack": ("DeviceChain/MainSequencer/Sample/ArrangerAutomation", "AudioClip"),
}

# The only automation event tags observed in any Automation/Events list.
AUTOMATION_EVENT_TAGS = frozenset({"FloatEvent", "BoolEvent", "EnumEvent"})


class SectionMode(enum.StrEnum):
    """What a section operation does when the destination is already occupied."""

    REFUSE = "refuse"
    REPLACE = "replace"


class SectionBoundary(enum.StrEnum):
    """Which edge of a section a straddling clip crosses."""

    START = "start"
    END = "end"
    BOTH = "both"


@dataclasses.dataclass(frozen=True, slots=True)
class CopiedPlacement:
    """One clip that made it into the destination section."""

    track_name: str
    clip_name: str
    start: float


@dataclasses.dataclass(frozen=True, slots=True)
class SkippedStraddler:
    """A clip that crosses a section edge, so it was neither copied nor cut."""

    track_name: str
    clip_name: str
    start: float
    end: float
    boundary: SectionBoundary


@dataclasses.dataclass(frozen=True, slots=True)
class SectionCopyReport:
    """What ``Clips.copy_section`` did, and what it refused to guess at."""

    dest_start: float
    copied: tuple[CopiedPlacement, ...]
    skipped: tuple[SkippedStraddler, ...]
    replaced: int
    envelope_events: int

    def lines(self) -> list[str]:
        """Human summary, one line per fact worth reading."""
        report = [f"copied {len(self.copied)} clip(s) to beat {number_value(self.dest_start)}"]
        if self.replaced:
            report.append(f"deleted {self.replaced} clip(s) at the destination")
        if self.envelope_events:
            report.append(f"copied {self.envelope_events} automation event(s)")
        report.extend(
            f"skipped {straddler.clip_name!r} on {straddler.track_name!r} "
            f"({number_value(straddler.start)}-{number_value(straddler.end)}): "
            f"crosses the section {straddler.boundary}"
            for straddler in self.skipped
        )
        return report


@dataclasses.dataclass(frozen=True, slots=True)
class ArrangementEvents:
    """One track's arrangement clip list, with the indentation its own tag sits on."""

    element: ET.Element
    indent: str
    clip_tag: str

    def clips(self) -> list[ET.Element]:
        return [child for child in self.element if child.tag == self.clip_tag]


def arrangement_events(track_root: ET.Element, track_type: str) -> ArrangementEvents | None:
    """A track's arrangement clip list, or ``None`` for a track type that has none.

    Group, return and main tracks hold no clips at all; the path is looked up
    by track tag rather than searched for, so a device's own ``Sample`` or a
    take lane's ``ClipAutomation`` can never be mistaken for the arrangement.
    """
    located = ARRANGEMENT_EVENTS_PATH.get(track_type)
    if located is None:
        return None
    path, clip_tag = located
    arranger = track_root.find(path)
    if arranger is None:
        return None
    events = arranger.find("Events")
    if events is None:
        return None
    # ``arranger.text`` is the whitespace before its first child, which is the
    # indentation ``Events`` itself sits on -- the anchor every insert needs.
    return ArrangementEvents(element=events, indent=arranger.text or "", clip_tag=clip_tag)


def event_time(element: ET.Element) -> float:
    """The ``Time`` attribute of a clip or automation event, in beats."""
    return float(element.get("Time", "0"))


def event_span(clip: ET.Element) -> tuple[float, float]:
    """A clip's arrangement span ``[Time, CurrentEnd)`` in beats."""
    current_end = clip.find("CurrentEnd")
    if current_end is None:
        raise ValueError(f"Arrangement {clip.tag} carries no CurrentEnd; it has no span")
    return event_time(clip), float(current_end.get("Value", "0"))


def clip_name(clip: ET.Element) -> str:
    name = clip.find("Name")
    return "" if name is None else name.get("Value", "")


def straddled_boundary(span: tuple[float, float], start: float, end: float) -> SectionBoundary | None:
    """Which section edge a span crosses, or ``None`` when it is inside or outside.

    A clip that merely touches an edge -- ending exactly at ``start`` or
    starting exactly at ``end`` -- is outside, matching the half-open
    ``[start, end)`` the rest of this module uses.
    """
    clip_start, clip_end = span
    if clip_end <= start or clip_start >= end:
        return None
    crosses_start = clip_start < start
    crosses_end = clip_end > end
    if crosses_start and crosses_end:
        return SectionBoundary.BOTH
    if crosses_start:
        return SectionBoundary.START
    if crosses_end:
        return SectionBoundary.END
    return None


def move_clip(clip: ET.Element, start: float, length: float | None) -> None:
    """Put a clip element at ``start``, optionally resizing its arrangement span.

    ``Time``, ``CurrentStart`` and ``CurrentEnd`` are the whole placement.
    A resize also carries ``LoopEnd`` on a non-looping clip, which is the one
    marker the corpus ties to the arrangement span.
    """
    current_start = _child(clip, "CurrentStart")
    current_end = _child(clip, "CurrentEnd")
    span = float(current_end.get("Value", "0")) - float(current_start.get("Value", "0"))
    if length is None:
        length = span
    if length <= 0:
        raise ValueError(f"Clip length must be positive, got {length}")

    clip.set("Time", number_value(start))
    current_start.set("Value", number_value(start))
    current_end.set("Value", number_value(start + length))

    loop_on = clip.find("Loop/LoopOn")
    if length != span and loop_on is not None and loop_on.get("Value") == "false":
        loop_start = _child(clip, "Loop/LoopStart")
        _child(clip, "Loop/LoopEnd").set("Value", number_value(float(loop_start.get("Value", "0")) + length))


def next_event_id(events: ET.Element) -> str | None:
    """The id a new sibling should take, or ``None`` where siblings carry none.

    Ids are list-local and sparse, so ``max + 1`` is the only claim the
    corpus supports; pre-10 clips have no ``Id`` attribute at all.
    """
    ids = [int(child.attrib["Id"]) for child in events if child.attrib.get("Id", "").lstrip("-").isdigit()]
    if not ids:
        return None
    return str(max(ids) + 1)


def assign_event_id(events: ET.Element, clip: ET.Element) -> None:
    """Give a clip about to join ``events`` a fresh id, where its siblings have one.

    A clip written before Live 10 carries no ``Id`` at all and keeps it that
    way. An empty list gives nothing to count from, so the first clip back in
    takes 0 -- where 10.0.6 and 11.0.12 both start.
    """
    fresh = next_event_id(events)
    if fresh is not None:
        clip.set("Id", fresh)
    elif "Id" in clip.attrib:
        clip.set("Id", "0")


def insert_in_time_order(container: ET.Element, element: ET.Element, *, indent: str) -> None:
    """Insert ``element`` keeping the container's children non-decreasing in ``Time``.

    Only the seam around the insertion point is re-indented: every other
    sibling keeps the tail Live gave it, so a list Live wrote on one line
    stays on one line.
    """
    time = event_time(element)
    children = list(container)
    position = next((index for index, child in enumerate(children) if event_time(child) > time), len(children))

    if not children:
        container.text = indent + "\t"
        element.tail = indent
        container.append(element)
        return
    if position == 0:
        element.tail = container.text
        container.insert(0, element)
        return

    previous = children[position - 1]
    element.tail = previous.tail
    if position == len(children):
        # The last child's tail closes the list; the new last child inherits it.
        previous.tail = children[position - 2].tail if position >= 2 else container.text
    container.insert(position, element)


def remove_children(container: ET.Element, doomed: Sequence[ET.Element]) -> None:
    """Drop elements from a list, keeping the list's own closing indentation."""
    for element in doomed:
        children = list(container)
        if children[-1] is element and len(children) > 1:
            children[-2].tail = element.tail
        container.remove(element)
    if not len(container):
        container.text = None


def track_envelopes(track_root: ET.Element) -> list[ET.Element]:
    """A track's automation envelopes, empty for Live 9 sets which have no container."""
    envelopes = track_root.find("AutomationEnvelopes/Envelopes")
    return [] if envelopes is None else [child for child in envelopes if child.tag == "AutomationEnvelope"]


def copy_envelope_segment(envelope: ET.Element, start: float, end: float, delta: float) -> int:
    """Copy one envelope's events in ``[start, end)`` to ``+delta``, in time order.

    ``EnvelopeTarget/PointeeId`` is never touched: it names a parameter that
    exists once, and the copied events belong to the same parameter. Copied
    events land in the envelope's own ``Events`` list with fresh list-local
    ids, so nothing outside the envelope changes.

    The copy is a segment, not a splice: no edge value is synthesized at
    ``start + delta``, so the copied stretch inherits whatever value the
    envelope already held just before it.
    """
    automation = envelope.find("Automation")
    if automation is None:
        return 0
    events = automation.find("Events")
    if events is None:
        return 0
    indent = automation.text or ""

    sources = [child for child in events if child.tag in AUTOMATION_EVENT_TAGS and start <= event_time(child) < end]
    next_id = next_event_id(events)
    counter = 0 if next_id is None else int(next_id)
    for source in sources:
        copied = copy.deepcopy(source)
        copied.set("Time", number_value(event_time(source) + delta))
        if "Id" in copied.attrib:
            copied.set("Id", str(counter))
            counter += 1
        insert_in_time_order(events, copied, indent=indent)
    return len(sources)


def _child(element: ET.Element, path: str) -> ET.Element:
    found = element.find(path)
    if found is None:
        raise ValueError(f"Clip element {element.tag} has no {path}")
    return found
