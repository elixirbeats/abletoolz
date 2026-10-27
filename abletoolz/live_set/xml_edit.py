"""Tree surgery shared by every authoring path: indentation, set-global ids, remote bindings.

Live pretty-prints with tabs, so a subtree copied to a new depth has to be
re-indented or the file stops looking like Live's own. And a subtree copied
into a set has to give up the ids it owns in the set-global space -- one
counter, ``LiveSet/NextPointeeId``, shared by ``Pointee``, every
``*AutomationTarget`` and ``*ModulationTarget`` (warp/audio-clip properties:
``VolumeModulationTarget``, ``TranspositionModulationTarget``,
``GrainSizeModulationTarget``, ``FluxModulationTarget``,
``SampleOffsetModulationTarget``, ``TransientEnvelopeModulationTarget``,
``ComplexProEnvelopeModulationTarget``, ``ComplexProFormantsModulationTarget``)
and every ``ControllerTargets.<N>`` (MIDI tracks' controller list) -- for
fresh ones, or two things answer to the same id and Live refuses the set.

A copied subtree also gives up its ``KeyMidi`` remote bindings, the key/MIDI
mappings a parameter carries: a device transplanted with them crashes Live 12
on load (measured by bisecting an authored set down to one device, otherwise
byte-identical to the donor's), and they mean nothing in a document that never
had the mapping.

None of the three jobs depends on what the subtree is, so all of them live here.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

from abletoolz.elements import get_element
from abletoolz.schema import Attr, Tag, path


def owns_pointee_id(tag: str) -> bool:
    """Whether an element with this tag owns an id in the set-global (NextPointeeId) space."""
    return (
        tag == Tag.POINTEE
        or tag.endswith(Tag.AUTOMATION_TARGET)
        or tag.endswith(Tag.MODULATION_TARGET)
        or tag.startswith("ControllerTargets.")
    )


def number_value(value: float) -> str:
    """Live writes whole numbers without a decimal point; anything else round-trips."""
    if value == int(value):
        return str(int(value))
    return repr(value)


def tab_depth(text: str | None) -> int:
    """How deep the indentation in ``text`` sits, in tabs."""
    return 0 if text is None else len(text) - len(text.rstrip("\t"))


def shift_indentation(element: ET.Element, levels: int) -> None:
    """Move a whole subtree's pretty-printing in or out by ``levels`` tabs.

    Only whitespace-only text is touched, so hex blobs and real values never are.
    """
    if levels == 0:
        return

    def shifted(text: str | None) -> str | None:
        if text is None or text.strip():
            return text
        if levels > 0:
            return text.replace("\n", "\n" + "\t" * levels)
        return text.replace("\n" + "\t" * -levels, "\n")

    for node in element.iter():
        node.text = shifted(node.text)
        node.tail = shifted(node.tail)


def strip_remote_bindings(subtree: ET.Element) -> None:
    """Drop every ``KeyMidi`` remote binding a copied subtree brought along.

    Measured on Live 12.4.5b11: a plugin device transplanted between sets with
    its bindings intact is an access violation at load time, after every
    plugin has restored. The donor set itself opens, so the bindings are not
    malformed; a copy just must not carry them.

    The last remaining sibling inherits the removed element's tail, so the
    parent still closes on its own indent, and a parent left with no children
    at all closes the way Live writes an empty element.
    """
    for parent in list(subtree.iter()):
        children = list(parent)
        bindings = [child for child in children if child.tag == Tag.KEY_MIDI]
        if not bindings:
            continue
        for binding in bindings:
            parent.remove(binding)
        remaining = list(parent)
        if remaining:
            remaining[-1].tail = children[-1].tail
        else:
            parent.text = None


def renumber_pointee_ids(subtree: ET.Element, root: ET.Element) -> dict[str, str]:
    """Give a copied subtree fresh set-global ids and move NextPointeeId past them.

    References are remapped only when they name an id the subtree itself
    owned: a ``PointeeId`` pointing outside the copy (a clip envelope naming a
    device parameter, say) still means what it meant and is left alone.
    Returns the old-id to new-id mapping, empty when the subtree owns nothing
    -- which is also why pre-10 sets, which have no ``NextPointeeId`` element
    to advance, never reach the allocator.
    """
    owners = [node for node in subtree.iter() if owns_pointee_id(node.tag) and Attr.ID in node.attrib]
    if not owners:
        return {}
    counter_element = get_element(root, path(Tag.LIVE_SET, Tag.NEXT_POINTEE_ID))
    next_id = int(get_element(root, path(Tag.LIVE_SET, Tag.NEXT_POINTEE_ID), attribute=Attr.VALUE))
    renumbered: dict[str, str] = {}
    for owner in owners:
        renumbered[owner.attrib[Attr.ID]] = str(next_id)
        owner.set(Attr.ID, str(next_id))
        next_id += 1
    for reference in subtree.iter(Tag.POINTEE_ID):
        replacement = renumbered.get(reference.get(Attr.VALUE, ""))
        if replacement is not None:
            reference.set(Attr.VALUE, replacement)
    counter_element.set(Attr.VALUE, str(next_id))
    return renumbered
