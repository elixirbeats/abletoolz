"""The plugin device, as a set stores it.

A set stores a hosted plugin as a ``PluginDevice`` wrapping a ``PluginDesc``
that holds exactly one info element -- ``VstPluginInfo``, ``Vst3PluginInfo``
or ``AuPluginInfo``. Everything a program can know about the plugin, and
everything it may change without changing the format, is in that element:
which plugin it is, which file it points at, and the preset it saved. Each
format spells those three differently and keeps different extras beside them.

This module is the one place that reads an info element of any kind into the
same neutral description -- :class:`Identity`, :class:`Reference`,
:class:`Preset` -- and writes a change back into the element it came from, at
the indentation Live wrote it with, touching nothing else. One container
adapter per format does the spelling; nothing outside this module names a
child of an info element.

Two kinds of write, and never a third. Within a format, a change edits its
field on the existing element and touches nothing else. Across formats, the
element is rebuilt from a description in the child order measured for the
target, in place, so it keeps its position and indentation and every child
the two formats share. Only bytes are written either way; what the bytes and
the identity should be is a recipe's decision, made in
:mod:`abletoolz.plugins.container.format_translation`. See ADR 0001.

:class:`PluginDevices` is every plugin device of one document in document
order. The position in that order is the device's handle across a plan and
the apply that follows it, so it has to be the same enumeration everywhere.
It also holds what a single element cannot know: which ``PluginDevice`` owns
an info element, and which automation targets the rest of the document points
at, which is what :class:`Controls` is read from.
"""

from __future__ import annotations

import dataclasses
import enum
import functools
import pathlib
import re
from collections.abc import Callable, Iterator
from xml.etree import ElementTree as ET

from abletoolz.formats import hex_text, path_data
from abletoolz.formats.path_data import SetOperatingSystem
from abletoolz.schema import Attr, Tag, path
from abletoolz.schema.versioning import MIN_SUPPORTED, Version, parse_creator

type UidFields = tuple[int, int, int, int]
type AuIdentifier = tuple[int, int, int]


class PluginKind(enum.StrEnum):
    """A plugin format, as a set stores it."""

    VST = "vst"
    VST3 = "vst3"
    AU = "au"


# The info element a PluginDesc holds for each format.
INFO_TAGS: dict[PluginKind, str] = {
    PluginKind.VST: Tag.VST_PLUGIN_INFO,
    PluginKind.VST3: Tag.VST3_PLUGIN_INFO,
    PluginKind.AU: Tag.AU_PLUGIN_INFO,
}
_KIND_BY_TAG: dict[str, PluginKind] = {tag: kind for kind, tag in INFO_TAGS.items()}
_PRESET_TAGS: dict[PluginKind, str] = {
    PluginKind.VST: Tag.VST_PRESET,
    PluginKind.VST3: Tag.VST3_PRESET,
    PluginKind.AU: Tag.AU_PRESET,
}
# Where each format keeps the preset bytes.
_STATE_TAGS: dict[PluginKind, str] = {
    PluginKind.VST: Tag.BUFFER,
    PluginKind.VST3: Tag.PROCESSOR_STATE,
    PluginKind.AU: Tag.BUFFER,
}
# Where each format keeps the plugin's display name.
_NAME_TAGS: dict[PluginKind, str] = {
    PluginKind.VST: Tag.PLUG_NAME,
    PluginKind.VST3: Tag.NAME,
    PluginKind.AU: Tag.NAME,
}
# Apple's component types, as the uint32 an AuPluginInfo stores them as.
_AU_INSTRUMENT = int.from_bytes(b"aumu", "big")
_AU_EFFECTS = frozenset(int.from_bytes(code, "big") for code in (b"aufx", b"aumf"))
# The device element that owns a PluginDesc, per format.
_OWNER_TAGS = frozenset({Tag.PLUGIN_DEVICE, Tag.AU_PLUGIN_DEVICE})
_UNASSIGNED_PARAMETER = -1
# Every element a device chain can hang off. MainTrack is Live 12's MasterTrack.
_TRACK_TAGS = frozenset(
    {Tag.AUDIO_TRACK, Tag.MIDI_TRACK, Tag.GROUP_TRACK, Tag.RETURN_TRACK, Tag.MASTER_TRACK, Tag.MAIN_TRACK}
)


