"""Audio Units read and written on a computer without them: Arturia's archive units, FabFilter's parameter units,
and the slots an AU device gives up on becoming a VST device.

Every preset here is built in the test, the shapes as units saved on a Mac hold them (2026-11-03): Arturia's
archive under ``Controller State`` beside the SDK's ``data`` snapshot, and a FabF-generation FabFilter's
parameters, numbered as its VST2 numbers them, in that snapshot alone.
"""

from __future__ import annotations

import plistlib
import struct
from xml.etree import ElementTree as ET

import pytest

from abletoolz.formats.arturia_archive import TwoArchives
from abletoolz.plugins.container.parameter_list import FLOAT_SLOT, VST_SLOTS, as_vst_slots
from abletoolz.plugins.identity.ids import PluginKind
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.fabfilter import PRO_C_2_TABLE
from abletoolz.plugins.state.fxbk import LegacyBank

ARCHIVE = b"22 serialization::archive 10 0 7 0 7 7 Default 6 Stereo 61 7 Arturia 0  0 0 0 0 "


def preset(map_id: str, **entries: object) -> bytes:
    unit = CATALOG.by_map_id(map_id)
    assert unit is not None
    component_type, subtype, manufacturer = unit.plugin_format(PluginKind.AU).id.identifier  # type: ignore[union-attr]
    identity = {"type": component_type, "subtype": subtype, "manufacturer": manufacturer, "version": 0}
    return plistlib.dumps(identity | entries, fmt=plistlib.FMT_XML)


def snapshot(values: dict[int, float]) -> bytes:
    pairs = b"".join(struct.pack(">If", key, value) for key, value in values.items())
    return struct.pack(">III", 0, 0, len(values)) + pairs


def lift(map_id: str, state: bytes) -> bytes:
    unit = CATALOG.by_map_id(map_id)
    assert unit is not None
    return unit.plugin_format(PluginKind.AU).framing.lift(state)


@pytest.mark.parametrize("map_id", ["arturia.cs-80-v3", "arturia.chorus-jun-6", "arturia.dist-tube-culture"])
def test_an_arturia_unit_s_controller_state_is_the_archive_its_vst_formats_hold(map_id: str) -> None:
    state = preset(map_id, data=snapshot({0: 0.5}), **{"Controller State": ARCHIVE})
    assert lift(map_id, state) == TwoArchives.join(ARCHIVE, ARCHIVE)


def test_an_arturia_unit_without_its_archive_and_another_unit_s_preset_are_refused() -> None:
    with pytest.raises(StateTransformError, match="no Controller State"):
        lift("arturia.chorus-jun-6", preset("arturia.chorus-jun-6", data=snapshot({0: 0.5})))
    with pytest.raises(StateTransformError, match="not"):
        lift("arturia.chorus-jun-6", preset("arturia.dist-tube-culture", **{"Controller State": ARCHIVE}))


def test_a_pro_c_2_unit_s_parameters_are_its_vst2_bank_in_the_plugin_s_units() -> None:
    knobs = tuple(0.0 if index % 3 else 0.75 for index in range(len(PRO_C_2_TABLE.parameters)))
    native = {p.bank_index: p.transfer(knobs[p.bank_index]) for p in PRO_C_2_TABLE.parameters}
    bank = LegacyBank.parse(lift("fabfilter.pro-c-2", preset("fabfilter.pro-c-2", name="Glue", data=snapshot(native))))
    assert bank.preset_name == "Glue"
    assert PRO_C_2_TABLE.convert(bank.encode()) == PRO_C_2_TABLE.convert(LegacyBank("Glue", knobs).encode())


def test_a_pro_c_2_unit_missing_a_parameter_is_refused() -> None:
    with pytest.raises(StateTransformError, match="no value for"):
        lift("fabfilter.pro-c-2", preset("fabfilter.pro-c-2", name="", data=snapshot({0: 0.0})))


def slot(tag: str, number: int) -> ET.Element:
    element = ET.Element(tag, Id=str(number))
    for name, value in (("ParameterName", ""), ("ParameterId", "-1"), ("VisualIndex", "1073741823")):
        ET.SubElement(element, name, Value=value)
    value = ET.SubElement(element, "ParameterValue")
    ET.SubElement(value, "AutomationTarget", Id=str(1000 + 2 * number))
    if tag == FLOAT_SLOT:
        ET.SubElement(value, "ModulationTarget", Id=str(1001 + 2 * number))
    return element


def test_an_au_device_leaving_for_a_vst_keeps_128_float_slots_with_fresh_modulation_ids() -> None:
    root = ET.fromstring('<Ableton><LiveSet><NextPointeeId Value="5000" /></LiveSet></Ableton>')
    parameters = ET.Element("ParameterList")
    parameters.extend(slot(FLOAT_SLOT if n % 2 == 0 else "PluginEnumParameter", n) for n in range(256))

    as_vst_slots(parameters, root)

    assert len(parameters) == VST_SLOTS and {child.tag for child in parameters} == {FLOAT_SLOT}
    enum_slot = parameters[1]
    assert enum_slot.get("Id") == "1"
    assert enum_slot.find("ParameterValue/AutomationTarget").get("Id") == "1002"  # type: ignore[union-attr]
    modulation = [int(child.find("ParameterValue/ModulationTarget").get("Id", "")) for child in parameters[1::2]]  # type: ignore[union-attr]
    assert modulation == list(range(5000, 5064))
    assert root.find("LiveSet/NextPointeeId").get("Value") == "5064"  # type: ignore[union-attr]


@pytest.mark.parametrize("map_id", ["arturia.cs-80-v3", "arturia.chorus-jun-6", "arturia.dist-tube-culture"])
def test_an_arturia_unit_s_preset_written_here_holds_the_archive_and_reads_back(map_id: str) -> None:
    unit = CATALOG.by_map_id(map_id)
    assert unit is not None
    framing = unit.plugin_format(PluginKind.AU).framing
    written = framing.lower(TwoArchives.join(ARCHIVE, ARCHIVE))
    assert plistlib.loads(written)["Controller State"] == ARCHIVE
    assert framing.lift(written) == TwoArchives.join(ARCHIVE, ARCHIVE)


def test_a_pro_c_2_unit_s_preset_written_here_holds_every_parameter_and_reads_back() -> None:
    unit = CATALOG.by_map_id("fabfilter.pro-c-2")
    assert unit is not None
    framing = unit.plugin_format(PluginKind.AU).framing
    knobs = tuple(0.0 if index % 4 else 1.0 for index in range(len(PRO_C_2_TABLE.parameters)))
    bank = LegacyBank("Glue", knobs).encode()
    written = framing.lower(bank)
    assert plistlib.loads(written)["name"] == "Glue"
    assert PRO_C_2_TABLE.convert(framing.lift(written)) == PRO_C_2_TABLE.convert(bank)
