"""Every settings enum, devices and set objects alike, resolved against the version of the set it is used in.

A settings member names what a setting is -- ``Sampler.VOLUME``,
``Track.COLOR``, ``Mixer.TEMPO`` -- and its value is where the newest Live
version writes it. Older versions sometimes wrote it elsewhere, and a few did
not write it at all. :func:`path_for` turns a member and a set's version into
the path that set uses, from the tables the census generated beside the enums
(:mod:`abletoolz.schema.devices`, :mod:`abletoolz.schema.objects`), so feature
code names a member and never a version.

Refusals rest on evidence only. The census knows the sets it read; a setting
it first saw in Live 11 says nothing about a Live 10 set unless the corpus
holds the same object from a version at or after that set's and still without
the setting. Where the corpus is silent, the lookup goes ahead and the XML
answers.
"""

from __future__ import annotations

import bisect
import enum
from collections.abc import Mapping
from types import ModuleType
from typing import cast

from abletoolz.errors import ElementNotFound
from abletoolz.schema import devices, objects
from abletoolz.schema.devices import ValueKind
from abletoolz.schema.versioning import Version

__all__ = [
    "NotInThisVersion",
    "NotOnThisElement",
    "SettingUnavailable",
    "first_seen",
    "is_parameter",
    "is_templated",
    "kind_of",
    "owner_tag",
    "path_for",
    "spellings",
]

_MODULES: tuple[ModuleType, ...] = (devices, objects)
_ZERO: Version = (0, 0, 0)


def _merged_versions() -> dict[str, tuple[Version, ...]]:
    merged: dict[str, set[Version]] = {}
    for module in _MODULES:
        for owner, seen in module.VERSIONS_SEEN.items():
            merged.setdefault(owner, set()).update(seen)
    return {owner: tuple(sorted(seen)) for owner, seen in merged.items()}


_VERSIONS_SEEN = _merged_versions()


class SettingUnavailable(ElementNotFound, LookupError):
    """The setting is not there to be read or written in this element.

    An :class:`~abletoolz.errors.ElementNotFound` with a reason attached, so
    code that already treats a missing element as "this set has no such
    setting" keeps working.
    """


class NotInThisVersion(SettingUnavailable):
    """The set was saved by a Live version that did not have the setting yet."""


class NotOnThisElement(SettingUnavailable):
    """The setting belongs to other element types than this one (a MIDI input on a return track)."""


def _table(name: str, setting: enum.StrEnum) -> Mapping[enum.StrEnum, object] | frozenset[enum.StrEnum]:
    """``setting``'s class's row of the generated table ``name``, whichever module holds it."""
    settings_class = type(setting)
    for module in _MODULES:
        table: Mapping[type[enum.StrEnum], Mapping[enum.StrEnum, object] | frozenset[enum.StrEnum]]
        table = getattr(module, name)
        if settings_class in table:
            return table[settings_class]
    raise TypeError(f"{settings_class.__name__} is not a settings enum")


def _label(setting: enum.StrEnum) -> str:
    return f"{type(setting).__name__}.{setting.name}"


def _version_text(version: Version) -> str:
    return ".".join(str(part) for part in version)


def kind_of(setting: enum.StrEnum) -> ValueKind:
    """What every value the census saw for ``setting`` parsed as."""
    return cast("Mapping[enum.StrEnum, ValueKind]", _table("SETTING_KIND", setting))[setting]


def is_parameter(setting: enum.StrEnum) -> bool:
    """Whether ``setting`` names an automatable parameter (its value sits in a ``Manual`` child)."""
    return setting in _table("PARAMETERS", setting)


def is_templated(setting: enum.StrEnum) -> bool:
    """Whether ``setting``'s path holds ``{index}`` placeholders to fill before use."""
    return setting in _table("TEMPLATED", setting)


def first_seen(setting: enum.StrEnum) -> Version:
    """The first Live version the census saw ``setting`` in."""
    return cast("Mapping[enum.StrEnum, Version]", _table("FIRST_SEEN", setting))[setting]


def spellings(setting: enum.StrEnum) -> tuple[tuple[Version, str], ...]:
    """Every path Live wrote ``setting`` at, newest first, each from the first version that writes it."""
    table = cast("Mapping[enum.StrEnum, tuple[tuple[Version, str], ...]]", _table("OLDER_PATHS", setting))
    older = table.get(setting)
    return older if older is not None else ((_ZERO, str(setting.value)),)


def owner_tag(tag: str) -> str:
    """An element tag as the census counts it: ``MasterTrack`` is the main track's older name."""
    return objects.OWNER_SPELLINGS.get(tag, tag)


def _seen_on(setting: enum.StrEnum) -> frozenset[str] | None:
    """The owners of a setting that is not on every element its class covers; None when it is."""
    table: Mapping[type[enum.StrEnum], Mapping[enum.StrEnum, frozenset[str]]] = objects.SEEN_ON
    per_class = table.get(type(setting))
    return None if per_class is None else per_class.get(setting)


def _corpus_lacks(owner: str, version: Version, first: Version) -> bool:
    """Whether the corpus holds ``owner`` from a version at or after ``version`` and before ``first``."""
    seen = _VERSIONS_SEEN.get(owner, ())
    position = bisect.bisect_left(seen, version)
    return position < len(seen) and seen[position] < first


def path_for(setting: enum.StrEnum, version: Version, owner: str) -> str:
    """Where a set saved by ``version`` keeps ``setting`` inside an ``owner`` element.

    ``owner`` is the element's tag (``AudioTrack``, ``Eq8``, ``MidiClip``).
    Raises :class:`NotOnThisElement` when the census saw that element type
    many times and never with this setting, and :class:`NotInThisVersion`
    when it saw the element in a version at or after ``version`` and before
    the setting first appeared. Placeholders are left for the caller to fill.
    """
    owner = owner_tag(owner)
    owners = _seen_on(setting)
    if owners is not None and owner not in owners and owner in _VERSIONS_SEEN:
        raise NotOnThisElement(f"{_label(setting)} is not on a {owner}; it is on {', '.join(sorted(owners))}")
    first = first_seen(setting)
    if version < first and _corpus_lacks(owner, version, first):
        raise NotInThisVersion(
            f"{_label(setting)} first appears in Live {_version_text(first)}; "
            f"a {owner} saved by Live {_version_text(version)} has no such setting"
        )
    for since, spelled in spellings(setting):
        if version >= since:
            return spelled
    return spellings(setting)[-1][1]