def is_info(element: ET.Element) -> bool:
    """Whether ``element`` is one of the three info elements."""
    return element.tag in _KIND_BY_TAG


def kind_of(info: ET.Element) -> PluginKind:
    """The format of an info element, by its tag."""
    try:
        return _KIND_BY_TAG[info.tag]
    except KeyError:
        raise ValueError(f"{info.tag} is not a plugin info element") from None


# -- the description --------------------------------------------------------


class ReferenceForm(enum.Enum):
    """How a set spells the file a plugin device points at."""

    PATH = "path"  # one Path element holding the whole path: Live 11 on, and a VST3 with a stored path
    DIR = "dir"  # a Dir blob holding the folder, and a FileName beside it: before Live 11
    NONE = "none"  # no file stored at all: a VST3 known by display name, every AU


@dataclasses.dataclass(frozen=True, slots=True)
class Reference:
    """The file a plugin device points at, as far as the set says.

    ``stub`` is a device whose set stores a bare file name and no folder, which
    no version of Live writes but a third-party set generator does; the name is
    all it has. ``os`` is the OS whose form a ``Dir`` blob is in, and ``UNSET``
    for every other form.
    """

    form: ReferenceForm
    path: pathlib.Path | None
    file_name: str | None
    stub: bool
    os: SetOperatingSystem


@dataclasses.dataclass(frozen=True, slots=True)
class Identity:
    """Which plugin a device is, in every format's terms at once.

    ``is_instrument`` is None when the set does not say, which a stub device
    never does. The id fields are each format's own and None for the others.
    The three counts are what a VST2 host recorded about the plugin's layout.
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
class Preset:
    """The patch a plugin device saved.

    ``state`` is empty for a device saved without a patch. ``controller`` is the
    second half a VST3 preset carries and None for the other formats. The
    rest is a VST2 preset's own record of the plugin that wrote it, which is
    not the host's record in :class:`Identity`: a device Live saved while the
    plugin was missing carries 0 parameters and 0 programs in the info element
    and the preset's real counts here (measured on a 10.1.18 set, Sonalksis
    MaxLimit: 0/0 against 9/8).
    """

    state: bytes
    controller: bytes | None
    type: int | None
    unique_id: int | None
    parameter_count: int | None
    program_count: int | None
    program_number: int | None
    has_parameter_settings: bool


@dataclasses.dataclass(frozen=True, slots=True)
class Controls:
    """The controls a plugin device has in use, read off its ``ParameterList``.

    These are the facts a format change could orphan: a parameter Live bound to
    a plugin parameter index, a MIDI mapping, and an automation or modulation
    target the rest of the set points at. ``owner_known`` is False when the
    info element sits in no ``PluginDevice``, which is a document nothing here
    can say anything about. ``malformed`` counts parameters whose id cannot be
    read, which every policy treats as configured.
    """

    owner_known: bool
    configured: tuple[int, ...]
    malformed: int
    mapped: bool
    targets: frozenset[str]
    referenced: frozenset[str]

    @property
    def in_use(self) -> bool:
        """Whether anything binds to this device's parameters."""
        return bool(self.configured or self.malformed or self.mapped or self.referenced)


# -- reading ----------------------------------------------------------------


def _value(parent: ET.Element, tag: str) -> str | None:
    element = parent.find(tag)
    return None if element is None else element.get(Attr.VALUE)


