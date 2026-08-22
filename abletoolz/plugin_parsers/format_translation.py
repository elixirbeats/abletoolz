"""Translate a plugin device from one plugin format to another, in place.

A set stores a device as a ``PluginDesc`` wrapping one format-specific info
element -- ``VstPluginInfo``, ``Vst3PluginInfo``, ``AuPluginInfo``. Measured on
Live 12.4.5: the ``PluginDevice`` wrapper around ``PluginDesc`` is byte for byte
the same whichever format is inside, so a translation is entirely local to the
info element.

Two pieces of knowledge make that possible, and they are kept apart:

* per format, how to read an info element into a neutral :class:`PluginIdentity`
  (what plugin, is it an instrument) -- ``_READERS``;
* per ordered format pair, how to rewrite one info element into the other --
  ``_TRANSLATIONS``.

Only ``(vst, vst3)`` is implemented today, because that is the pair whose output
has been loaded in Live and listened to. Another pair is another entry in
``_TRANSLATIONS`` plus a reader; nothing in the dispatch names a format.

What the target format cannot infer from the source is supplied by a
:class:`TranslationTarget`: VST3 identifies a plugin by a class id that appears
nowhere in a VST2 device, so it has to come from somewhere else. The
:data:`KNOWN_TRANSLATIONS` seed table is one place; every other one is
:mod:`abletoolz.plugin_parsers.uid_sources`.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol
from xml.etree import ElementTree as ET

import pydantic

from abletoolz import decode_encode
from abletoolz.misc import get_element
from abletoolz.plugin_parsers.base import PluginKind
from abletoolz.plugin_parsers.state import (
    NO_CONTROLLER_STATE,
    ControllerState,
    CustomState,
    StatePolicy,
    StateTransform,
    parse_controller_state,
    parse_state,
    state_bytes,
)
from abletoolz.versioning import MIN_SUPPORTED, Version

if TYPE_CHECKING:
    from abletoolz.live_set.document import AbletonSet
type UidFields = tuple[int, int, int, int]


class IncompleteDevice(ValueError):
    """A device the set describes too thinly to rewrite. See :func:`is_translatable`."""


# The element a PluginDesc holds for each format.
INFO_TAGS: dict[PluginKind, str] = {
    PluginKind.VST: "VstPluginInfo",
    PluginKind.VST3: "Vst3PluginInfo",
    PluginKind.AU: "AuPluginInfo",
}


@dataclasses.dataclass(frozen=True)
class TranslationTarget:
    """What one plugin becomes in another format.

    ``uid_fields`` is the target format's identity: for VST3 the four signed
    int32 slices of the Audio Module Class id. ``name`` is the display name the
    target format knows the plugin by, which is often not the VST2 file name
    ("FabFilter Pro-Q 3" becomes "Pro-Q 3").

    ``controller_state`` is the second half of a VST3 preset, and saying nothing
    about it means the empty element Live writes for a plugin that keeps none.
    That is right for the plugins measured to write none and wrong for the rest,
    which is why it is declared per target rather than assumed.
    """

    to_format: PluginKind
    name: str
    uid_fields: UidFields
    state_transform: StatePolicy = StateTransform.VERBATIM
    controller_state: ControllerState = NO_CONTROLLER_STATE


@dataclasses.dataclass(frozen=True)
class NamedTarget:
    """A target known by display name only, whose class id is still to be found.

    What a config entry means when it gives a ``name`` and no ``uid``: the user
    knows which VST3 they want, and the class id is a machine fact
    :mod:`abletoolz.plugin_parsers.uid_sources` can look up. Kept apart from
    :class:`TranslationTarget` so nothing can reach ``translate_device`` without
    a class id in hand.
    """

    to_format: PluginKind
    name: str
    state_transform: StatePolicy = StateTransform.VERBATIM
    controller_state: ControllerState = NO_CONTROLLER_STATE


# What a mapping table may hold: an identity, or a name still to be resolved.
type ConfiguredTarget = TranslationTarget | NamedTarget


class UidResolver(Protocol):
    """Anything that can turn a VST3 display name into its four Uid fields.

    A protocol rather than an import so that class id sourcing can depend on
    this module and not the other way round.
    """

    def resolve(self, name: str) -> UidFields | None: ...


def resolve_target(target: ConfiguredTarget, uid_lookup: UidResolver | None) -> TranslationTarget | None:
    """Give a configured target its class id, or answer None when none is known."""
    if isinstance(target, TranslationTarget):
        return target
    fields = None if uid_lookup is None else uid_lookup.resolve(target.name)
    if fields is None:
        return None
    return TranslationTarget(target.to_format, target.name, fields, target.state_transform, target.controller_state)


@dataclasses.dataclass(frozen=True)
class PluginIdentity:
    """What a set says about one plugin device, in format-neutral terms.

    ``is_instrument`` is None when the set does not say -- see
    :func:`is_translatable`. A device is still perfectly identifiable then; it
    just cannot be rewritten as another format.
    """

    format: PluginKind
    name: str
    is_instrument: bool | None


def _vst3(
    name: str,
    uid_fields: UidFields,
    state_transform: StatePolicy = StateTransform.VERBATIM,
) -> TranslationTarget:
    """Seed-table shorthand for a VST3 target."""
    return TranslationTarget(PluginKind.VST3, name, uid_fields, state_transform)


# Keyed by the VST2 PlugName a set stores. Measured 2026-08-10 against installed
# plugins and sets that already carry the VST3; every entry but the last was
# translated, loaded in Live 12.4.5 and confirmed by ear to keep its patch.
# "FabFilter Pro-Q 3" is measured only: its class id is cross-checked by
# test_format_translation against a VST3 instance in the 10.1.3 fixture, but no
# translated device has been listened to.
KNOWN_TRANSLATIONS: dict[str, TranslationTarget] = {
    "Serum_x64": _vst3("Serum", (1448301656, 1718835315, 1701999981, 0)),
    "Ghz Tupe 3": _vst3("Ghz Tupe 3", (1448301652, 1345542247, 1752834164, 1970300192)),
    "Serato Sample": _vst3("Serato Sample", (1448301651, 1836084339, 1701994868, 1864397665)),
    "Prophet V3": _vst3("Prophet V3", (1098019957, 1096173907, 1347571507, 1349676899)),
    "FabFilter Timeless 3": _vst3("Timeless 3", (-756127758, -984465363, -1876879981, 452832875)),
    "FabFilter Pro-Q 3": _vst3("Pro-Q 3", (1925503857, 2051884442, -1182903948, 1568977821)),
    "kHs Distortion": _vst3("kHs Distortion", (-209286662, 4410480, 16003238, 13171824), StateTransform.KILOHEARTS),
    "kHs Filter": _vst3("kHs Filter", (2034193867, 5261734, 12553643, 11857824), StateTransform.KILOHEARTS),
}


# -- config -----------------------------------------------------------------


# Measured across the Kilohearts range: every kHs VST3 wraps the payload its
# VST2 stores raw, so a kHs entry that says nothing about state means this.
# A VST2 device from that vendor is named "kHs <effect>" in every set seen.
KILOHEARTS_PREFIX = "kHs "


class TargetConfig(pydantic.BaseModel):
    """One ``plugin_translation.targets`` entry from config.yaml.

    ``uid`` is optional: leaving it out asks for the class id to be looked up by
    ``name`` at translation time, which is the normal case on a machine that has
    the VST3 installed. ``state`` names a rung of MODEL.md's ladder --
    ``verbatim``, ``kilohearts``, or ``custom:<name>`` for a registered
    per-plugin re-encode. Left out it means verbatim, except for Kilohearts
    plugins -- see :data:`KILOHEARTS_PREFIX`.

    ``controller`` names one of the registered ``ControllerState`` shapes, and
    left out it means the empty element Live writes for a plugin that keeps
    none. That is right for the plugins measured to write none and wrong for
    the rest, which is why it is written per entry rather than guessed from the
    target's name.
    """

    model_config = pydantic.ConfigDict(extra="forbid")

    to: PluginKind = PluginKind.VST3
    name: str
    uid: tuple[int, int, int, int] | None = None
    state: StateTransform | CustomState | None = None
    controller: str | None = None

    @pydantic.field_validator("state", mode="before")
    @classmethod
    def _read_state(cls, raw: object) -> object:
        """Turn the written rung name into a policy, loudly if it names nothing."""
        return parse_state(raw) if isinstance(raw, str) else raw

    @pydantic.field_validator("controller")
    @classmethod
    def _check_controller(cls, raw: str | None) -> str | None:
        """Refuse a name nothing registered, here rather than at translation time.

        The name stays a name: a :class:`ControllerState` is a protocol and
        pydantic has no schema for one. It is looked up again in
        :meth:`target`, which is cheap and keeps the failure where the user can
        see which line caused it.
        """
        if raw is not None:
            parse_controller_state(raw)
        return raw

    def state_for(self, source_name: str) -> StatePolicy:
        """The state transform this entry asks for, or the vendor default."""
        if self.state is not None:
            return self.state
        if source_name.startswith(KILOHEARTS_PREFIX):
            return StateTransform.KILOHEARTS
        return StateTransform.VERBATIM

    def target(self, source_name: str) -> ConfiguredTarget:
        """The mapping entry, resolved if it carries a class id and named if not."""
        transform = self.state_for(source_name)
        controller = NO_CONTROLLER_STATE if self.controller is None else parse_controller_state(self.controller)
        if self.uid is None:
            return NamedTarget(self.to, self.name, transform, controller)
        return TranslationTarget(self.to, self.name, self.uid, transform, controller)


def parse_config_targets(raw: object) -> dict[str, ConfiguredTarget]:
    """Build targets from the ``plugin_translation.targets`` mapping in config.yaml."""
    targets = pydantic.TypeAdapter(dict[str, TargetConfig]).validate_python(raw)
    return {source_name: entry.target(source_name) for source_name, entry in targets.items()}


# -- identity ---------------------------------------------------------------


def _read_vst2(info: ET.Element) -> PluginIdentity:
    """VST2 keeps its file-derived name in PlugName; Category 2 means instrument.

    A stub device has no Category at all and the set says nothing about what the
    plugin is -- the name is still there, which is the half repair needs to
    report it.
    """
    category = info.find("Category")
    return PluginIdentity(
        format=PluginKind.VST,
        name=get_element(info, "PlugName", attribute="Value"),
        is_instrument=None if category is None else category.get("Value") == "2",
    )


def _read_vst3(info: ET.Element) -> PluginIdentity:
    """VST3 keeps its display name in Name; DeviceType 1 means instrument."""
    return PluginIdentity(
        format=PluginKind.VST3,
        name=get_element(info, "Name", attribute="Value"),
        is_instrument=get_element(info, "DeviceType", attribute="Value") == "1",
    )


_READERS: dict[PluginKind, Callable[[ET.Element], PluginIdentity]] = {
    PluginKind.VST: _read_vst2,
    PluginKind.VST3: _read_vst3,
}

_KIND_BY_TAG: dict[str, PluginKind] = {INFO_TAGS[kind]: kind for kind in _READERS}


def read_identity(info: ET.Element) -> PluginIdentity:
    """Read any supported plugin info element into format-neutral terms."""
    return _READERS[_KIND_BY_TAG[info.tag]](info)


# What a VST2 info element has to carry before it can become anything else: the
# Category that decides DeviceType, and the preset block that becomes the
# Vst3Preset. Measured over 811 sets: 54 devices in 11 of them carry a stub
# instead -- Dir, FileName, PlugName, UniqueId and sometimes Preset, nothing
# more. Every one of those 11 sets is the output of a third-party set generator
# (see MODEL.md); no set Live wrote carries the shape. Nothing in that stub says
# what the device is, so nothing here will invent it.
_REQUIRED_FIELDS: dict[PluginKind, tuple[str, ...]] = {
    PluginKind.VST: ("Category", "Preset/VstPreset"),
    PluginKind.VST3: (),
}


def is_translatable(info: ET.Element) -> bool:
    """Whether this device carries what rewriting it into another format needs."""
    return all(info.find(path) is not None for path in _REQUIRED_FIELDS[_KIND_BY_TAG[info.tag]])


# The oldest set each format's info element may be written into.
#
# A set's XML is schema-versioned by its root element, and Live reads a tag as a
# class only if the schema that version declared knows the name. So writing a
# Vst3PluginInfo into a set Live 9 saved does not produce a set with a VST3
# device in it -- it produces a file Live will not open at all:
#
#     ... is corrupt and cannot be loaded.
#     (Unknown class 'Vst3PluginInfo' encountered (at line 19371, column 25))
#
# Measured 2026-08-13 on Live 12.4.5b, opening a 9.0.1 set (MinorVersion
# 9.0_305) whose Pro-Q 1 devices had been translated to Pro-Q 3. The floor comes
# from the library, by document schema: MinorVersion 9.0_305 (9 sets), 9.5_326
# (10), 9.5_327 (22) and 10.0_370 (61) carry no Vst3PluginInfo between them,
# while 10.0_377 -- the schema Live 10.1 writes -- carries 129 across 23 sets.
# By Creator the same line falls between 10.0.6 and 10.1.
_FORMAT_FLOORS: dict[PluginKind, Version] = {
    PluginKind.VST: MIN_SUPPORTED,
    PluginKind.VST3: (10, 1, 0),
    PluginKind.AU: MIN_SUPPORTED,
}


def set_supports(kind: PluginKind, version: Version) -> bool:
    """Whether a set saved by ``version`` may hold ``kind``'s info element.

    Identity and state are beside the point when the answer is no: the container
    is invalid for the document, and Live rejects the whole file rather than the
    one device.
    """
    return version >= _FORMAT_FLOORS[kind]


# -- shaping ----------------------------------------------------------------
# Live pretty-prints with tabs, so an element added without matching its
# siblings makes the file stop looking like Live's own.


def _inner_indent(parent: ET.Element) -> str | None:
    """The whitespace Live writes before each child of ``parent``."""
    text = parent.text
    return text if text is not None and not text.strip() else None


def _rebuild(parent: ET.Element, order: tuple[str, ...], closing: str | None) -> None:
    """Keep only ``order``'s tags, in ``order``, indented the way Live writes them.

    ``closing`` is the indentation before the parent's own closing tag, which
    has to be read off the last child before anything is appended.

    Dropping by keep-list rather than by a list of unwanted tags is what makes
    one rule cover every Live version: a set from Live 10 carries Dir/FileName
    and no NumAudioInputs, Live 12 the other way round, and the result each time
    is exactly the element that version of Live writes for a VST3.
    """
    inner = _inner_indent(parent)
    kept = {child.tag: child for child in parent}
    children = [kept[tag] for tag in order if tag in kept]
    for child in children:
        child.tail = inner
    children[-1].tail = closing
    parent[:] = children


def _uid_element(fields: UidFields, indent: str | None) -> ET.Element:
    """Live's Uid block, indented for a parent whose children sit at ``indent``."""
    uid = ET.Element("Uid")
    uid.text = None if indent is None else indent + "\t"
    for index, value in enumerate(fields):
        field = ET.SubElement(uid, f"Fields.{index}", {"Value": str(value)})
        field.tail = uid.text
    uid[-1].tail = indent
    return uid


