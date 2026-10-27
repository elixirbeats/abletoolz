"""What Live's own schemas declare: the classes and members of each document schema, by ``MinorVersion``.

Live reads a set through the AbletonSchema resource its root ``MinorVersion``
names, and ships one for every schema back to Live 8 (203 in Live 12.4.7).
:file:`data/live_schemas.json.gz`, packed from them by
``scripts/gen_live_schemas.py``, says which classes each one declares and which
members each class has, with each member's type. A device's element tag is its
class (``Eq8``), and a member is a child tag (``Bands.0``) whose type is the
class it holds (``Eq8Band``), so a path inside an element walks from class to
class.

A resource is the last state of its ``MinorVersion``. A set also carries a
``SchemaChangeCount``, and sets with a lower count than the resource's own were
written before some of its changes: Live 12.2.6 sets (``12.0_12203``, count 3)
hold members the shipped ``12.0_12203`` (count 4) does not declare. So the
schema says what Live declares, not everything a set of that version holds;
:mod:`abletoolz.schema.settings` lets the corpus overrule it where a real set
did.
"""

from __future__ import annotations

import functools
import gzip
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict, cast

DATA = Path(__file__).with_name("data") / "live_schemas.json.gz"

_GENERIC = re.compile(r"^\w+<(\w+)>$")
_PREDICATE = re.compile(r"\[[^\]]*\]")
_PLACEHOLDER = re.compile(r"\{\w+\}")
# Live's value types, by the kind of value each reads as; an enum is stored as Int.
_KINDS = {"Bool": "bool", "Int": "int", "Int64": "int", "Float": "float", "Double": "float", "String": "str"}


@dataclass(frozen=True, slots=True)
class ValueType:
    """What a setting's element holds, as a schema declares it."""

    kind: str
    """``bool``, ``int``, ``float`` or ``str``, the values of :class:`abletoolz.schema.devices.ValueKind`."""
    timeable: bool
    """Whether the value sits in a ``Manual`` child: an automatable parameter."""


class _SchemaRow(TypedDict):
    minor_version: str
    change_count: int | None
    sha256: str


class _Packed(TypedDict):
    format: int
    source: str
    schemas: list[_SchemaRow]
    classes: dict[str, str]
    members: dict[str, dict[str, dict[str, str]]]


@dataclass(frozen=True, slots=True)
class LiveSchemas:
    """Every schema resource Live ships, as bitmasks over :attr:`minor_versions`."""

    source: str
    minor_versions: tuple[str, ...]
    change_counts: tuple[int | None, ...]
    classes: dict[str, int]
    members: dict[str, dict[str, dict[str, int]]]

    def bit(self, minor_version: str) -> int | None:
        """The schema's bit in every mask, or None when Live ships no schema of that ``MinorVersion``."""
        try:
            return 1 << self.minor_versions.index(minor_version)
        except ValueError:
            return None

    def member_type(self, minor_version: str, owner: str, member: str) -> str | None:
        """The class ``owner``'s ``member`` holds in that schema, or None when it declares no such member."""
        bit = self.bit(minor_version)
        if bit is None:
            return None
        for kind, mask in self.members.get(owner, {}).get(member, {}).items():
            if mask & bit:
                return kind
        return None

    def declares_class(self, minor_version: str, name: str) -> bool:
        bit = self.bit(minor_version)
        return bit is not None and bool(self.classes.get(name, 0) & bit)

    def declares(self, minor_version: str, owner: str, path: str) -> bool | None:
        """Whether that schema declares ``path`` inside an ``owner`` element.

        ``path`` is a settings path: list placeholders (``Bands.{index}``) and
        predicates (``TrackSendHolder[@Id='{index}']``) are allowed. None when
        the schema cannot say: Live ships no schema of that ``MinorVersion``,
        it does not declare ``owner``, or the path steps into an element whose
        class it does not declare, such as an entry of an untyped list.
        """
        found = self._walk(minor_version, owner, path)
        return found if found is None or found is False else True

    def value_type(self, minor_version: str, owner: str, path: str) -> ValueType | None:
        """What the element at ``path`` inside an ``owner`` element holds in that schema.

        The element's class says it: a ``Timeable*`` class keeps its value in
        a ``Manual`` child, a ``Remoteable*`` one in its own ``Value``, and a
        member declared as a plain type (``Int``, ``Double``) is an element
        with that ``Value``. None when the schema does not declare the path,
        cannot say (as for :meth:`declares`), or the class holds no such value.
        """
        leaf = self._walk(minor_version, owner, path)
        if not isinstance(leaf, str):
            return None
        if leaf in _KINDS:
            return ValueType(_KINDS[leaf], timeable=False)
        manual = self.member_type(minor_version, leaf, "Manual")
        holder = leaf if manual is None else manual
        kind = _KINDS.get(self.member_type(minor_version, holder, "Value") or "")
        return None if kind is None else ValueType(kind, timeable=manual is not None)

    def newest_value_type(self, owners: Iterable[str], paths: Iterable[str]) -> ValueType | None:
        """:meth:`value_type` in the newest schema that types any of ``paths`` in any of ``owners``.

        None when no schema does, or when that schema types them differently.
        """
        pairs = [(owner, path) for owner in owners for path in paths]
        for minor_version in reversed(self.minor_versions):
            found = {self.value_type(minor_version, owner, path) for owner, path in pairs} - {None}
            if found:
                return found.pop() if len(found) == 1 else None
        return None

    def _walk(self, minor_version: str, owner: str, path: str) -> str | Literal[False] | None:
        """The class or type ``path`` ends at inside an ``owner`` element; False when undeclared, None when unknown."""
        if not self.declares_class(minor_version, owner):
            return None
        current, element_class_next = owner, False
        kind = owner
        for segment in _PLACEHOLDER.sub("0", _PREDICATE.sub("", path)).split("/"):
            if element_class_next:
                # A list or slot holds elements tagged with their own class.
                if not self.declares_class(minor_version, segment):
                    return None
                current, element_class_next, kind = segment, False, segment
                continue
            if not self.declares_class(minor_version, current):
                return None
            member = self.member_type(minor_version, current, segment)
            if member is None:
                return False
            kind = member
            generic = _GENERIC.match(kind)
            if generic is not None:
                current, element_class_next = generic.group(1), True
            elif self.declares_class(minor_version, kind):
                current = kind
            else:
                current, element_class_next = kind, True
        return kind


def _mask(text: str) -> int:
    return int(text, 16)


@functools.cache
def live_schemas(path: Path = DATA) -> LiveSchemas:
    """The packed schemas, read once."""
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        # scripts/gen_live_schemas.py writes exactly this shape; the tests read it back.
        packed = cast(_Packed, json.load(stream))
    return LiveSchemas(
        source=packed["source"],
        minor_versions=tuple(row["minor_version"] for row in packed["schemas"]),
        change_counts=tuple(row["change_count"] for row in packed["schemas"]),
        classes={name: _mask(mask) for name, mask in packed["classes"].items()},
        members={
            name: {member: {kind: _mask(mask) for kind, mask in types.items()} for member, types in per_class.items()}
            for name, per_class in packed["members"].items()
        },
    )