def _int(parent: ET.Element, tag: str) -> int | None:
    value = _value(parent, tag)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _split_path(value: str) -> tuple[pathlib.Path | None, str | None, bool]:
    """A stored path as (path, file name, stub): a bare name is a stub with no path."""
    if not value:
        return None, None, False
    if "/" not in value and "\\" not in value:
        return None, value, True
    separator = path_data.path_separator_type(value)
    return pathlib.Path(value), value.split(separator)[-1], False


def _uid_fields(info: ET.Element) -> UidFields | None:
    uid = info.find(Tag.UID)
    if uid is None:
        return None
    fields = [_int(uid, f"Fields.{index}") for index in range(4)]
    if any(field is None for field in fields):
        return None
    first, second, third, fourth = fields
    assert first is not None and second is not None and third is not None and fourth is not None
    return first, second, third, fourth


def _read_vst2_identity(info: ET.Element) -> Identity:
    category = _value(info, Tag.CATEGORY)
    return Identity(
        PluginKind.VST,
        name=_value(info, Tag.PLUG_NAME),
        is_instrument=None if category is None else category == "2",
        unique_id=_int(info, Tag.UNIQUE_ID),
        parameter_count=_int(info, Tag.NUMBER_OF_PARAMETERS),
        program_count=_int(info, Tag.NUMBER_OF_PROGRAMS),
        flags=_int(info, Tag.FLAGS),
    )


def _read_vst3_identity(info: ET.Element) -> Identity:
    device_type = _value(info, Tag.DEVICE_TYPE)
    return Identity(
        PluginKind.VST3,
        name=_value(info, Tag.NAME),
        is_instrument=None if device_type is None else device_type == "1",
        uid_fields=_uid_fields(info),
    )


def _read_au_identity(info: ET.Element) -> Identity:
    component_type = _int(info, Tag.COMPONENT_TYPE)
    subtype = _int(info, Tag.COMPONENT_SUB_TYPE)
    manufacturer = _int(info, Tag.COMPONENT_MANUFACTURER)
    identifier = None
    if component_type is not None and subtype is not None and manufacturer is not None:
        identifier = (component_type, subtype, manufacturer)
    is_instrument = None
    if component_type == _AU_INSTRUMENT:
        is_instrument = True
    elif component_type in _AU_EFFECTS:
        is_instrument = False
    return Identity(
        PluginKind.AU,
        name=_value(info, Tag.NAME),
        is_instrument=is_instrument,
        au=identifier,
        manufacturer=_value(info, Tag.MANUFACTURER),
    )


def _read_vst2_reference(info: ET.Element) -> Reference:
    directory = info.find(Tag.DIR)
    if directory is not None:
        return _read_dir_reference(info, directory)
    direct = info.find(Tag.PATH)
    if direct is None:
        return Reference(ReferenceForm.NONE, None, None, False, SetOperatingSystem.UNSET)
    stored, file_name, stub = _split_path(direct.get(Attr.VALUE, ""))
    return Reference(ReferenceForm.PATH, stored, file_name, stub, SetOperatingSystem.UNSET)


def _read_dir_reference(info: ET.Element, directory: ET.Element) -> Reference:
    """The pre-11 form: the folder as a path blob, the file's name beside it."""
    file_name = _value(info, Tag.FILE_NAME)
    data = directory.find(Tag.DATA)
    blob = path_data.read(data.text) if data is not None and data.text else None
    if blob is None:
        return Reference(ReferenceForm.DIR, None, file_name, False, SetOperatingSystem.UNSET)
    folder = blob.path
    if folder is None:
        return Reference(ReferenceForm.DIR, None, file_name, False, blob.os)
    if "/" not in folder and "\\" not in folder:
        return Reference(ReferenceForm.DIR, None, file_name, True, blob.os)
    if file_name is None:
        return Reference(ReferenceForm.DIR, None, None, False, blob.os)
    separator = path_data.path_separator_type(folder)
    joined = folder + file_name if folder.endswith(separator) else folder + separator + file_name
    return Reference(ReferenceForm.DIR, pathlib.Path(joined), file_name, False, blob.os)