# -- state ------------------------------------------------------------------
# What the bytes become is :mod:`abletoolz.plugin_parsers.state`'s knowledge;
# here is only where they sit in the file.


def _state_payload(element: ET.Element) -> bytes:
    """The bytes a state element holds, or none at all for a device saved without a patch."""
    text = element.text
    if text is None or not text.strip():
        return b""
    return bytes.fromhex(decode_encode.xml_to_string(text)[0])


def _transform_state(element: ET.Element, policy: StatePolicy) -> None:
    """Rewrite a state element's hex blob in place, keeping Live's hex line width."""
    rewrite = state_bytes(policy)
    if rewrite is None:
        return
    text = element.text
    if text is None or not text.strip():
        return
    hex_string, levels = decode_encode.xml_to_string(text)
    payload = rewrite(bytes.fromhex(hex_string))
    element.text = decode_encode.string_to_xml(payload.hex().upper(), levels=levels)


def _controller_element(controller: ControllerState, source: bytes, indent: str | None) -> ET.Element:
    """The ControllerState Live writes beside every ProcessorState.

    Measured over 150 sets: there is exactly one of these per ``ProcessorState``
    -- 917 pairs out of 917 in a second sample -- and whether it holds anything
    is a fact about the plugin. It is built from what the *source* device saved
    rather than from what the processor half became, because for the products
    that populate it the two are halves of the same saved chunk.

    A device saved with no patch at all has nothing to build one out of, and
    gets the empty element that says so.
    """
    element = ET.Element("ControllerState")
    payload = controller.build(source) if source else None
    if payload:
        levels = 0 if indent is None else indent.count("\t") + 1
        element.text = decode_encode.string_to_xml(payload.hex().upper(), levels=levels)
    return element


