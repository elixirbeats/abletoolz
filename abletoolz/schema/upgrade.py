"""A set's document moved to a newer Live schema, so it can hold what that schema adds.

Live reads a set through the schema its root ``MinorVersion`` names, and Live 12
still ships every one of them, so a document only has to be a valid example of
some schema to open; Live upgrades it the rest of the way itself. Moving a
document up one schema is the difference between the two, read off Live's own
schemas (:mod:`abletoolz.schema.live_schemas`): a member the newer schema no
longer declares is removed, a member it adds is left out for Live to fill with
its default, and a class it removes refuses the move when the set uses it,
since Live converted those itself on reading and nothing here knows how.

``10.0_374`` is the first schema whose ``Vst3PluginInfo`` has the members a
written VST3 device holds, and the last before Live 10.1 replaced CrossDelay and
PingPongDelay with Delay; a Live 10.0 set (``10.0_370``) moves to it by losing
the warp markers its MIDI clips carried.

A Live 9 document (``9.0_274`` to ``9.5_327``) moves to it through what Live 10
changed on the way, each step read off the two schemas and Live 10's own sets:

* Arrangement automation leaves each parameter's own ``ArrangerAutomation`` for
  an ``AutomationEnvelope`` on the parameter's track, pointing at its
  ``AutomationTarget``, its events unchanged. Live 9 writes one event, before
  the timeline starts, into every parameter; a parameter with nothing else is
  not automated (``Manual`` holds its value) and gets no envelope, as Live 10
  writes none for it.
* A track's ``AutomationLanes`` becomes Live 10's class of that name, keeping
  its lanes; a group device's renamed zone-view setting keeps its value.
* Document format 5 (MajorVersion 5, from ``10.0_350``) numbers the entries of
  every list, which Live 9 left unnumbered; they take their positions, the
  meaning Live 9 gave them (a send is its return track's, a clip slot its
  scene's). ``LiveSet/NextPointeeId`` starts past every id the set owns.
* Auto Filter and Simpler's filter, rebuilt in Live 9.5, move as Live moves
  them: their old type and resonance keep their values and automation under
  the Legacy names, and legacy mode turns on, so they play as before. Read off
  sets Live saved after converting older ones. Operator's filter (Live 9.5) and
  Utility (Live 10), rebuilt the same way, still refuse the move: no set here
  shows what Live writes for them.
* What Live 10 no longer stores -- view settings such as a track's MIDI fold,
  settings of removed features -- is removed and named in :attr:`Move.removed`.

Measured on 19 copies of Live 9 sets (2026-11-01); Live has not opened a
document moved here, so the list ids and the moved envelopes follow what Live
10 writes rather than a set Live converted.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from abletoolz.schema import Attr, Tag
from abletoolz.schema.live_schemas import live_schemas

VST3_SCHEMA = "10.0_374"
"""The oldest schema a set can be moved to that holds a VST3 device as abletoolz writes one."""

# The documents moved to VST3_SCHEMA. A Live 9 document (any 9.x schema Live ships) is one too.
_LIVE_10_0 = "10.0_370"

# The first schema of document format 5, which Live 10 writes: MajorVersion 5, every list entry numbered.
_FORMAT_5 = "10.0_350"

# Live's time for the value before the timeline starts, the first event of every envelope.
_BEFORE_TIMELINE = "-63072000"

# Members Live renamed, keeping their value.
_RENAMED = {"MidiTargetShowsZonesInsteadOfNoteNames": "ShowsZonesInsteadOfNoteNames"}

# What Live does to a filter it rebuilt with a legacy mode when it converts an older set, read off sets it saved
# after converting them: the old members keep their values and automation targets under Legacy names, LegacyMode
# turns on, and the new members are left to Live's defaults, which legacy mode does not play.
_LEGACY_RENAMES = {
    "SimplerFilter": {"Type": "LegacyType", "Q": "LegacyQ"},
    "AutoFilter": {"FilterType": "LegacyFilterType", "Resonance": "LegacyQ"},
    "SimplerGlobals": {},
}
_LEGACY_MODE = "LegacyMode"

_MODULATION_AUTOMATION = "ModulationAutomation"

_GENERIC = re.compile(r"^\w+<(\w+)>$")


def vst3_upgrade(minor_version: str) -> str | None:
    """The schema a document of ``minor_version`` moves to before it can hold a VST3 device, or None."""
    live_9 = minor_version.startswith("9.") and minor_version in live_schemas().minor_versions
    return VST3_SCHEMA if minor_version == _LIVE_10_0 or live_9 else None


def can_upgrade(minor_version: str, to: str) -> bool:
    """Whether a document of ``minor_version`` is older than ``to`` and both are schemas Live ships."""
    schemas = live_schemas()
    found = schemas.minor_versions
    return minor_version in found and to in found and found.index(minor_version) < found.index(to)


def is_automated(events: ET.Element) -> bool:
    """Whether a Live 9 parameter's ``ArrangerAutomation/Events`` holds more than the value before the timeline."""
    return len(events) > 1 or any(event.get(Attr.TIME) != _BEFORE_TIMELINE for event in events)