def _read_vst3_reference(info: ET.Element) -> Reference:
    direct = info.find(Tag.PATH)
    if direct is None:
        return Reference(ReferenceForm.NONE, None, None, False, SetOperatingSystem.UNSET)
    stored, file_name, stub = _split_path(direct.get(Attr.VALUE, ""))
    return Reference(ReferenceForm.PATH, stored, file_name, stub, SetOperatingSystem.UNSET)


def _no_reference(_info: ET.Element) -> Reference:
    return Reference(ReferenceForm.NONE, None, None, False, SetOperatingSystem.UNSET)


_IDENTITY_READERS = {
    PluginKind.VST: _read_vst2_identity,
    PluginKind.VST3: _read_vst3_identity,
    PluginKind.AU: _read_au_identity,
}
_REFERENCE_READERS = {
    PluginKind.VST: _read_vst2_reference,
    PluginKind.VST3: _read_vst3_reference,
    PluginKind.AU: _no_reference,
}


# -- hex blobs --------------------------------------------------------------


def _inner_depth(parent: ET.Element) -> int:
    """The depth hex lines sit at inside a child of ``parent``.

    Live indents each child of ``parent`` with the whitespace in ``parent.text``
    and the hex lines one tab deeper. A document with no indentation at all
    gets no indentation here either.
    """
    text = parent.text
    if text is None or text.strip():
        return 0
    return text.count("\t") + 1


def _set_hex(element: ET.Element, parent: ET.Element, payload: bytes) -> None:
    """Write ``payload`` as ``element``'s hex text at the depth it was read from.

    An element holding bytes already says what its depth is; an empty one is
    written at the depth its position in the tree gives it. No bytes at all is
    the empty element Live writes for a device saved without a patch.
    """
    if not payload:
        element.text = None
        return
    text = element.text
    depth = hex_text.depth(text) if text and text.strip() else _inner_depth(parent)
    element.text = hex_text.encode(payload, depth)


# -- the floor --------------------------------------------------------------
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
_SCHEMA = re.compile(r"(\d+)\.(\d+)_(\d+)")
_VST3_SCHEMA_FLOOR = (10, 0, 377)


def set_supports(kind: PluginKind, version: Version, *, minor_version: str | None = None) -> bool:
    """Whether a set saved by ``version`` may hold ``kind``'s info element.

    Identity and state are beside the point when the answer is no: the container
    is invalid for the document, and Live rejects the whole file rather than the
    one device. ``minor_version`` is the document's own schema, which an older
    document keeps when a newer Live re-saves it, and it decides over the Creator.
    """
    if version < _FORMAT_FLOORS[kind]:
        return False
    if kind is PluginKind.VST3 and minor_version is not None:
        schema = _SCHEMA.fullmatch(minor_version)
        if schema is None:
            return False
        return tuple(int(part) for part in schema.groups()) >= _VST3_SCHEMA_FLOOR
    return True


# -- rebuilding as another format -------------------------------------------
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
    uid: ET.Element = ET.Element(Tag.UID)
    uid.text = None if indent is None else indent + "\t"
    for index, value in enumerate(fields):
        field = ET.SubElement(uid, f"Fields.{index}", {Attr.VALUE: str(value)})
        field.tail = uid.text
    uid[-1].tail = indent
    return uid


# Everything a Vst3PluginInfo holds, in the order Live writes it. What a VST2
# device carries beyond this -- Path, PlugName, UniqueId, Inputs, Outputs,
# NumberOfParameters, NumberOfPrograms, Flags, Version, VstVersion,
# IsShellClient, Category, plus the Dir/FileName/LastPresetFolder of older sets
# -- has no VST3 counterpart and goes. Verified against the Vst3PluginInfo
# elements in the Live 10.1.3 and 11.3.42 fixtures, which are this list minus
# whatever that version of Live did not write yet.
_VST3_INFO_ORDER = (
    Tag.WIN_POS_X,
    Tag.WIN_POS_Y,
    Tag.NUM_AUDIO_INPUTS,
    Tag.NUM_AUDIO_OUTPUTS,
    Tag.IS_PLACEHOLDER_DEVICE,
    Tag.PRESET,
    Tag.NAME,
    Tag.UID,
    Tag.DEVICE_TYPE,
)

