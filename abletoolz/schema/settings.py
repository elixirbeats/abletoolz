"""Every settings enum, devices and set objects alike, resolved against the version of the set it is used in.

A settings member names what a setting is -- ``Sampler.VOLUME``,
``Track.COLOR``, ``Mixer.TEMPO`` -- and its value is where the newest Live
version writes it. Older versions sometimes wrote it elsewhere, and a few did
not write it at all. :func:`path_for` turns a member and a set's version into
the path that set uses, from what the census measured about each member
(``data/settings.json``, generated beside the enums in
:mod:`abletoolz.schema.devices` and :mod:`abletoolz.schema.objects`), so
feature code names a member and never a version.

Whether a version has a setting at all is for Live's own schema of that
version to say (:mod:`abletoolz.schema.live_schemas`): a setting it does not
declare is refused. A real set outranks it, so a setting the corpus saw in a
set of that ``MinorVersion`` is never refused (its ``undeclared`` record), and where
the schema cannot tell -- a release the corpus does not pin to a schema, a
path into an element whose class it does not declare -- the lookup goes ahead
and the XML answers.
"""

from __future__ import annotations

import bisect
import enum
import functools
import json
import re
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from abletoolz.errors import ElementNotFound
from abletoolz.schema import devices, objects, structure
from abletoolz.schema.devices import ValueKind
from abletoolz.schema.live_schemas import ValueType, live_schemas
from abletoolz.schema.versioning import Version, version_text

__all__ = [
    "NotInThisVersion",
    "NotOnThisElement",
    "SettingUnavailable",
    "is_parameter",
    "is_templated",
    "kind_of",
    "minor_version_of",
    "owner_tag",
    "path_for",
    "respelled",
    "spellings",
]

_ZERO: Version = (0, 0, 0)
_PLACEHOLDER = re.compile(r"\{index\d*\}")


_RELEASES: tuple[Version, ...] = tuple(sorted(structure.RELEASES))
_MINOR_VERSIONS: dict[Version, str] = {
    release: dict(header)["MinorVersion"] for release, header in structure.RELEASES.items()
}
# Every element tag the census measured settings on.
_CENSUS_OWNERS: frozenset[str] = frozenset(tag.value for tag in devices.SETTINGS_OF).union(*objects.OWNERS.values())
# Each device settings class's element tag.
_DEVICE_TAGS: dict[type[enum.StrEnum], str] = {cls: tag.value for tag, cls in devices.SETTINGS_OF.items()}


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


class _Record(TypedDict):
    """What ``data/settings.json`` holds about one member; ``scripts/gen_vocab.py`` documents each field."""

    kind: NotRequired[str]
    parameter: NotRequired[bool]
    older: NotRequired[list[tuple[list[int], str]]]
    undeclared: NotRequired[list[str]]
    seen_on: NotRequired[list[str]]
    census: str


@functools.cache
def _records() -> dict[str, dict[str, dict[str, _Record]]]:
    """``data/settings.json``: module (``devices``, ``objects``) -> settings class -> member -> record."""
    with (Path(__file__).with_name("data") / "settings.json").open(encoding="utf-8") as stream:
        data = json.load(stream)
    # gen_vocab writes exactly this shape; test_settings reads every member back.
    return {module: cast("dict[str, dict[str, _Record]]", part["settings"]) for module, part in data.items()}


def _record(setting: enum.StrEnum) -> _Record:
    settings_class = type(setting)
    members = _records().get(settings_class.__module__.rpartition(".")[2], {}).get(settings_class.__name__)
    if members is None:
        raise TypeError(f"{settings_class.__name__} is not a settings enum")
    return members[setting.name]


def _label(setting: enum.StrEnum) -> str:
    return f"{type(setting).__name__}.{setting.name}"


def kind_of(setting: enum.StrEnum) -> ValueKind:
    """What ``setting``'s value reads as.

    Live's newest schema that declares the setting says, from the class of
    its element. The census answers where no schema can, and where Live
    changed the type and the sets it saw hold both (``Saturator.OVERSAMPLING``
    was an enum before it was a switch): those read as text.
    """
    kind = _record(setting).get("kind")
    return ValueKind(kind if kind is not None else _schema_value(setting).kind)


def is_parameter(setting: enum.StrEnum) -> bool:
    """Whether ``setting`` names an automatable parameter (its value sits in a ``Manual`` child).

    Live's newest schema that declares the setting says, and the census where none can.
    """
    parameter = _record(setting).get("parameter")
    return parameter if parameter is not None else _schema_value(setting).timeable


def _schema_value(setting: enum.StrEnum) -> ValueType:
    found = _schema_values(type(setting), setting.name)
    if found is None:
        raise LookupError(f"neither Live's schemas nor the census say what {_label(setting)} holds")
    return found


@functools.cache
def _schema_values(settings_class: type[enum.StrEnum], name: str) -> ValueType | None:
    device = _DEVICE_TAGS.get(settings_class)
    owners = {device} if device is not None else objects.OWNERS[settings_class]
    inside = objects.INSIDE.get(settings_class)
    prefix = f"{inside}/" if inside else ""
    return live_schemas().newest_value_type(
        {cls for owner in owners for cls in _schema_classes(owner)},
        [prefix + spelled for _, spelled in spellings(settings_class[name])],
    )


