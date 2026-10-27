"""Plugin format translation: a VST2 device rewritten as the VST3 Live writes.

Hermetic. The oracle is the fixtures themselves: a skeleton that holds both a
VstPluginInfo and a Vst3PluginInfo says exactly what shape that version of Live
gives a VST3 device, so a translated device is compared against its neighbour
rather than against a shape written down here. State payloads are built here.
"""

from __future__ import annotations

import dataclasses
from xml.etree import ElementTree as ET

import pytest
from conftest import device_named, serum_as, skeleton

from abletoolz.elements import get_element
from abletoolz.formats import hex_text
from abletoolz.plugins import conversion
from abletoolz.plugins.container.device import PluginDevice, PluginDevices
from abletoolz.plugins.conversion import Rebuild, write
from abletoolz.plugins.identity.ids import PluginKind, UidFields, Vst2Id, Vst3Id
from abletoolz.plugins.identity.vst3_parameters import no_parameters
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.fabfilter import (
    FABFILTER_CONSTANT_CONTROLLER,
    FABFILTER_CONTROLLER_TRAILER,
)
from abletoolz.plugins.state.fxbk import LegacyBank
from abletoolz.plugins.state.plugin_map import FormatState

KHS_DISTORTION_VST2 = 1802724468
PRO_C_2_VST2 = 1178808944
INSTRUMENT_UID = (-5, 6, -7, 8)


def serum(key: str = "11.3.42") -> PluginDevice:
    """A skeleton's Serum_x64 VST2 device, which saved no patch."""
    return device_named(skeleton(key), "Serum_x64")


def tags(element: ET.Element) -> tuple[str, ...]:
    return tuple(child.tag for child in element)


def as_vst3(
    device: PluginDevice,
    name: str = "Test Effect",
    uid_fields: UidFields = (1, 2, 3, 4),
    *,
    controller: bytes | None = None,
) -> None:
    """Rebuild a VST2 device as an ad-hoc VST3 holding its patch as it is.

    Ad hoc so the tests exercise the container rather than any plugin's map.
    """
    identity = dataclasses.replace(device.identity, kind=PluginKind.VST3, name=name, uid_fields=uid_fields)
    write(device, Rebuild(PluginKind.VST3, identity, dataclasses.replace(device.saved_state, controller=controller)))


def read_state(element: ET.Element) -> bytes:
    assert element.text is not None
    return hex_text.decode(element.text)


# -- shape ------------------------------------------------------------------


@pytest.mark.parametrize(("key", "plug_name"), [("11.3.42", "Serum_x64"), ("10.1.18", "EchoBoy")])
def test_translated_device_matches_the_vst3_live_wrote_beside_it(key: str, plug_name: str) -> None:
    """Every version keeps a different subset of the info tags; the fixture says which.

    Matching them exactly is also what drops every VST2-only tag (Path, PlugName,
    UniqueId, the counts, Flags, Category; Type, Buffer and the rest of the preset).
    """
    root = skeleton(key)
    reference = root.find(".//Vst3PluginInfo")
    assert reference is not None
    device = device_named(root, plug_name)
    as_vst3(device)

    assert device.info.tag == "Vst3PluginInfo"
    assert tags(device.info) == tags(reference)
    assert tags(get_element(device.info, "Preset.Vst3Preset")) == tags(get_element(reference, "Preset.Vst3Preset"))


def test_shared_preset_head_is_untouched() -> None:
    """The 13 tags up to ParametersListWrapperLomId mean the same in both formats."""
    device = serum()
    before = [ET.tostring(child) for child in list(get_element(device.info, "Preset.VstPreset"))[:13]]
    as_vst3(device)
    assert [ET.tostring(child) for child in list(get_element(device.info, "Preset.Vst3Preset"))[:13]] == before


def test_the_targets_name_and_class_id_are_written_the_id_at_both_levels() -> None:
    """A VST3 is usually known by a shorter display name than its VST2 file."""
    device = serum()
    as_vst3(device, "Test Instrument", INSTRUMENT_UID)
    assert get_element(device.info, "Name", attribute="Value") == "Test Instrument"
    uids = list(device.info.iter("Uid"))
    assert len(uids) == 2
    for uid in uids:
        assert tags(uid) == ("Fields.0", "Fields.1", "Fields.2", "Fields.3")
        assert tuple(int(field.get("Value", "")) for field in uid) == INSTRUMENT_UID


@pytest.mark.parametrize(("category", "device_type"), [("2", "1"), ("1", "2"), ("0", "2"), ("4", "2")])
def test_device_type_follows_vst2_category(category: str, device_type: str) -> None:
    """VST2 says instrument with Category 2; VST3 says it with DeviceType 1."""
    device = serum()
    get_element(device.info, "Category").set("Value", category)
    as_vst3(device)
    assert get_element(device.info, "DeviceType", attribute="Value") == device_type
    assert get_element(device.info, "Preset.Vst3Preset.DeviceType", attribute="Value") == device_type


