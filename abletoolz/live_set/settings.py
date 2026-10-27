"""One setting of any object in a set -- a device, a track, its mixer, a clip, the transport -- read and written.

:func:`resolve` is the one place a settings member meets the XML: it asks
:func:`abletoolz.schema.settings.path_for` where the set's Live version keeps
the setting, fills list positions, and hands back a :class:`Setting` that says
what it found and where. The path it resolved travels with it, so a caller can
always see which element a member landed on.
"""

from __future__ import annotations

import dataclasses
import enum
import logging
import math
import re
from collections.abc import Mapping
from xml.etree import ElementTree as ET

from abletoolz.errors import ElementNotFound
from abletoolz.live_set.xml_edit import number_value
from abletoolz.schema import Attr, Tag
from abletoolz.schema.devices import ValueKind
from abletoolz.schema.objects import Track
from abletoolz.schema.settings import SettingUnavailable, is_templated, kind_of, path_for, spellings
from abletoolz.schema.versioning import Version, version_text

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True, slots=True)
class Setting:
    """One setting, read and written as the value it is.

    ``path`` is where this set keeps it -- the member's own value, or an older
    spelling when the set's Live version wrote it there -- and ``version`` is
    that set's version. ``kind`` is the value type Live's schema declares for
    it. Live writes a whole float without a decimal point, so INT and FLOAT
    both read as a number; on write FLOAT takes any finite number and INT
    (enums too) a whole one.
    """

    member: enum.StrEnum
    path: str
    version: Version
    kind: ValueKind
    element: ET.Element = dataclasses.field(repr=False, compare=False)

    def __repr__(self) -> str:
        return f"{type(self.member).__name__}.{self.member.name} → {self.path} (Live {version_text(self.version)})"

    @property
    def raw(self) -> str:
        """The ``Value`` attribute exactly as the set holds it."""
        value = self.element.get(Attr.VALUE)
        if value is None:
            raise ElementNotFound(f"{self.path} has no Value attribute")
        return value

    @property
    def value(self) -> bool | int | float | str:
        raw = self.raw
        if self.kind is ValueKind.BOOL:
            return raw == "true"
        if self.kind in (ValueKind.INT, ValueKind.FLOAT):
            number = float(raw)
            return int(number) if number.is_integer() else number
        return raw

    def encode(self, value: bool | float | str) -> str:
        """``value`` as Live writes it, refusing one of the wrong kind."""
        if self.kind is ValueKind.BOOL:
            if not isinstance(value, bool):
                raise TypeError(f"{self.path} is a switch; give True or False, not {value!r}")
            return "true" if value else "false"
        if self.kind in (ValueKind.INT, ValueKind.FLOAT):
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise TypeError(f"{self.path} is a number, not {value!r}")
            if not math.isfinite(value):
                raise ValueError(f"{self.path} takes a finite number, not {value!r}")
            if self.kind is ValueKind.INT and not float(value).is_integer():
                raise TypeError(f"{self.path} is a whole number, not {value!r}")
            if self.member is Track.COLOR and not 0 <= value <= 69:
                raise ValueError("Color index must be within 0 - 69")
            return number_value(value)
        if not isinstance(value, str):
            raise TypeError(f"{self.path} holds text, not {value!r}")
        return value

    def set(self, value: bool | float | str) -> None:
        """Write ``value`` the way Live would."""
        self.element.set(Attr.VALUE, self.encode(value))


def _fill(template: str, indices: Mapping[str, int]) -> str:
    """Fill list positions; a list written without ids takes the same index as a 1-based position."""
    positions = {f"{name}_position": index + 1 for name, index in indices.items()}
    return template.format(**indices, **positions)


def _candidates(member: enum.StrEnum, version: Version, owner_tag: str) -> list[str]:
    """The path ``version`` resolves to, then the member's other recorded spellings."""
    resolved = path_for(member, version, owner_tag)
    return [resolved, *(spelled for _, spelled in spellings(member) if spelled != resolved)]


def _setting(member: enum.StrEnum, path: str, version: Version, element: ET.Element) -> Setting:
    # A parameter keeps its value in Manual; a setting that later became one did not.
    manual = element.find(Tag.MANUAL)
    return Setting(
        member=member,
        path=path,
        version=version,
        kind=kind_of(member),
        element=element if manual is None else manual,
    )