# Same for the preset. The 13 tags up to ParametersListWrapperLomId are the ones
# both formats share untouched; Type, ProgramCount, ParameterCount,
# ProgramNumber, PluginVersion, UniqueId and ByteOrder are VST2-only and go.
_VST3_PRESET_ORDER = (
    Tag.OVERWRITE_PROTECTION_NUMBER,
    Tag.MPE_ENABLED,
    Tag.MPE_SETTINGS,
    Tag.PARAMETER_SETTINGS,
    Tag.IS_ON,
    Tag.POWER_MACRO_CONTROL_INDEX,
    Tag.POWER_MACRO_MAPPING_RANGE,
    Tag.IS_FOLDED,
    Tag.STORED_ALL_PARAMETERS,
    Tag.DEVICE_LOM_ID,
    Tag.DEVICE_VIEW_LOM_ID,
    Tag.IS_ON_LOM_ID,
    Tag.PARAMETERS_LIST_WRAPPER_LOM_ID,
    Tag.UID,
    Tag.DEVICE_TYPE,
    Tag.PROCESSOR_STATE,
    Tag.CONTROLLER_STATE,
    Tag.NAME,
    Tag.PRESET_REF,
)


def _write_vst3(device: PluginDevice, identity: Identity, preset: Preset) -> None:
    """Rebuild ``device``'s element as the Vst3PluginInfo Live writes."""
    if identity.name is None or identity.uid_fields is None or identity.is_instrument is None:
        raise ValueError("a VST3 device needs a name, a class id and an instrument-or-effect answer")
    info = device.info
    device_type = "1" if identity.is_instrument else "2"
    info_closing = info[-1].tail
    preset_element = device._require_preset()
    preset_closing = preset_element[-1].tail
    preset_element.tag = Tag.VST3_PRESET
    state = device._require(_STATE_TAGS[device.kind], preset_element)
    state.tag = Tag.PROCESSOR_STATE
    _set_hex(state, preset_element, preset.state)
    preset_element.append(_uid_element(identity.uid_fields, _inner_indent(preset_element)))
    preset_element.append(ET.Element(Tag.DEVICE_TYPE, {Attr.VALUE: device_type}))
    controller: ET.Element = ET.Element(Tag.CONTROLLER_STATE)
    _set_hex(controller, preset_element, preset.controller or b"")
    preset_element.append(controller)
    _rebuild(preset_element, _VST3_PRESET_ORDER, preset_closing)

    info.append(ET.Element(Tag.NAME, {Attr.VALUE: identity.name}))
    info.append(_uid_element(identity.uid_fields, _inner_indent(info)))
    info.append(ET.Element(Tag.DEVICE_TYPE, {Attr.VALUE: device_type}))
    _rebuild(info, _VST3_INFO_ORDER, info_closing)
    info.tag = Tag.VST3_PLUGIN_INFO


# One per format a device can be rebuilt as. A format missing here is a writer
# nobody has measured yet, which is a fact to report, not a pair to implement.
_WRITERS: dict[PluginKind, Callable[[PluginDevice, Identity, Preset], None]] = {
    PluginKind.VST3: _write_vst3,
}


def has_writer(kind: PluginKind) -> bool:
    """Whether a device can be rebuilt as ``kind`` at all."""
    return kind in _WRITERS


# -- one device -------------------------------------------------------------