# -- controller state -------------------------------------------------------


def test_a_target_declaring_no_controller_state_writes_the_element_empty() -> None:
    """Right for the plugins measured to write none, which is what the default says.

    Live writes exactly one ControllerState per ProcessorState whether or not
    the plugin puts anything in it, so the element is there either way.
    """
    device = serum()
    device.set_state(b"some state")
    as_vst3(device)
    preset = get_element(device.info, "Preset.Vst3Preset")
    order = tags(preset)
    assert order.index("ControllerState") == order.index("ProcessorState") + 1
    controller = get_element(preset, "ControllerState")
    assert controller.text is None
    assert len(controller) == 0


def test_a_controller_state_is_laid_out_as_live_lays_out_a_blob() -> None:
    """A hex block Live did not lay out makes the file stop looking like Live's own.

    The blob is FabFilter's constant, whose bytes ``test_state`` holds against a set Live wrote.
    """
    device = serum()
    bank = LegacyBank(preset_name="Quiet Glue", parameters=tuple(0.5 for _ in range(8))).encode()
    device.set_state(bank)
    as_vst3(device, "Pro-C 2", controller=FABFILTER_CONSTANT_CONTROLLER.build(bank))
    preset = get_element(device.info, "Preset.Vst3Preset")
    depth = (preset.text or "").count("\t") + 1
    assert get_element(preset, "ControllerState").text == hex_text.encode(FABFILTER_CONTROLLER_TRAILER, depth)


# -- state ------------------------------------------------------------------


def test_state_carries_over_verbatim_by_default() -> None:
    """Serum, Prophet V3, Serato Sample and FabFilter all read their VST2 blob unchanged."""
    device = serum()
    device.set_state(bytes(range(256)) * 4)
    original = get_element(device.info, "Preset.VstPreset.Buffer").text
    as_vst3(device)
    assert get_element(device.info, "Preset.Vst3Preset.ProcessorState").text == original


def test_a_mapped_plugin_carries_its_patch_through_its_own_map() -> None:
    """The device holds what the map crosses, under the map's name; the bytes are ``test_plugin_maps``'s."""
    payload = b"PK\x03\x04" + bytes(range(64))
    device = serum_as(skeleton("11.3.42"), "kHs Distortion", KHS_DISTORTION_VST2, payload)
    khs_distortion = CATALOG.find(Vst2Id(KHS_DISTORTION_VST2))
    assert khs_distortion is not None

    write(device, conversion.as_format(device, khs_distortion, PluginKind.VST3, parameters=no_parameters))
    preset = get_element(device.info, "Preset.Vst3Preset")
    crossed = khs_distortion.cross(FormatState(payload), PluginKind.VST, PluginKind.VST3)
    assert read_state(get_element(preset, "ProcessorState")) == crossed.state
    assert get_element(preset, "ControllerState").text is None
    assert get_element(device.info, "Name").get("Value") == "kHs Distortion"


@pytest.mark.parametrize(
    ("name", "unique_id"), [("kHs Distortion", KHS_DISTORTION_VST2), ("FabFilter Pro-C 2", PRO_C_2_VST2)]
)
def test_a_device_saved_with_no_patch_stays_without_one(name: str, unique_id: int) -> None:
    """There is nothing to frame, and nothing to build a controller state out of, whatever the map declares."""
    device = serum_as(skeleton("11.3.42"), name, unique_id)
    assert get_element(device.info, "Preset.VstPreset.Buffer").text is None
    plugin_map = CATALOG.find(Vst2Id(unique_id))
    assert plugin_map is not None
    write(device, conversion.as_format(device, plugin_map, PluginKind.VST3, parameters=no_parameters))
    assert get_element(device.info, "Preset.Vst3Preset.ProcessorState").text is None
    assert get_element(device.info, "Preset.Vst3Preset.ControllerState").text is None


def test_a_plugin_map_refuses_a_device_of_another_plugin() -> None:
    khs_distortion = CATALOG.find(Vst2Id(KHS_DISTORTION_VST2))
    assert khs_distortion is not None
    with pytest.raises(ValueError, match="not kHs Distortion"):
        conversion.as_format(serum(), khs_distortion, PluginKind.VST3, parameters=no_parameters)


# -- class ids measured in a set ----------------------------------------------


def test_a_mapped_class_id_matches_a_set_that_already_uses_the_vst3() -> None:
    """Cross-check of a plugin map against an independent measurement: the class id Live saved."""
    stored = {
        device.identity.name: device.identity.uid_fields
        for device in PluginDevices(skeleton("10.1.30"))
        if device.kind is PluginKind.VST3
    }
    assert set(stored) == {"Phase Plant", "Pro-Q 3", "Pro-L 2"}
    pro_q_3 = stored["Pro-Q 3"]
    assert pro_q_3 is not None
    found = CATALOG.find(Vst3Id(pro_q_3))
    assert found is not None and found.name == "FabFilter Pro-Q 3"