def _label(member: enum.StrEnum) -> str:
    return f"{type(member).__name__}.{member.name}"


def resolve(
    owner: ET.Element,
    member: enum.StrEnum,
    version: Version,
    indices: Mapping[str, int] | None = None,
    *,
    owner_tag: str | None = None,
    label: str | None = None,
) -> Setting:
    """``member`` inside ``owner``, an element of a set saved by ``version``.

    ``owner_tag`` is the element type the census counts this owner as, when
    that is not ``owner.tag`` itself (a mixer belongs to its track's type).
    A setting that lives in a list (an EQ band, a send, an automation lane)
    takes its position in ``indices``: ``{"index": 3}``.

    The path the version resolves to is tried first. Where the corpus has no
    sets of that version, a rename's exact version is not known, so the other
    recorded spellings are tried after it; the one found is the ``path`` the
    result reports, and a debug line says so.
    """
    indices = indices or {}
    name = label or owner.tag
    if is_templated(member) and not indices:
        raise ValueError(f"{_label(member)} is in a list; say which one, e.g. index=0")
    candidates = _candidates(member, version, owner_tag or owner.tag)
    for candidate in candidates:
        filled = _fill(candidate, indices)
        element = owner.find(filled)
        if element is None:
            continue
        if candidate != candidates[0]:
            logger.debug("%s: %s has no %s; found the %s spelling", _label(member), name, candidates[0], filled)
        elif candidate != member.value:
            logger.debug("%s is at %s in a Live %s set", _label(member), filled, version)
        return _setting(member, filled, version, element)
    raise ElementNotFound(f"{name} has no {_fill(candidates[0], indices)}")


def find(
    owner: ET.Element,
    member: enum.StrEnum,
    version: Version,
    indices: Mapping[str, int] | None = None,
    *,
    owner_tag: str | None = None,
    label: str | None = None,
) -> Setting | None:
    """:func:`resolve`, or None where this element has no such setting in this version."""
    try:
        return resolve(owner, member, version, indices, owner_tag=owner_tag, label=label)
    except ElementNotFound:
        return None


_ID_SEGMENT = re.compile(r"^(?P<tag>[^\[]+)\[@Id='\{index\d*\}'\]$")
_POSITION_SEGMENT = re.compile(r"^(?P<tag>[^\[]+)\[\{index\d*_position\}\]$")
_NUMBERED_SEGMENT = re.compile(r"^(?P<stem>.+)\.\{index\d*\}$")


def _expand(owner: ET.Element, template: str) -> list[tuple[str, ET.Element]]:
    """Every element ``template`` reaches, each with the path that names it, list placeholders taken as 'all'."""
    reached: list[tuple[str, ET.Element]] = [("", owner)]
    for segment in template.split("/"):
        following: list[tuple[str, ET.Element]] = []
        for prefix, element in reached:
            if (match := _ID_SEGMENT.match(segment)) is not None:
                for child in element.findall(match["tag"]):
                    if (element_id := child.get(Attr.ID)) is not None:
                        following.append((f"{prefix}{match['tag']}[@Id='{element_id}']/", child))
            elif (match := _POSITION_SEGMENT.match(segment)) is not None:
                for position, child in enumerate(element.findall(match["tag"]), start=1):
                    following.append((f"{prefix}{match['tag']}[{position}]/", child))
            elif (match := _NUMBERED_SEGMENT.match(segment)) is not None:
                for child in element:
                    stem, _, number = child.tag.rpartition(".")
                    if stem == match["stem"] and number.isdigit():
                        following.append((f"{prefix}{child.tag}/", child))
            elif (found := element.find(segment)) is not None:
                following.append((f"{prefix}{segment}/", found))
        reached = following
    return [(prefix.rstrip("/"), element) for prefix, element in reached]


def every(owner: ET.Element, member: enum.StrEnum, version: Version, *, owner_tag: str | None = None) -> list[Setting]:
    """``member`` at every position of its list inside ``owner`` (every lane, every send), in document order.

    For a member outside any list this is that one setting, or nothing. An
    element that does not have the setting at all gives an empty list rather
    than an error: "every send" of a track with no sends is none.
    """
    try:
        candidates = _candidates(member, version, owner_tag or owner.tag)
    except SettingUnavailable:
        return []
    for candidate in candidates:
        reached = _expand(owner, candidate)
        if reached:
            return [_setting(member, path, version, element) for path, element in reached]
    return []