@dataclass(frozen=True, slots=True)
class Move:
    """What moving a document to a newer schema does, worked out before anything changes."""

    to: str
    refusal: str | None
    """Why the document cannot move, or None when it can."""
    envelopes: int
    """Arrangement envelopes moved from their parameters onto their tracks."""
    removed: tuple[str, ...]
    """``Class.Member`` of everything the newer schema no longer stores, sorted."""


def preview(root: ET.Element, to: str) -> Move:
    """What moving the document ``root`` holds to the schema ``to`` would do; nothing is changed.

    Raises ValueError when the document is not older than ``to``.
    """
    return _Work(root, to).move


def upgrade(root: ET.Element, to: str) -> Move:
    """Move the document ``root`` holds to the schema ``to``, in place, and say what moving it did.

    Raises ValueError when the document is not older than ``to``, or holds
    something the move cannot carry (:attr:`Move.refusal`); nothing is changed then.
    """
    work = _Work(root, to)
    if work.move.refusal is not None:
        raise ValueError(work.move.refusal)
    work.apply()
    return work.move


class _Work:
    """Everything one move does, collected in a single walk before any of it is done."""

    def __init__(self, root: ET.Element, to: str) -> None:
        schemas = live_schemas()
        source = root.get(Attr.MINOR_VERSION, "")
        if not can_upgrade(source, to):
            raise ValueError(f"a {source or 'unversioned'} document cannot be moved to Live's {to} schema")
        self.schemas, self.source, self.to, self.root = schemas, source, to, root
        index = schemas.minor_versions.index
        self.reformat = index(source) < index(_FORMAT_5) <= index(to)
        self.drops: list[tuple[ET.Element, ET.Element]] = []
        self.removed: set[str] = set()
        self.renames: list[tuple[ET.Element, str]] = []
        self.unnumbered: list[ET.Element] = []
        self.tracks: list[ET.Element] = []
        self.envelopes: dict[ET.Element, list[tuple[str, ET.Element]]] = {}
        self.gone: set[str] = set()
        self.legacy: set[str] = set()
        self.names: dict[ET.Element, str] = {}  # members renamed into their legacy names, looked up under those
        self.legacy_modes: list[tuple[ET.Element, ET.Element | None]] = []  # (device, member LegacyMode goes before)
        self.problems: set[str] = set()
        self._walk(root, None, None, None)

    @functools.cached_property
    def move(self) -> Move:
        problems = sorted(self.problems)
        if self.gone:
            problems.append(f"the set uses {', '.join(sorted(self.gone))}, which Live's {self.to} schema replaced")
        if self.legacy:
            problems.append(
                f"the set uses {', '.join(sorted(self.legacy))}, which Live's {self.to} schema rebuilt "
                "with a legacy mode nothing here sets"
            )
        return Move(
            to=self.to,
            refusal="; ".join(problems) or None,
            envelopes=sum(len(moved) for moved in self.envelopes.values()),
            removed=tuple(sorted(self.removed)),
        )

    def _walk(self, element: ET.Element, source: str | None, target: str | None, track: ET.Element | None) -> None:
        """Collect what moving ``element``'s children takes; ``source`` and ``target`` are its class in each schema."""
        schemas = self.schemas
        for child in element:
            name = self.names.get(child, child.tag)
            held = None if source is None else _member(self.source, source, child.tag)
            kept = None if target is None else _member(self.to, target, name)
            if held is not None and kept is None:
                self._leaving(element, child, source, target, track)
                continue
            source_class = _class_of(self.source, source, child.tag)
            target_class = _class_of(self.to, target, name)
            if source_class is None and held is None and self._dropped_extra(source, target, child.tag):
                # A member the set holds that neither schema declares: Live removed it with a change
                # of the older schema the set was saved before, so the newer one has no place for it.
                assert source is not None
                self._drop(element, child, source)
                continue
            if source_class is not None and schemas.declares_class(self.source, source_class):
                if target_class is None or not schemas.declares_class(self.to, target_class):
                    self.gone.add(source_class)
                    continue
                if _legacy_members(self.source, source_class, self.to, target_class):
                    if source_class in _LEGACY_RENAMES:
                        self._legacy(child, source_class, target_class)
                    else:
                        self.legacy.add(source_class)
                if (
                    _member(self.to, target_class, Tag.AUTOMATION_ENVELOPES) is not None
                    and _member(self.source, source_class, Tag.AUTOMATION_ENVELOPES) is None
                ):
                    self.tracks.append(child)
            if self.reformat and kept is not None and _is_list(kept):
                self._number(child)
            self._walk(child, source_class, target_class, child if self.tracks and self.tracks[-1] is child else track)

    def _legacy(self, device: ET.Element, source_class: str, target_class: str) -> None:
        """Rename ``device``'s rebuilt members as Live does and turn its legacy mode on, before the first of them."""
        first = None
        for old, new in _LEGACY_RENAMES[source_class].items():
            member = device.find(old)
            if member is not None:
                self.renames.append((member, new))
                self.names[member] = new
                first = member if first is None or list(device).index(member) < list(device).index(first) else first
        if _member(self.to, target_class, _LEGACY_MODE) is not None and device.find(_LEGACY_MODE) is None:
            self.legacy_modes.append((device, first))

    def _leaving(
        self, element: ET.Element, child: ET.Element, source: str | None, target: str | None, track: ET.Element | None
    ) -> None:
        """A member the older schema declares and the newer one does not: renamed, moved or removed."""
        assert source is not None
        renamed = _RENAMED.get(child.tag)
        if renamed is not None and target is not None and _member(self.to, target, renamed) is not None:
            self.renames.append((child, renamed))
            return
        self.drops.append((element, child))
        if child.tag == Tag.ARRANGER_AUTOMATION:
            self._automation(element, child, track)
        elif child.tag == _MODULATION_AUTOMATION:
            if child.find(f"{Tag.EVENTS}/*") is not None:
                self.problems.add("the set holds arrangement modulation, which Live 10 keeps nowhere")
        else:
            self.removed.add(f"{source}.{child.tag}")

    def _dropped_extra(self, source: str | None, target: str | None, tag: str) -> bool:
        if not self.reformat or source is None or target is None:
            return False
        schemas = self.schemas
        return (
            schemas.declares_class(self.source, source)
            and schemas.declares_class(self.to, target)
            and _member(self.to, target, tag) is None
        )

    def _drop(self, element: ET.Element, child: ET.Element, source: str) -> None:
        self.drops.append((element, child))
        self.removed.add(f"{source}.{child.tag}")

    def _automation(self, parameter: ET.Element, arranger: ET.Element, track: ET.Element | None) -> None:
        events = arranger.find(Tag.EVENTS)
        if events is None or not is_automated(events):
            return
        target = parameter.find(Tag.AUTOMATION_TARGET)
        if track is None or target is None or Attr.ID not in target.attrib:
            self.problems.add(f"the set holds {parameter.tag} automation with no track or target to move it to")
            return
        self.envelopes.setdefault(track, []).append((target.attrib[Attr.ID], events))

    def _number(self, entries: ET.Element) -> None:
        numbered = [Attr.ID in entry.attrib for entry in entries]
        if not all(numbered):
            if any(numbered):
                self.problems.add(f"the set's {entries.tag} list numbers only some of its entries")
            else:
                self.unnumbered.append(entries)

    def apply(self) -> None:
        for element, name in self.renames:
            element.tag = name
        for device, before in self.legacy_modes:
            _insert_value(device, _LEGACY_MODE, "true", before)
        for entries in self.unnumbered:
            for position, entry in enumerate(entries):
                _set_id(entry, position)
        for track in self.tracks:
            _add_envelopes(track, self.envelopes.get(track, []))
        for parent, child in self.drops:
            parent.remove(child)
        root, schemas = self.root, self.schemas
        if self.reformat:
            live_set = root.find(Tag.LIVE_SET)
            if (
                live_set is not None
                and live_set.find(Tag.NEXT_POINTEE_ID) is None
                and _member(self.to, Tag.LIVE_SET, Tag.NEXT_POINTEE_ID) is not None
            ):
                _add_next_pointee_id(root, live_set)
            root.set(Attr.MAJOR_VERSION, "5")
        root.set(Attr.MINOR_VERSION, self.to)
        count = schemas.change_counts[schemas.minor_versions.index(self.to)]
        if count is not None:
            root.set(Attr.SCHEMA_CHANGE_COUNT, str(count))