def is_templated(setting: enum.StrEnum) -> bool:
    """Whether ``setting``'s path holds ``{index}`` placeholders to fill before use."""
    return _PLACEHOLDER.search(setting.value) is not None


def spellings(setting: enum.StrEnum) -> tuple[tuple[Version, str], ...]:
    """Every path Live wrote ``setting`` at, newest first, each from the first version that writes it."""
    return _spellings(type(setting), setting.name)


# Keyed by class and name: a StrEnum member equals its string, so two devices' members
# with one path would share a cache entry.
@functools.cache
def _spellings(settings_class: type[enum.StrEnum], name: str) -> tuple[tuple[Version, str], ...]:
    setting = settings_class[name]
    older = _record(setting).get("older")
    if older is None:
        return ((_ZERO, str(setting.value)),)
    return tuple(((since[0], since[1], since[2]), spelled) for since, spelled in older)


def respelled() -> dict[type[enum.StrEnum], dict[enum.StrEnum, tuple[tuple[Version, str], ...]]]:
    """Every setting Live wrote at more than one path, by settings class, with its :func:`spellings`."""
    found: dict[type[enum.StrEnum], dict[enum.StrEnum, tuple[tuple[Version, str], ...]]] = {}
    for module in (devices, objects):
        for class_name, members in _records()[module.__name__.rpartition(".")[2]].items():
            settings_class: type[enum.StrEnum] = getattr(module, class_name)
            found[settings_class] = {
                settings_class[name]: _spellings(settings_class, name)
                for name, record in members.items()
                if "older" in record
            }
    return found


def owner_tag(tag: str) -> str:
    """An element tag as the census counts it: ``MasterTrack`` is the main track's older name."""
    return objects.OWNER_SPELLINGS.get(tag, tag)


def _seen_on(setting: enum.StrEnum) -> frozenset[str] | None:
    """The owners of a setting that is not on every element its class covers; None when it is."""
    owners = _record(setting).get("seen_on")
    return None if owners is None else frozenset(owners)


def _undeclared(setting: enum.StrEnum) -> frozenset[str]:
    """The ``MinorVersion``s a real set held ``setting`` in although Live's schema of it does not declare it."""
    return frozenset(_record(setting).get("undeclared", ()))


def minor_version_of(version: Version) -> str | None:
    """The ``MinorVersion`` sets saved by ``version`` carry, where the corpus pins it.

    A release the corpus holds says so itself; one between two releases that
    share a ``MinorVersion`` carries the same. Anything else is None.
    """
    exact = _MINOR_VERSIONS.get(version)
    if exact is not None:
        return exact
    position = bisect.bisect_left(_RELEASES, version)
    if 0 < position < len(_RELEASES):
        before, after = _MINOR_VERSIONS[_RELEASES[position - 1]], _MINOR_VERSIONS[_RELEASES[position]]
        if before == after:
            return before
    return None


def _schema_classes(owner: str) -> set[str]:
    """The schema classes an ``owner`` tag can be: ``MainTrack`` was ``MasterTrack`` before Live 12."""
    census = owner_tag(owner)
    return {owner, census, *(old for old, new in objects.OWNER_SPELLINGS.items() if new == census)}


def _declared(setting: enum.StrEnum, minor_version: str, owner: str) -> bool:
    """Whether a set written in ``minor_version`` can hold ``setting`` in an ``owner`` element.

    Yes when Live's schema declares any of its spellings there, when a real set
    of that ``MinorVersion`` held it anyway, or when the schema cannot tell.
    """
    if minor_version in _undeclared(setting):
        return True
    inside = objects.INSIDE.get(type(setting))
    prefix = f"{inside}/" if inside else ""
    schemas = live_schemas()
    answers = {
        schemas.declares(minor_version, cls, prefix + spelled)
        for cls in _schema_classes(owner)
        for _, spelled in spellings(setting)
    }
    return True in answers or False not in answers


def path_for(setting: enum.StrEnum, version: Version, owner: str) -> str:
    """Where a set saved by ``version`` keeps ``setting`` inside an ``owner`` element.

    ``owner`` is the element's tag (``AudioTrack``, ``Eq8``, ``MidiClip``).
    Raises :class:`NotOnThisElement` when the census saw that element type
    many times and never with this setting, and :class:`NotInThisVersion`
    when Live's schema of the set's ``MinorVersion`` does not declare it (the
    module docstring says when the lookup goes ahead regardless).
    Placeholders are left for the caller to fill.
    """
    census_owner = owner_tag(owner)
    owners = _seen_on(setting)
    if owners is not None and census_owner not in owners and census_owner in _CENSUS_OWNERS:
        raise NotOnThisElement(f"{_label(setting)} is not on a {census_owner}; it is on {', '.join(sorted(owners))}")
    minor_version = minor_version_of(version)
    if minor_version is not None and not _declared(setting, minor_version, owner):
        raise NotInThisVersion(
            f"{_label(setting)} is not in Live's schema {minor_version}, "
            f"which a {census_owner} saved by Live {version_text(version)} is written in"
        )
    for since, spelled in spellings(setting):
        if version >= since:
            return spelled
    return spellings(setting)[-1][1]