class PluginDevice:
    """One plugin device, read from and written to its info element in place.

    Every property reads the element afresh, so what a caller sees is what the
    element holds now, including its own writes. ``ordinal`` and ``document``
    are set when the device came from :class:`PluginDevices`; the facts that
    need the rest of the document -- the owner and the controls -- are only
    readable then.
    """

    __slots__ = ("document", "info", "kind", "ordinal")

    def __init__(self, info: ET.Element, *, ordinal: int | None = None, document: PluginDevices | None = None) -> None:
        self.kind = kind_of(info)
        self.info = info
        self.ordinal = ordinal
        self.document = document

    @property
    def identity(self) -> Identity:
        return _IDENTITY_READERS[self.kind](self.info)

    @property
    def reference(self) -> Reference:
        return _REFERENCE_READERS[self.kind](self.info)

    @property
    def preset(self) -> Preset:
        preset = self._preset()
        if preset is None:
            return Preset(b"", None, None, None, None, None, None, False)
        state = preset.find(_STATE_TAGS[self.kind])
        controller = None
        if self.kind is PluginKind.VST3:
            element = preset.find(Tag.CONTROLLER_STATE)
            controller = None if element is None else hex_text.decode(element.text)
        return Preset(
            state=b"" if state is None else hex_text.decode(state.text),
            controller=controller,
            type=_int(preset, Tag.TYPE),
            unique_id=_int(preset, Tag.UNIQUE_ID),
            parameter_count=_int(preset, Tag.PARAMETER_COUNT),
            program_count=_int(preset, Tag.PROGRAM_COUNT),
            program_number=_int(preset, Tag.PROGRAM_NUMBER),
            has_parameter_settings=any(len(settings) for settings in preset.findall(Tag.PARAMETER_SETTINGS)),
        )

    @property
    def complete(self) -> bool:
        """Whether the set says enough about this device to rewrite it as another format.

        Measured over 811 sets: 54 VST2 devices in 11 of them are stubs -- Dir,
        FileName, PlugName, UniqueId and sometimes a Preset, nothing more. Every
        one of those sets is the output of a third-party set generator. Nothing
        in a stub says what the device is, so nothing here will invent it.
        """
        if self.kind is not PluginKind.VST:
            return True
        return self.info.find(Tag.CATEGORY) is not None and self.info.find(self._preset_path()) is not None

    @property
    def owner(self) -> ET.Element | None:
        """The ``PluginDevice`` holding this device, or None when nothing well-formed does."""
        parents = self._document().parents
        description = parents.get(self.info)
        if description is None or description.tag != Tag.PLUGIN_DESC:
            return None
        owner = parents.get(description)
        return owner if owner is not None and owner.tag in _OWNER_TAGS else None

    @property
    def track(self) -> ET.Element | None:
        """The track this device sits on, or None for a device outside every track."""
        return self._document().track_of(self.info)

    @property
    def controls(self) -> Controls:
        owner = self.owner
        parameters = None if owner is None else owner.find(Tag.PARAMETER_LIST)
        if parameters is None:
            return Controls(owner is not None, (), 0, False, frozenset(), frozenset())
        configured: list[int] = []
        malformed = 0
        targets: set[str] = set()
        for parameter in parameters:
            index = _int(parameter, Tag.PARAMETER_ID)
            if index is None:
                malformed += 1
            elif index != _UNASSIGNED_PARAMETER:
                configured.append(index)
            targets.update(
                element.attrib[Attr.ID]
                for element in parameter.iter()
                if element.tag in (Tag.AUTOMATION_TARGET, Tag.MODULATION_TARGET) and Attr.ID in element.attrib
            )
        return Controls(
            owner_known=True,
            configured=tuple(configured),
            malformed=malformed,
            mapped=parameters.find(f".//{Tag.KEY_MIDI}") is not None,
            targets=frozenset(targets),
            referenced=frozenset(targets) & self._document().referenced_ids,
        )

    def _document(self) -> PluginDevices:
        if self.document is None:
            raise ValueError("the owner and the controls of a device need its document; read it through PluginDevices")
        return self.document

    # -- writes, same format --

    def set_name(self, name: str) -> None:
        """Rename the plugin the device says it is."""
        self._require(_NAME_TAGS[self.kind]).set(Attr.VALUE, name)

    def set_unique_id(self, unique_id: int) -> None:
        """The host's record of a VST2 plugin's unique id. The preset keeps its own; see :meth:`set_preset_record`."""
        self._only(PluginKind.VST)
        self._require(Tag.UNIQUE_ID).set(Attr.VALUE, str(unique_id))

    def set_layout(
        self, *, parameters: int | None = None, programs: int | None = None, flags: int | None = None
    ) -> None:
        """The host's record of a VST2 plugin's layout: its counts and its flags."""
        self._only(PluginKind.VST)
        if parameters is not None:
            self._require(Tag.NUMBER_OF_PARAMETERS).set(Attr.VALUE, str(parameters))
        if programs is not None:
            self._require(Tag.NUMBER_OF_PROGRAMS).set(Attr.VALUE, str(programs))
        if flags is not None:
            self._require(Tag.FLAGS).set(Attr.VALUE, str(flags))

    def set_preset_record(
        self,
        *,
        unique_id: int | None = None,
        parameters: int | None = None,
        programs: int | None = None,
        program_number: int | None = None,
    ) -> None:
        """A VST2 preset's own record of the plugin that wrote it.

        Kept apart from :meth:`set_unique_id` and :meth:`set_layout` because the
        two records disagree in real sets (see :class:`Preset`); a policy that
        means both says so by calling both.
        """
        self._only(PluginKind.VST)
        preset = self._require_preset()
        for tag, value in (
            (Tag.UNIQUE_ID, unique_id),
            (Tag.PARAMETER_COUNT, parameters),
            (Tag.PROGRAM_COUNT, programs),
            (Tag.PROGRAM_NUMBER, program_number),
        ):
            if value is not None:
                self._require(tag, preset).set(Attr.VALUE, str(value))

    def set_reference(self, target: pathlib.Path) -> None:
        """Point the device at ``target``, in whichever form the set already spells its file.

        A ``Dir`` blob is written in the Windows form whatever form it was read
        in, as the library always has; see :mod:`abletoolz.formats.path_data`.
        """
        if self.kind is PluginKind.AU:
            raise ValueError("an Audio Unit device points at no file")
        # A Windows path handed over on another OS is one component to pathlib;
        # read it as Windows spells it, so its folder and file name come apart.
        spelled: pathlib.PurePath = pathlib.PureWindowsPath(target) if "\\" in str(target) else target
        directory = self.info.find(Tag.DIR) if self.kind is PluginKind.VST else None
        if directory is not None:
            data = self._require(Tag.DATA, directory)
            _set_hex(data, directory, path_data.encode(str(spelled.parent)))
            self._require(Tag.TYPE, directory).set(Attr.VALUE, "2")
            self._require(Tag.FILE_NAME).set(Attr.VALUE, spelled.name)
            return
        direct = self.info.find(Tag.PATH)
        if direct is None:
            raise ValueError(f"{self.info.tag} stores no file to point at")
        direct.set(Attr.VALUE, spelled.as_posix())

    def set_state(self, payload: bytes) -> None:
        """Replace the preset bytes; nothing at all is the empty element Live writes."""
        preset = self._require_preset()
        _set_hex(self._require(_STATE_TAGS[self.kind], preset), preset, payload)

    def set_controller(self, payload: bytes) -> None:
        """Replace the controller half of a VST3 preset."""
        self._only(PluginKind.VST3)
        preset = self._require_preset()
        _set_hex(self._require(Tag.CONTROLLER_STATE, preset), preset, payload)

    # -- writes, another format --

    def rewrite(self, kind: PluginKind, identity: Identity, preset: Preset) -> None:
        """Rebuild this device as ``kind`` from a description, in place.

        ``identity`` and ``preset`` say what the device is and holds afterwards,
        in the target format's terms; they are written as given. The element
        keeps its position, its indentation and every child the two formats
        share, and the device reads as the new format from here on.
        """
        writer = _WRITERS.get(kind)
        if writer is None:
            raise ValueError(f"no writer for {kind.value}: nothing has measured the {INFO_TAGS[kind]} Live writes")
        if kind is self.kind:
            raise ValueError(f"already a {INFO_TAGS[kind]}: edit its fields instead")
        writer(self, identity, preset)
        self.kind = kind

    # -- the element --

    def _preset_path(self) -> str:
        return path(Tag.PRESET, _PRESET_TAGS[self.kind])

    def _preset(self) -> ET.Element | None:
        """The device's one preset; a device that holds two is nothing Live wrote."""
        found = self.info.findall(self._preset_path())
        if len(found) > 1:
            raise ValueError(f"{self.info.tag} holds {len(found)} {_PRESET_TAGS[self.kind]} elements")
        return found[0] if found else None

    def _require_preset(self) -> ET.Element:
        preset = self._preset()
        if preset is None:
            raise ValueError(f"{self.info.tag} holds no {_PRESET_TAGS[self.kind]}")
        return preset

    def _require(self, tag: str, parent: ET.Element | None = None) -> ET.Element:
        element = (self.info if parent is None else parent).find(tag)
        if element is None:
            raise ValueError(f"{self.info.tag} holds no {tag}")
        return element

    def _only(self, kind: PluginKind) -> None:
        if self.kind is not kind:
            raise ValueError(f"not a {INFO_TAGS[kind]}: {self.info.tag}")