# -- vst2 to vst3 -----------------------------------------------------------

# Everything a Vst3PluginInfo holds, in the order Live writes it. What a VST2
# device carries beyond this -- Path, PlugName, UniqueId, Inputs, Outputs,
# NumberOfParameters, NumberOfPrograms, Flags, Version, VstVersion,
# IsShellClient, Category, plus the Dir/FileName/LastPresetFolder of older sets
# -- has no VST3 counterpart and goes. Verified against the Vst3PluginInfo
# elements in the Live 10.1.3 and 11.3.42 fixtures, which are this list minus
# whatever that version of Live did not write yet.
_VST3_INFO_ORDER = (
    "WinPosX",
    "WinPosY",
    "NumAudioInputs",
    "NumAudioOutputs",
    "IsPlaceholderDevice",
    "Preset",
    "Name",
    "Uid",
    "DeviceType",
)

# Same for the preset. The 13 tags up to ParametersListWrapperLomId are the ones
# both formats share untouched; Type, ProgramCount, ParameterCount,
# ProgramNumber, PluginVersion, UniqueId and ByteOrder are VST2-only and go.
_VST3_PRESET_ORDER = (
    "OverwriteProtectionNumber",
    "MpeEnabled",
    "MpeSettings",
    "ParameterSettings",
    "IsOn",
    "PowerMacroControlIndex",
    "PowerMacroMappingRange",
    "IsFolded",
    "StoredAllParameters",
    "DeviceLomId",
    "DeviceViewLomId",
    "IsOnLomId",
    "ParametersListWrapperLomId",
    "Uid",
    "DeviceType",
    "ProcessorState",
    "ControllerState",
    "Name",
    "PresetRef",
)


