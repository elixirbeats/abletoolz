"""Looking up elements and attributes in Live's XML by path."""

from collections.abc import Collection, Sequence
from typing import Literal, overload
from xml.etree import ElementTree as ET

from abletoolz.ansi import R
from abletoolz.errors import ElementNotFound
from abletoolz.schema import Attr, Tag, path


@overload
def get_element(
    root: ET.Element,
    attribute_path: str,
) -> ET.Element: ...


@overload
def get_element(
    root: ET.Element,
    attribute_path: str,
    *,
    silent_error: Literal[False] = False,
    attribute: Literal[None] = None,
) -> ET.Element: ...


@overload
def get_element(
    root: ET.Element,
    attribute_path: str,
    *,
    silent_error: Literal[True],
    attribute: Literal[None] = None,
) -> ET.Element | None: ...


@overload
def get_element(
    root: ET.Element,
    attribute_path: str,
    *,
    silent_error: Literal[False] = False,
    attribute: str,
) -> str: ...


@overload
def get_element(
    root: ET.Element,
    attribute_path: str,
    *,
    attribute: str,
    silent_error: Literal[True],
) -> str | None: ...


def get_element(
    root: ET.Element,
    attribute_path: str,
    *,
    silent_error: bool = False,
    attribute: str | None = None,
) -> ET.Element | str | None:
    """Get element using Element tree xpath syntax."""
    element = root.findall(f"./{'/'.join(attribute_path.split('.'))}")
    if not element:
        if silent_error:
            return None
        raise ElementNotFound(f"{R}No element for path [{attribute_path}]")
    if attribute:
        attr = element[0].get(attribute)
        if attr is None:
            raise ElementNotFound(f"{R}Element {attribute}is empty!")
        return attr
    return element[0]


def remove_children(container: ET.Element, doomed: Sequence[ET.Element]) -> None:
    """Drop elements from a list, keeping the list's own closing indentation."""
    for element in doomed:
        children = list(container)
        if children[-1] is element and len(children) > 1:
            children[-2].tail = element.tail
        container.remove(element)
    if not len(container):
        container.text = None


def drop_envelopes(root: ET.Element, target_ids: Collection[str | None]) -> int:
    """Remove every envelope under ``root`` whose target is one of ``target_ids``; answers how many.

    That is a track's own automation and the ``ClipEnvelope`` of every clip,
    wherever they sit under ``root``, the way ``save_as`` drops orphaned envelopes.
    """
    removed = 0
    for holder in list(root.iter()):
        doomed = [
            envelope
            for envelope in holder
            if (target := envelope.find(path(Tag.ENVELOPE_TARGET, Tag.POINTEE_ID))) is not None
            and target.get(Attr.VALUE) in target_ids
        ]
        if doomed:
            remove_children(holder, doomed)
            removed += len(doomed)
    return removed
