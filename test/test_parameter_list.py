"""Live's own list of a plugin's parameters: recognising it, and rebuilding it for the VST3 a device becomes."""

from __future__ import annotations

import struct
from xml.etree import ElementTree as ET

import pytest
from conftest import serum_as, skeleton

from abletoolz.elements import get_element
from abletoolz.formats import hex_text
from abletoolz.plugins.container.device import PluginDevice, PluginDevices
from abletoolz.plugins.container.parameter_list import UNASSIGNED_VISUAL_INDEX
from abletoolz.plugins.container.rewritable import CONTROLS_IN_USE, LIVE_LIST_IN_USE, refusal
from abletoolz.plugins.conversion import as_format, write
from abletoolz.plugins.identity.ids import PluginKind
from abletoolz.plugins.identity.vst3_host import class_id
from abletoolz.plugins.identity.vst3_parameters import ParameterSource, Vst3Parameter, live_list, no_parameters
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.fxbk import LegacyBank
from abletoolz.plugins.state.plugin_map import PluginMap

PRO_C_2_ID = 1178808944
PRO_C_2_PARAMETERS = 46


def parameter(index: int, title: str, *flags: str, low: float = 0.0, high: float = 1.0) -> Vst3Parameter:
    return Vst3Parameter(index, title, 0, frozenset(flags), low, high, low)


def test_live_lists_no_more_parameters_than_a_device_has_slots() -> None:
    parameters = tuple(parameter(index, f"p{index}", "can_automate") for index in range(200))
    assert len(live_list(parameters)) == 128


def test_a_set_class_id_is_the_factorys_class_id() -> None:
    """Pro-C 2's factory names e315f479...; its devices store these four fields."""
    assert class_id((2046039523, -924366841, -1386372145, 1881470488)).hex() == "e315f479e7c80748ad5da3cf7024f618"


# -- a real device out of a skeleton set ---------------------------------------


def pro_c_2_device(listed: int, *, visual_offset: int = 0) -> tuple[ET.Element, PluginDevice]:
    """The skeleton's VST2 device made a Pro-C 2, its first ``listed`` slots naming parameters 0 on."""
    root = skeleton("11.3.42")
    bank = LegacyBank("Glue", tuple(0.5 for _ in range(PRO_C_2_PARAMETERS))).encode()
    device = serum_as(root, "FabFilter Pro-C 2", PRO_C_2_ID, bank)
    assert device.owner is not None and device.ordinal is not None
    for position, slot in enumerate(get_element(device.owner, "ParameterList")):
        if position < listed:
            get_element(slot, "ParameterId").set("Value", str(position))
            get_element(slot, "VisualIndex").set("Value", str(position + visual_offset))
            get_element(slot, "ParameterName").set("Value", f"VST2 parameter {position}")
    # Read again: a document's controls are read once, and the slots just changed.
    return root, PluginDevices(root)[device.ordinal]


def slots(device: PluginDevice) -> list[ET.Element]:
    assert device.owner is not None
    return list(get_element(device.owner, "ParameterList"))


def pro_c_2() -> PluginMap:
    (plugin_map,) = CATALOG.named("FabFilter Pro-C 2")
    return plugin_map


def as_pro_c_2_vst3(device: PluginDevice, parameters: ParameterSource) -> None:
    """Rewrite ``device`` as Pro-C 2's VST3, the way a plan's format target does."""
    write(device, as_format(device, pro_c_2(), PluginKind.VST3, parameters=parameters))


@pytest.mark.parametrize(
    ("listed", "visual_offset", "can_rebuild", "reason"),
    [
        pytest.param(PRO_C_2_PARAMETERS, 0, True, None, id="live's own list, a route that rebuilds it"),
        pytest.param(PRO_C_2_PARAMETERS, 0, False, LIVE_LIST_IN_USE, id="live's own list, a route that can't"),
        pytest.param(3, 1, True, CONTROLS_IN_USE, id="a list someone arranged"),
        pytest.param(0, 0, True, None, id="no list at all"),
    ],
)
def test_only_live_s_own_list_is_rebuilt_for_the_target(
    listed: int, visual_offset: int, can_rebuild: bool, reason: str | None
) -> None:
    """Slot ``i`` naming parameter ``i`` at position ``i`` is the list nobody chose; anything else is a choice."""
    _root, device = pro_c_2_device(listed, visual_offset=visual_offset)
    assert refusal(device, PluginKind.VST3, can_rebuild_live_list=can_rebuild) == reason


def test_a_list_something_points_at_is_not_live_s_to_rebuild() -> None:
    root, device = pro_c_2_device(PRO_C_2_PARAMETERS)
    target = slots(device)[1].find("ParameterValue/AutomationTarget")
    assert target is not None
    envelope = ET.SubElement(root, "AutomationEnvelope")
    ET.SubElement(ET.SubElement(envelope, "EnvelopeTarget"), "PointeeId", Value=target.attrib["Id"])
    assert device.ordinal is not None
    assert refusal(PluginDevices(root)[device.ordinal], PluginKind.VST3, can_rebuild_live_list=True) == CONTROLS_IN_USE


