"""Writing a plugin device as an Audio Unit, and back out of one.

The oracle is the AUBandpass device Live 11.3.41 wrote into its fixture: the
written element must have the children that one has, in the same order, inside
an ``AuPluginDevice``. The patches are the Valhalla fixtures the headless hosts
read back, so what goes into the Buffer is what the AU itself saved.
"""

from __future__ import annotations

import dataclasses
from xml.etree import ElementTree as ET

import pytest
from conftest import AU_UNITS, au_fixture, serum_as, skeleton

from abletoolz.formats import hex_text, live_document
from abletoolz.plugins.container.device import PluginDevice, PluginDevices
from abletoolz.plugins.conversion import as_format, write
from abletoolz.plugins.identity.ids import PluginKind
from abletoolz.plugins.identity.vst3_parameters import no_parameters
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.audio_unit import ClassInfo
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.plugin_map import PluginMap
from abletoolz.schema.live_schemas import live_schemas

SUPERMASSIVE = AU_UNITS["valhalla-supermassive"]


def supermassive() -> PluginMap:
    found = CATALOG.find(SUPERMASSIVE)
    assert found is not None
    return found


def live_au_device() -> ET.Element:
    """The AuPluginDevice Live 11.3.41 wrote."""
    return next(skeleton("11.3.41").iter("AuPluginDevice"))


def children(element: ET.Element, tag: str) -> list[str]:
    found = next(element.iter(tag))
    return [child.tag for child in found]


def vst2_supermassive() -> tuple[ET.Element, PluginDevice]:
    """The 11.3.42 fixture's Serum device, made into a Valhalla Supermassive VST2 holding a real patch."""
    root = skeleton("11.3.42")
    return root, serum_as(root, "ValhallaSupermassive", 1934451059, au_fixture("valhalla-supermassive", "vst2.chunk"))


def test_a_vst2_device_becomes_the_au_device_live_writes() -> None:
    root, device = vst2_supermassive()
    owner = device.owner
    assert owner is not None

    write(device, as_format(device, supermassive(), PluginKind.AU, parameters=no_parameters))

    live = live_au_device()
    assert owner.tag == "AuPluginDevice"
    assert [child.tag for child in owner] == [child.tag for child in live]
    assert [child.tag for child in device.info] == children(live, "AuPluginInfo")
    preset = device.info.find("Preset/AuPreset")
    assert preset is not None
    assert [child.tag for child in preset] == children(live, "AuPreset")


def test_the_written_au_device_reads_back_as_that_plugin_holding_that_patch() -> None:
    root, device = vst2_supermassive()
    write(device, as_format(device, supermassive(), PluginKind.AU, parameters=no_parameters))

    (written,) = [item for item in PluginDevices(root) if item.kind is PluginKind.AU]
    identity = written.identity
    assert identity.au == SUPERMASSIVE.identifier
    assert (identity.name, identity.manufacturer, identity.flags) == ("ValhallaSupermassive", "Valhalla DSP, LLC", 0)
    assert identity.is_instrument is False
    assert ClassInfo.parse(written.saved_state.state).plugin_state == au_fixture("valhalla-supermassive", "vst2.chunk")
    preset = written.info.find("Preset/AuPreset")
    assert preset is not None
    codes = {tag: preset.find(tag).get("Value") for tag in ("Manufacturer", "SubType", "Type")}  # type: ignore[union-attr]
    assert codes == {
        "Manufacturer": str(SUPERMASSIVE.identifier[2]),
        "SubType": str(SUPERMASSIVE.identifier[1]),
        "Type": str(SUPERMASSIVE.identifier[0]),
    }


def test_an_au_device_becomes_a_vst3_in_a_plugin_device() -> None:
    device = next(item for item in PluginDevices(skeleton("11.3.41")) if item.kind is PluginKind.AU)
    info = device.info
    for tag, value in zip(
        ("ComponentType", "ComponentSubType", "ComponentManufacturer"), SUPERMASSIVE.identifier, strict=True
    ):
        info.find(tag).set("Value", str(value))  # type: ignore[union-attr]
    device.set_state(au_fixture("valhalla-supermassive", "au.plist"))
    owner = device.owner
    assert owner is not None

    write(device, as_format(device, supermassive(), PluginKind.VST3, parameters=no_parameters))

    assert owner.tag == "PluginDevice"
    assert info.tag == "Vst3PluginInfo"
    state = info.find("Preset/Vst3Preset/ProcessorState")
    assert state is not None
    assert hex_text.decode(state.text) == au_fixture("valhalla-supermassive", "vst3.state")


