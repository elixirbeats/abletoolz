"""A plugin device's parameter slots, and the list Live fills them with by itself.

A ``PluginDevice`` holds a ``ParameterList`` of 128 slots. A slot either names
one of the plugin's parameters -- by ``ParameterId``, the VST2 index or the
VST3 id -- or holds -1 and names none. The named ones are the knobs under the
device, and what automation lanes, macros and MIDI mappings point at, through
the slot's own target ids.

For a plugin with few parameters Live fills the slots by itself, and that list
is recognisable: slot ``i`` names parameter ``i`` and shows it at position
``i``, from the first slot on. Measured 2026-10-24 over the user's sets: every
VST2 Pro-C 2, Pro-L 2, Pro-G, Tupe 3, Shimmer, Ozone 9 Exciter and Kilohearts
device that names any parameter names exactly those. A list someone arranged
with Configure, or that something points at, is not this module's to touch.

A VST3 numbers its parameters its own way, so a device crossing into a VST3
gets the list Live would make for it:
:func:`~abletoolz.plugins.identity.vst3_parameters.live_list`, slot by slot,
with each slot's range in the plugin's units as Live writes it.
"""

from __future__ import annotations

import copy
from xml.etree import ElementTree as ET

from abletoolz.elements import get_element
from abletoolz.live_set.xml_edit import number_value, renumber_pointee_ids
from abletoolz.plugins.identity.vst3_parameters import Vst3Parameter
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.schema import Attr, Tag, path

UNASSIGNED = -1
"""``ParameterId`` of a slot that names no parameter."""

UNASSIGNED_VISUAL_INDEX = 1073741823
"""``VisualIndex`` Live writes for a slot that names no parameter."""

FLOAT_SLOT = "PluginFloatParameter"
"""The one slot class a VST2 or VST3 device holds; an AU device also holds ``PluginEnumParameter`` slots."""

VST_SLOTS = 128
"""How many slots every VST2 and VST3 device holds (Live 10 to 12, measured over the library); an AU device 256."""


def _int(slot: ET.Element, tag: str) -> int:
    return int(get_element(slot, tag, attribute=Attr.VALUE))


def is_automatic(parameters: ET.Element) -> bool:
    """Whether the named slots are only Live's own list: slot ``i`` names parameter ``i``, shown at ``i``.

    A named slot without a ``VisualIndex`` is a layout no list Live made by itself is known in.
    """
    named = [slot for slot in parameters if _int(slot, Tag.PARAMETER_ID) != UNASSIGNED]
    if not named or any(slot.find(Tag.VISUAL_INDEX) is None for slot in named):
        return False
    return all(
        position == _int(slot, Tag.PARAMETER_ID) == _int(slot, Tag.VISUAL_INDEX)
        for position, slot in enumerate(parameters)
        if _int(slot, Tag.PARAMETER_ID) != UNASSIGNED
    )


def _set(slot: ET.Element, tag_path: str, value: str, *, optional: bool = False) -> None:
    element = get_element(slot, tag_path, silent_error=True)
    if element is None:
        if optional:
            return
        raise StateTransformError(f"A parameter slot has no {tag_path}, which is no slot layout Live is known to write")
    element.set(Attr.VALUE, value)


def write_live_list(parameters: ET.Element, listed: tuple[Vst3Parameter, ...]) -> None:
    """Fill the slots with ``listed`` the way Live does, and leave every other slot naming nothing.

    A slot keeps its own automation and modulation target ids, which nothing
    points at -- a device whose slots something points at never gets here.
    """
    for position, slot in enumerate(parameters):
        if position >= len(listed):
            _set(slot, Tag.PARAMETER_ID, str(UNASSIGNED))
            _set(slot, Tag.VISUAL_INDEX, str(UNASSIGNED_VISUAL_INDEX))
            continue
        parameter = listed[position]
        low, high = number_value(parameter.minimum), number_value(parameter.maximum)
        if slot.find(Tag.PARAMETER_NAME) is None:  # Live 9.0 sometimes writes none; every later Live writes it first
            name = ET.Element(Tag.PARAMETER_NAME)
            name.tail = slot.text
            slot.insert(0, name)
        _set(slot, Tag.PARAMETER_NAME, parameter.title)
        _set(slot, Tag.PARAMETER_ID, str(parameter.id))
        _set(slot, Tag.VISUAL_INDEX, str(position))
        _set(slot, path(Tag.PARAMETER_VALUE, Tag.MANUAL), number_value(parameter.default))
        _set(slot, path(Tag.PARAMETER_VALUE, Tag.MIDI_CONTROLLER_RANGE, Tag.RANGE_MIN), low)
        _set(slot, path(Tag.PARAMETER_VALUE, Tag.MIDI_CONTROLLER_RANGE, Tag.RANGE_MAX), high)
        # Live 12 writes a last user range; most Live 10 and 11 slots have none.
        _set(slot, path(Tag.LAST_USER_RANGE, Tag.RANGE_FIRST), low, optional=True)
        _set(slot, path(Tag.LAST_USER_RANGE, Tag.RANGE_LAST), high, optional=True)


def as_vst_slots(parameters: ET.Element, root: ET.Element) -> None:
    """Give an AU device leaving for a VST the slots a VST device holds: its first 128, every one a float slot.

    Each other slot becomes a copy of the list's first float slot, naming nothing, that keeps its own ``Id`` and
    automation target; its modulation target, which an enum slot has none of, takes a fresh id from the set's
    ``NextPointeeId``. The slots past 128 go, with the device's other controls the conversion forgets.
    """
    if len(parameters) > VST_SLOTS:
        parameters[VST_SLOTS - 1].tail = parameters[-1].tail
        del parameters[VST_SLOTS:]
    template = parameters.find(FLOAT_SLOT)
    for position, slot in enumerate(list(parameters)):
        if slot.tag == FLOAT_SLOT:
            continue
        if template is None:
            raise StateTransformError(f"A parameter list of {slot.tag} slots only has no float slot to model one on")
        replacement = copy.deepcopy(template)
        replacement.set(Attr.ID, slot.get(Attr.ID, ""))
        replacement.tail = slot.tail
        _set(replacement, Tag.PARAMETER_NAME, "")
        _set(replacement, Tag.PARAMETER_ID, str(UNASSIGNED))
        _set(replacement, Tag.VISUAL_INDEX, str(UNASSIGNED_VISUAL_INDEX))
        automation = get_element(slot, path(Tag.PARAMETER_VALUE, Tag.AUTOMATION_TARGET))
        get_element(replacement, path(Tag.PARAMETER_VALUE, Tag.AUTOMATION_TARGET)).set(
            Attr.ID, automation.get(Attr.ID, "")
        )
        renumber_pointee_ids(get_element(replacement, path(Tag.PARAMETER_VALUE, Tag.MODULATION_TARGET)), root)
        parameters[position] = replacement
