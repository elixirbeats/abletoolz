"""Tree surgery shared by every authoring path: indentation and set-global ids.

Live pretty-prints with tabs, so a subtree copied to a new depth has to be
re-indented or the file stops looking like Live's own. And a subtree copied
into a set has to give up the ids it owns in the set-global space --
``AutomationTarget``, ``ModulationTarget`` and ``Pointee`` elements, counted
by ``LiveSet/NextPointeeId`` -- for fresh ones, or two things answer to the
same id and Live refuses the set.

Neither job depends on what the subtree is, so both live here rather than in
whichever domain module needed them first (clips, then device chains).
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

from abletoolz.misc import get_element

# The only tags observed owning an id in the set-global (NextPointeeId) space.
POINTEE_OWNER_TAGS = frozenset({"AutomationTarget", "ModulationTarget", "Pointee"})


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

    Only whitespace-only text is touched, so hex blobs and real values are
    untouched no matter where a subtree gets grafted.
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


def renumber_pointee_ids(subtree: ET.Element, root: ET.Element) -> dict[str, str]:
    """Give a copied subtree fresh set-global ids and move NextPointeeId past them.

    References are remapped only when they name an id the subtree itself
    owned: a ``PointeeId`` pointing outside the copy (a clip envelope naming a
    device parameter, say) still means what it meant and is left alone.
    Returns the old-id to new-id mapping, empty when the subtree owns nothing
    -- which is also why pre-10 sets, which have no ``NextPointeeId`` element
    to advance, never reach the allocator.
    """
    owners = [node for node in subtree.iter() if node.tag in POINTEE_OWNER_TAGS and "Id" in node.attrib]
    if not owners:
        return {}
    counter_element = get_element(root, "LiveSet.NextPointeeId")
    next_id = int(get_element(root, "LiveSet.NextPointeeId", attribute="Value"))
    renumbered: dict[str, str] = {}
    for owner in owners:
        renumbered[owner.attrib["Id"]] = str(next_id)
        owner.set("Id", str(next_id))
        next_id += 1
    for reference in subtree.iter("PointeeId"):
        replacement = renumbered.get(reference.get("Value", ""))
        if replacement is not None:
            reference.set("Value", replacement)
    counter_element.set("Value", str(next_id))
    return renumbered
