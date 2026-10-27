"""The vocabulary of the identity axis: plugin formats, the ids each stores, and a device's identity.

Every other plugin module speaks these terms -- the container reads and writes
them, plugin maps are keyed by them, plans and recovery routes name targets in
them -- so they live here, below all of it, and depend on nothing.
"""

from __future__ import annotations

import dataclasses
import enum

type UidFields = tuple[int, int, int, int]
type AuIdentifier = tuple[int, int, int]


class PluginKind(enum.StrEnum):
    """A plugin format, as a set stores it."""

    VST = "vst"
    VST3 = "vst3"
    AU = "au"


@dataclasses.dataclass(frozen=True, slots=True)
class Identity:
    """Which plugin a device is, in every format's terms at once.

    ``is_instrument`` is None when the set does not say, which a stub device
    never does. The id fields are each format's own and None for the others.
    The three counts are what a VST2 host recorded about the plugin's layout;
    ``flags`` is a VST2's Flags or an Audio Unit's ComponentFlags.
    """

    kind: PluginKind
    name: str | None
    is_instrument: bool | None
    unique_id: int | None = None
    uid_fields: UidFields | None = None
    au: AuIdentifier | None = None
    manufacturer: str | None = None
    parameter_count: int | None = None
    program_count: int | None = None
    flags: int | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class Vst2Id:
    """The UniqueId a VST2 device stores."""

    unique_id: int


@dataclasses.dataclass(frozen=True, slots=True)
class Vst3Id:
    """The class id a VST3 device stores, as its four signed int32."""

    uid_fields: UidFields


def fourcc(code: str) -> int:
    """A FourCC as the uint32 a set stores it as."""
    return int.from_bytes(code.encode("latin1"), "big")


@dataclasses.dataclass(frozen=True, slots=True)
class AuId:
    """The type, subtype and manufacturer an AU device stores, as uint32."""

    identifier: AuIdentifier

    @classmethod
    def of(cls, component_type: str, subtype: str, manufacturer: str) -> AuId:
        """From the three FourCCs as a component's Info.plist spells them."""
        return cls((fourcc(component_type), fourcc(subtype), fourcc(manufacturer)))


type StoredId = Vst2Id | Vst3Id | AuId


@dataclasses.dataclass(frozen=True, slots=True)
class AuRegistration:
    """What macOS registers for an Audio Unit besides its codes, which a set repeats.

    ``manufacturer`` is the part of the component's registered name before the
    colon, and ``flags`` its registered component flags (2 when it declares
    itself sandbox-safe).
    """

    manufacturer: str
    flags: int


def stored_id(identity: Identity) -> StoredId | None:
    """The id a device's identity carries for its own plugin format, if it stores one."""
    if identity.kind is PluginKind.VST and identity.unique_id is not None:
        return Vst2Id(identity.unique_id)
    if identity.kind is PluginKind.VST3 and identity.uid_fields is not None:
        return Vst3Id(identity.uid_fields)
    if identity.kind is PluginKind.AU and identity.au is not None:
        return AuId(identity.au)
    return None