# -- one document -----------------------------------------------------------


class PluginDevices:
    """Every plugin device of one document, in document order.

    The order is the one thing a plan and its apply have to agree on, so it is
    read once here and nowhere else: every info element of every format, as
    ``root.iter()`` meets them.

    The parent map and the referenced ids are read once too. A structural edit
    elsewhere in the document -- an element replaced or moved -- needs a new
    instance; an edit inside an info element does not.
    """

    def __init__(self, root: ET.Element) -> None:
        self.root = root
        self._infos = [element for element in root.iter() if element.tag in _KIND_BY_TAG]

    def __len__(self) -> int:
        return len(self._infos)

    def __iter__(self) -> Iterator[PluginDevice]:
        for ordinal, info in enumerate(self._infos):
            yield PluginDevice(info, ordinal=ordinal, document=self)

    def __getitem__(self, ordinal: int) -> PluginDevice:
        return PluginDevice(self._infos[ordinal], ordinal=ordinal, document=self)

    @functools.cached_property
    def parents(self) -> dict[ET.Element, ET.Element]:
        """Every element's parent; ElementTree keeps no upward link of its own."""
        return {child: parent for parent in self.root.iter() for child in parent}

    @functools.cached_property
    def referenced_ids(self) -> frozenset[str]:
        """Every automation or modulation target id something in the document points at."""
        return frozenset(
            element.attrib[Attr.VALUE] for element in self.root.iter(Tag.POINTEE_ID) if Attr.VALUE in element.attrib
        )

    def track_of(self, element: ET.Element) -> ET.Element | None:
        """The track element holding ``element``, of any kind, or None outside every track."""
        parents = self.parents
        current: ET.Element | None = element
        while current is not None:
            if current.tag in _TRACK_TAGS:
                return current
            current = parents.get(current)
        return None

    @functools.cached_property
    def version(self) -> Version:
        """The Live that saved the document, from its Creator."""
        return parse_creator(self.root.get(Attr.CREATOR, ""))

    @property
    def minor_version(self) -> str:
        """The document's own schema, which outlives a re-save by a newer Live."""
        return self.root.get(Attr.MINOR_VERSION, "")

    def supports(self, kind: PluginKind) -> bool:
        """Whether this document may hold ``kind``'s info element at all."""
        return set_supports(kind, self.version, minor_version=self.minor_version)