def _add_envelopes(track: ET.Element, moved: list[tuple[str, ET.Element]]) -> None:
    """Give ``track`` the ``AutomationEnvelopes`` Live 10 writes, holding ``moved``: (target id, events)."""
    holder = ET.Element(Tag.AUTOMATION_ENVELOPES)
    envelopes = ET.SubElement(holder, Tag.ENVELOPES)
    for position, (target_id, events) in enumerate(moved):
        envelope = ET.SubElement(envelopes, Tag.AUTOMATION_ENVELOPE, {Attr.ID: str(position)})
        ET.SubElement(ET.SubElement(envelope, Tag.ENVELOPE_TARGET), Tag.POINTEE_ID, {Attr.VALUE: target_id})
        ET.SubElement(envelope, Tag.AUTOMATION).append(events)
        for number, event in enumerate(events):
            _set_id(event, number)
    # Live 10 writes it after the track's colour.
    anchor = track.find(Tag.COLOR_INDEX)
    if anchor is None:
        anchor = track.find(Tag.NAME)
    at = 0 if anchor is None else list(track).index(anchor) + 1
    tail = (track.text if anchor is None else anchor.tail) or ""
    ET.indent(holder, space="\t", level=len(tail) - len(tail.rstrip("\t")))
    holder.tail = tail
    track.insert(at, holder)