VST3_LIST = (
    parameter(0, "Style", "can_automate", high=7.0),
    parameter(1, "Threshold", "can_automate", low=-60.0, high=0.0),
    parameter(9, "Oversampling"),
    parameter(42, "Lookahead Enabled", "can_automate"),
    parameter(1073741824, "Host Bypass", "can_automate", "bypass"),
    parameter(1073741959, "MIDI CC", "can_automate", "program_change"),
)


def installed(uid_fields: tuple[int, int, int, int]) -> tuple[Vst3Parameter, ...]:
    """This machine's Pro-C 2 VST3, as the plugin would describe its parameters."""
    return VST3_LIST


def test_a_device_with_live_s_list_crosses_with_the_list_live_makes_for_the_vst3() -> None:
    """Live lists the automatable parameters without the bypass, in the plugin's order.

    Measured on Pro-C 2, Pro-L 2 and Pro-G: Pro-L 2's Style (not automatable) is
    left out, and so is Host Bypass. Every slot past the list names nothing.
    """
    _root, device = pro_c_2_device(PRO_C_2_PARAMETERS)
    owner = device.owner
    assert owner is not None
    targets_before = [get_element(slot, "ParameterValue.AutomationTarget").attrib["Id"] for slot in slots(device)]
    as_pro_c_2_vst3(device, installed)

    assert owner.find("PluginDesc/Vst3PluginInfo") is not None
    after = list(get_element(owner, "ParameterList"))
    named = [
        (get_element(slot, "ParameterId", attribute="Value"), get_element(slot, "ParameterName", attribute="Value"))
        for slot in after[:4]
    ]
    assert named == [("0", "Style"), ("1", "Threshold"), ("42", "Lookahead Enabled"), ("1073741959", "MIDI CC")]
    assert get_element(after[1], "ParameterValue.MidiControllerRange.Min", attribute="Value") == "-60"
    assert get_element(after[0], "LastUserRange.Last", attribute="Value") == "7"
    assert all(get_element(slot, "ParameterId", attribute="Value") == "-1" for slot in after[4:])
    unassigned = str(UNASSIGNED_VISUAL_INDEX)
    assert all(get_element(slot, "VisualIndex", attribute="Value") == unassigned for slot in after[4:])
    # Each slot keeps its own target ids, so nothing elsewhere in the set changes meaning.
    assert [get_element(slot, "ParameterValue.AutomationTarget").attrib["Id"] for slot in after] == targets_before


def test_a_device_with_live_s_list_is_left_as_it_was_when_the_vst3_cannot_be_read() -> None:
    _root, device = pro_c_2_device(PRO_C_2_PARAMETERS)
    assert device.owner is not None
    before = ET.tostring(device.owner)
    with pytest.raises(StateTransformError, match="could not be read"):
        as_pro_c_2_vst3(device, no_parameters)
    assert ET.tostring(device.owner) == before


def test_a_bank_still_has_to_be_the_plugins_own() -> None:
    """The list is rebuilt only after the patch itself crosses; a bank of the wrong size refuses first."""
    _root, device = pro_c_2_device(PRO_C_2_PARAMETERS)
    preset = get_element(device.info, "Preset.VstPreset")
    get_element(preset, "Buffer").text = hex_text.encode(b"Glue".ljust(28, b"\x00") + struct.pack("<9f", *[0.5] * 9), 1)
    with pytest.raises(StateTransformError, match="no recorded layout"):
        as_pro_c_2_vst3(device, installed)


def test_slots_without_a_last_user_range_still_take_the_list() -> None:
    """Most Live 10 and 11 slots have no LastUserRange; Live 12's do."""
    _root, device = pro_c_2_device(PRO_C_2_PARAMETERS)
    owner = device.owner
    assert owner is not None
    for slot in slots(device):
        last = slot.find("LastUserRange")
        if last is not None:
            slot.remove(last)
    as_pro_c_2_vst3(device, installed)
    assert get_element(get_element(owner, "ParameterList")[1], "ParameterId", attribute="Value") == "1"


def test_a_slot_missing_what_live_always_writes_leaves_the_device_untouched() -> None:
    _root, device = pro_c_2_device(PRO_C_2_PARAMETERS)
    value = get_element(slots(device)[0], "ParameterValue")
    value.remove(get_element(value, "MidiControllerRange"))
    assert device.owner is not None
    before = ET.tostring(device.owner)
    with pytest.raises(StateTransformError, match="no slot layout"):
        as_pro_c_2_vst3(device, installed)
    assert ET.tostring(device.owner) == before