def vst2_supermassive_in(root: ET.Element) -> PluginDevice:
    """The first VST2 device of ``root``, its parameter list emptied, made into a Valhalla Supermassive.

    It holds a real patch; the emptied list keeps Live's own list out of the way.
    """
    device = next(item for item in PluginDevices(root) if item.kind is PluginKind.VST and item.complete)
    owner = device.owner
    assert owner is not None
    parameters = owner.find("ParameterList")
    assert parameters is not None
    parameters[:] = []
    assert not device.controls.in_use
    device.set_name("ValhallaSupermassive")
    device.set_unique_id(1934451059)
    device.set_state(au_fixture("valhalla-supermassive", "vst2.chunk"))
    return device


@pytest.mark.parametrize(("key", "relabel"), [("11.3.42", "11.0_11202"), ("12.4.5b", None)], ids=["11.2", "12.4.5b"])
def test_a_vst2_device_becomes_the_au_its_sets_schema_declares(key: str, relabel: str | None) -> None:
    """Exactly the AuPluginInfo and AuPreset members that schema declares, in Live's order.

    Schema-checked, not opened in Live. No fixture of 11.2's schema (11.0_11202)
    holds a plugin device, so the 11.3.42 set stands in, labelled as one.
    """
    root = skeleton(key)
    if relabel is not None:
        root.set("MinorVersion", relabel)
        root.set("Creator", "Ableton Live 11.2.10")
    device = vst2_supermassive_in(root)
    assert PluginDevices(root).supports(PluginKind.AU)

    write(device, as_format(device, supermassive(), PluginKind.AU, parameters=no_parameters))

    schemas = live_schemas()
    minor_version = root.get("MinorVersion", "")
    declared = schemas.members_of(minor_version, "AuPluginInfo")
    assert declared is not None
    assert [child.tag for child in device.info] == [
        tag for tag in children(live_au_device(), "AuPluginInfo") if tag in declared
    ]
    assert {child.tag for child in device.info} == declared
    preset = device.info.find("Preset/AuPreset")
    assert preset is not None
    assert {child.tag for child in preset} == schemas.members_of(minor_version, "AuPreset")
    (written,) = [
        item for item in PluginDevices(live_document.parse(live_document.encode(root))) if item.kind is PluginKind.AU
    ]
    assert written.identity.au == SUPERMASSIVE.identifier


@pytest.mark.parametrize("key", ["9.0.1", "9.6.0", "10.0.6", "10.1.18", "11.0.12"])
def test_an_older_schema_whose_au_info_holds_a_parameter_count_is_refused(key: str) -> None:
    """Those schemas declare a ParameterCount, which a device of another format has no AU value for."""
    root = skeleton(key)
    assert "ParameterCount" in (live_schemas().members_of(root.get("MinorVersion", ""), "AuPluginInfo") or ())
    device = vst2_supermassive_in(root)
    assert not PluginDevices(root).supports(PluginKind.AU)
    before = ET.tostring(device.info)
    with pytest.raises(ValueError, match="ParameterCount"):
        write(device, as_format(device, supermassive(), PluginKind.AU, parameters=no_parameters))
    assert ET.tostring(device.info) == before


def test_moving_a_device_into_an_au_needs_its_owner() -> None:
    """Without the document there is no AuPluginDevice to rename, and Live would not load the result."""
    root, device = vst2_supermassive()
    bare = PluginDevice(device.info)
    before = ET.tostring(device.info)
    with pytest.raises(ValueError, match="needs its owner"):
        write(bare, as_format(bare, supermassive(), PluginKind.AU, parameters=no_parameters))
    assert ET.tostring(device.info) == before


def test_an_au_whose_registration_is_not_recorded_is_not_written() -> None:
    plugin_map = supermassive()
    assert plugin_map.can_write(PluginKind.AU)
    unrecorded = plugin_map.au
    assert unrecorded is not None
    bare = dataclasses.replace(plugin_map, au=dataclasses.replace(unrecorded, registration=None))
    assert not bare.can_write(PluginKind.AU)
    with pytest.raises(StateTransformError, match="registration is not recorded"):
        bare.lower(au_fixture("valhalla-supermassive", "vst2.chunk"), PluginKind.AU)