def _translate_vst2_to_vst3(info: ET.Element, target: TranslationTarget) -> None:
    """Rewrite a VstPluginInfo element into the Vst3PluginInfo Live writes."""
    is_instrument = _read_vst2(info).is_instrument
    if is_instrument is None:
        raise IncompleteDevice(f"{read_identity(info).name} is a stub device: the set never says what it is")
    device_type = "1" if is_instrument else "2"
    info_closing = info[-1].tail

    preset = get_element(info, "Preset.VstPreset")
    preset_closing = preset[-1].tail
    preset.tag = "Vst3Preset"
    state = get_element(preset, "Buffer")
    state.tag = "ProcessorState"
    # Read before the rewrite: the controller half is built from what the VST2
    # device saved, not from what the processor half is about to become.
    source = _state_payload(state)
    _transform_state(state, target.state_transform)
    preset.append(_uid_element(target.uid_fields, _inner_indent(preset)))
    preset.append(ET.Element("DeviceType", {"Value": device_type}))
    preset.append(_controller_element(target.controller_state, source, _inner_indent(preset)))
    _rebuild(preset, _VST3_PRESET_ORDER, preset_closing)

    info.append(ET.Element("Name", {"Value": target.name}))
    info.append(_uid_element(target.uid_fields, _inner_indent(info)))
    info.append(ET.Element("DeviceType", {"Value": device_type}))
    _rebuild(info, _VST3_INFO_ORDER, info_closing)
    info.tag = INFO_TAGS[PluginKind.VST3]