def _insert_value(parent: ET.Element, tag: str, value: str, before: ET.Element | None) -> None:
    """Put ``<tag Value=value />`` in ``parent`` before ``before``, or last, indented like its siblings."""
    element = ET.Element(tag, {Attr.VALUE: value})
    children = list(parent)
    at = len(children) if before is None else children.index(before)
    if at < len(children):
        element.tail = parent.text if at == 0 else children[at - 1].tail
    elif children:
        element.tail, children[-1].tail = children[-1].tail, (children[-2].tail if len(children) > 1 else parent.text)
    parent.insert(at, element)


def _add_next_pointee_id(root: ET.Element, live_set: ET.Element) -> None:
    """Start Live 10's set-global id counter past every id the set already owns."""
    from abletoolz.live_set.xml_edit import owns_pointee_id  # abletoolz.live_set reaches back here

    owned = (
        int(element.attrib[Attr.ID])
        for element in root.iter()
        if owns_pointee_id(element.tag) and Attr.ID in element.attrib
    )
    counter = ET.Element(Tag.NEXT_POINTEE_ID, {Attr.VALUE: str(max(owned, default=0) + 1)})
    counter.tail = live_set.text
    live_set.insert(0, counter)


def _set_id(element: ET.Element, number: int) -> None:
    """Number a list entry with ``Id`` first, as Live writes it."""
    attributes = dict(element.attrib)
    element.attrib.clear()
    element.set(Attr.ID, str(number))
    element.attrib.update(attributes)


def _is_list(kind: str) -> bool:
    """Whether a member of type ``kind`` holds entries Live's format 5 numbers."""
    return kind == "RemoteableList" or kind.startswith(("List<", "Slot<"))


@functools.cache
def _member(version: str, owner: str, member: str) -> str | None:
    """:meth:`LiveSchemas.member_type` of the packed schemas, remembered: a walk asks the same few thousand times."""
    return live_schemas().member_type(version, owner, member)


@functools.cache
def _legacy_members(source: str, source_class: str, to: str, target_class: str) -> bool:
    """Whether ``to`` gives the class a legacy-mode member the older schema's class lacks."""
    schemas = live_schemas()
    added = (schemas.members_of(to, target_class) or frozenset()) - (
        schemas.members_of(source, source_class) or frozenset()
    )
    return any(member.startswith("Legacy") for member in added)


@functools.cache
def _class_of(version: str, owner: str | None, tag: str) -> str | None:
    """Resolve a member through its owner; list entries and roots name their own class. Remembered, like ``_member``."""
    held = None if owner is None else _member(version, owner, tag)
    if held is None:
        return tag if live_schemas().declares_class(version, tag) else None
    generic = _GENERIC.fullmatch(held)
    return generic.group(1) if generic else held
