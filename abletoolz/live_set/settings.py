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
from collections.abc import Mapping
from xml.etree import ElementTree as ET

from abletoolz.errors import ElementNotFound
from abletoolz.live_set.xml_edit import number_value
from abletoolz.schema import Attr, Tag
from abletoolz.schema.devices import ValueKind
from abletoolz.schema.settings import is_templated, kind_of, path_for, spellings
from abletoolz.schema.versioning import Version

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True, slots=True)
class Setting:
    """One setting, read and written as the value it is.

    ``path`` is where this set keeps it -- the member's own value, or an older
    spelling when the set's Live version wrote it there -- and ``version`` is
    that set's version. ``kind`` is what the census saw the setting hold. Whole
    numbers are what Live writes for any number that sits on one, so INT and
    FLOAT both read as a number and take any number on write.
    """

    member: enum.StrEnum
    path: str
    version: Version
    kind: ValueKind
    element: ET.Element = dataclasses.field(repr=False, compare=False)

    def __repr__(self) -> str:
        version = ".".join(str(part) for part in self.version)
        return f"{type(self.member).__name__}.{self.member.name} → {self.path} (Live {version})"

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
            return number_value(value)
        if not isinstance(value, str):
            raise TypeError(f"{self.path} holds text, not {value!r}")
        return value

    def set(self, value: bool | float | str) -> None:
        """Write ``value`` the way Live would."""
        self.element.set(Attr.VALUE, self.encode(value))


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
    member_label = f"{type(member).__name__}.{member.name}"
    if is_templated(member) and not indices:
        raise ValueError(f"{member_label} is in a list; say which one, e.g. index=0")
    resolved = path_for(member, version, owner_tag or owner.tag)
    candidates = [resolved, *(spelled for _, spelled in spellings(member) if spelled != resolved)]
    for candidate in candidates:
        filled = candidate.format(**indices)
        element = owner.find(filled)
        if element is None:
            continue
        if candidate != resolved:
            logger.debug("%s: %s has no %s; found the %s spelling", member_label, name, resolved, filled)
        elif candidate != member.value:
            logger.debug("%s is at %s in a Live %s set", member_label, filled, version)
        # A parameter keeps its value in Manual; a setting that later became one did not.
        manual = element.find(Tag.MANUAL)
        return Setting(
            member=member,
            path=filled,
            version=version,
            kind=kind_of(member),
            element=element if manual is None else manual,
        )
    raise ElementNotFound(f"{name} has no {resolved.format(**indices)}")