_TRANSLATIONS: dict[tuple[PluginKind, PluginKind], Callable[[ET.Element, TranslationTarget], None]] = {
    (PluginKind.VST, PluginKind.VST3): _translate_vst2_to_vst3,
}


def translate_device(info: ET.Element, target: TranslationTarget) -> None:
    """Rewrite one plugin info element into ``target``'s format, in place."""
    _TRANSLATIONS[(_KIND_BY_TAG[info.tag], target.to_format)](info, target)


def has_translator(source: PluginKind, to: PluginKind) -> bool:
    """Whether this ordered format pair can be translated at all.

    Direction comes from the mapping entry, so an entry may name a pair nothing
    here implements -- today anything but ``(vst, vst3)``. That is a fact to
    report, not an error: the entry is the user's statement of intent, and the
    missing half is a translator nobody has written yet.
    """
    return (source, to) in _TRANSLATIONS


def device_infos(live_set: AbletonSet) -> list[tuple[ET.Element, PluginKind]]:
    """Every plugin device in a set this module can read, with its format.

    Snapshotted before anything is rewritten, on purpose: translating a device
    changes its tag, and a live iteration would meet the result again as a
    device of the target format and report it twice.
    """
    found: list[tuple[ET.Element, PluginKind]] = []
    for plugin_desc in live_set.root.iter("PluginDesc"):
        for kind in _READERS:
            found.extend((info, kind) for info in plugin_desc.iter(INFO_TAGS[kind]))
    return found
