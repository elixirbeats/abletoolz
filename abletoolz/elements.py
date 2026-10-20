"""Looking up elements and attributes in Live's XML by path."""

from typing import Literal, overload
from xml.etree import ElementTree as ET

from abletoolz.ansi import R
from abletoolz.errors import ElementNotFound


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
        # ElementTree.dump(root)
        raise ElementNotFound(f"{R}No element for path [{attribute_path}]")
    if attribute:
        attr = element[0].get(attribute)
        if attr is None:
            raise ElementNotFound(f"{R}Element {attribute}is empty!")
        return attr
    return element[0]


@overload
def search_element(
    root: ET.Element,
    search_name: str,
    attribute: Literal[None] = None,
) -> ET.Element | None: ...


@overload
def search_element(root: ET.Element, search_name: str, attribute: str) -> str | None: ...


def search_element(root: ET.Element, search_name: str, attribute: str | None = None) -> ET.Element | str | None:
    """Similar to get_element but searches entire tree instead of absolute path."""
    element = root.find(f".//{search_name}")
    if element is None:
        return None
    if attribute:
        return element.get(attribute)
    return element
