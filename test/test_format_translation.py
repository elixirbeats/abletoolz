"""Plugin format translation: a VST2 device rewritten as the VST3 Live writes.

Hermetic. The oracle is the fixtures themselves: a skeleton that holds both a
VstPluginInfo and a Vst3PluginInfo says exactly what shape that version of Live
gives a VST3 device, so a translated device is compared against its neighbour
rather than against a shape written down here. State payloads, moduleinfo.json
files and config mappings are built in tmp_path.
"""

from __future__ import annotations

import dataclasses
import pathlib
import struct
from xml.etree import ElementTree as ET

import pytest

from abletoolz import decode_encode
from abletoolz.live_set import AbletonSet, plugins
from abletoolz.misc import get_element
from abletoolz.plugin_parsers import PluginKind
from abletoolz.plugin_parsers import format_translation as translation
from abletoolz.plugin_parsers.config import AbletoolzConfig
from abletoolz.plugin_parsers.format_translation import TranslationTarget
from abletoolz.plugin_parsers.state import StateTransform
from abletoolz.plugin_parsers.state.fabfilter import (
    FABFILTER_CONSTANT_CONTROLLER,
    FABFILTER_CONTROLLER_TRAILER,
    EditorState,
    FfbsControllerState,
)
from abletoolz.plugin_parsers.state.fxbk import LegacyBank

SKELETONS = pathlib.Path(__file__).parent / "version_fixtures" / "skeletons"

# Ad-hoc targets, so the tests exercise the machinery rather than the seed table.
EFFECT = TranslationTarget(PluginKind.VST3, "Test Effect", (1, 2, 3, 4))
INSTRUMENT = TranslationTarget(PluginKind.VST3, "Test Instrument", (-5, 6, -7, 8))
KILOHEARTS = TranslationTarget(PluginKind.VST3, "kHs Test", (9, 10, 11, 12), StateTransform.KILOHEARTS)
# A FabF-generation FabFilter: a constant twelve byte controller state, whatever
# the patch. A legacy bank is all its VST2 ever saved.
FABF_TARGET = TranslationTarget(
    PluginKind.VST3, "Pro-C 2", (13, 14, 15, 16), controller_state=FABFILTER_CONSTANT_CONTROLLER
)
# An FFBS-generation one, whose controller state carries the preset name.
FFBS_TARGET = TranslationTarget(
    PluginKind.VST3, "Pro-Q 3", (17, 18, 19, 20), controller_state=FfbsControllerState(b"FQ3p")
)


def legacy_bank(preset_name: str, parameters: int = 8) -> bytes:
    """A Live stored-parameter bank, which is what an .als holds for an older FabFilter."""
    return LegacyBank(preset_name=preset_name, parameters=tuple(0.5 for _ in range(parameters))).encode()


def make_set(key: str) -> AbletonSet:
    ableton_set = AbletonSet(SKELETONS / f"{key}.als")
    assert ableton_set.parse()
    return ableton_set


def vst2_info(live_set: AbletonSet, plug_name: str) -> ET.Element:
    """The VstPluginInfo of one named device in a set."""
    for info in live_set.root.iter("VstPluginInfo"):
        if get_element(info, "PlugName", attribute="Value") == plug_name:
            return info
    raise AssertionError(f"no VST2 device named {plug_name}")


def tags(element: ET.Element) -> tuple[str, ...]:
    return tuple(child.tag for child in element)


def set_state(info: ET.Element, payload: bytes) -> str:
    """Give a VST2 device some saved state, wrapped the way Live wraps a hex blob."""
    preset = get_element(info, "Preset.VstPreset")
    state = get_element(preset, "Buffer")
    state.text = decode_encode.string_to_xml(payload.hex().upper(), levels=(preset.text or "").count("\t") + 1)
    return state.text


def read_state(element: ET.Element) -> bytes:
    assert element.text is not None
    return bytes.fromhex(decode_encode.xml_to_string(element.text)[0])


# -- shape ------------------------------------------------------------------


@pytest.mark.parametrize(("key", "plug_name"), [("11.3.42", "Serum_x64"), ("10.1.3", "Texture")])
def test_translated_device_matches_the_vst3_live_wrote_beside_it(key: str, plug_name: str) -> None:
    """Every version keeps a different subset of the info tags; the fixture says which."""
    live_set = make_set(key)
    reference = live_set.root.find(".//Vst3PluginInfo")
    assert reference is not None
    expected_info = tags(reference)
    expected_preset = tags(get_element(reference, "Preset.Vst3Preset"))

    info = vst2_info(live_set, plug_name)
    translation.translate_device(info, EFFECT)

    assert info.tag == "Vst3PluginInfo"
    assert tags(info) == expected_info
    assert tags(get_element(info, "Preset.Vst3Preset")) == expected_preset


def test_vst2_only_elements_are_gone() -> None:
    live_set = make_set("11.3.42")
    info = vst2_info(live_set, "Serum_x64")
    translation.translate_device(info, EFFECT)
    dropped = {
        "Path",
        "PlugName",
        "UniqueId",
        "Inputs",
        "Outputs",
        "NumberOfParameters",
        "NumberOfPrograms",
        "Flags",
        "Version",
        "VstVersion",
        "IsShellClient",
        "Category",
    }
    assert dropped.isdisjoint(tags(info))
    preset = get_element(info, "Preset.Vst3Preset")
    assert {
        "Type",
        "ProgramCount",
        "ParameterCount",
        "ProgramNumber",
        "PluginVersion",
        "UniqueId",
        "ByteOrder",
        "Buffer",
    }.isdisjoint(tags(preset))


def test_shared_preset_head_is_untouched() -> None:
    """The 13 tags up to ParametersListWrapperLomId mean the same in both formats."""
    live_set = make_set("11.3.42")
    info = vst2_info(live_set, "Serum_x64")
    preset = get_element(info, "Preset.VstPreset")
    before = [ET.tostring(child) for child in list(preset)[:13]]
    translation.translate_device(info, EFFECT)
    assert [ET.tostring(child) for child in list(get_element(info, "Preset.Vst3Preset"))[:13]] == before


def test_uid_fields_are_written_at_both_levels() -> None:
    live_set = make_set("11.3.42")
    info = vst2_info(live_set, "Serum_x64")
    translation.translate_device(info, INSTRUMENT)
    uids = list(info.iter("Uid"))
    assert len(uids) == 2
    for uid in uids:
        assert tags(uid) == ("Fields.0", "Fields.1", "Fields.2", "Fields.3")
        assert translation.read_uid_fields(uid) == INSTRUMENT.uid_fields


def test_name_comes_from_the_target_not_the_vst2() -> None:
    """A VST3 is usually known by a shorter display name than its VST2 file."""
    live_set = make_set("11.3.42")
    info = vst2_info(live_set, "Serum_x64")
    translation.translate_device(info, EFFECT)
    assert get_element(info, "Name", attribute="Value") == "Test Effect"


@pytest.mark.parametrize(("category", "device_type"), [("2", "1"), ("1", "2"), ("0", "2"), ("4", "2")])
def test_device_type_follows_vst2_category(category: str, device_type: str) -> None:
    """VST2 says instrument with Category 2; VST3 says it with DeviceType 1."""
    live_set = make_set("11.3.42")
    info = vst2_info(live_set, "Serum_x64")
    get_element(info, "Category").set("Value", category)
    translation.translate_device(info, EFFECT)
    assert get_element(info, "DeviceType", attribute="Value") == device_type
    assert get_element(info, "Preset.Vst3Preset.DeviceType", attribute="Value") == device_type


def test_a_target_declaring_no_controller_state_writes_the_element_empty() -> None:
    """Right for the plugins measured to write none, which is what the default says.

    Live writes exactly one ControllerState per ProcessorState whether or not
    the plugin puts anything in it, so the element is there either way.
    """
    live_set = make_set("11.3.42")
    info = vst2_info(live_set, "Serum_x64")
    set_state(info, b"some state")
    translation.translate_device(info, EFFECT)
    preset = get_element(info, "Preset.Vst3Preset")
    order = tags(preset)
    assert order.index("ControllerState") == order.index("ProcessorState") + 1
    controller = get_element(preset, "ControllerState")
    assert controller.text is None
    assert len(controller) == 0


@pytest.mark.parametrize("preset_name", ["Quiet Glue", "Snare Bus"])
def test_a_constant_controller_state_is_written_whatever_the_patch(preset_name: str) -> None:
    """Pro-C 2, Pro-L 2, Pro-MB and Pro-R each write the same twelve bytes every time."""
    live_set = make_set("11.3.42")
    info = vst2_info(live_set, "Serum_x64")
    set_state(info, legacy_bank(preset_name))
    translation.translate_device(info, FABF_TARGET)
    assert read_state(get_element(info, "Preset.Vst3Preset.ControllerState")) == FABFILTER_CONTROLLER_TRAILER


def test_a_constant_controller_state_is_indented_the_way_live_writes_a_blob() -> None:
    """A hex block Live did not lay out makes the file stop looking like Live's own."""
    live_set = make_set("11.3.42")
    info = vst2_info(live_set, "Serum_x64")
    set_state(info, legacy_bank("Default Setting"))
    translation.translate_device(info, FABF_TARGET)
    preset = get_element(info, "Preset.Vst3Preset")
    text = get_element(preset, "ControllerState").text
    assert text is not None
    assert text == decode_encode.string_to_xml(
        FABFILTER_CONTROLLER_TRAILER.hex().upper(), levels=(preset.text or "").count("\t") + 1
    )


def test_an_ffbs_controller_state_carries_the_preset_name_out_of_the_bank() -> None:
    """The name lives in the editor state, and an .als does carry that -- as this element."""
    live_set = make_set("11.3.42")
    info = vst2_info(live_set, "Serum_x64")
    set_state(info, legacy_bank("Smear bM"))
    translation.translate_device(info, FFBS_TARGET)
    payload = read_state(get_element(info, "Preset.Vst3Preset.ControllerState"))
    assert payload.endswith(FABFILTER_CONTROLLER_TRAILER)
    assert EditorState.parse(payload).preset_name == "Smear bM"


def test_an_ffbs_controller_state_carries_a_chunk_s_editor_half_across_whole() -> None:
    """Same product both sides: the VST2 chunk's second half is already this state."""
    editor = EditorState(
        magic=b"FQ3p",
        version=3,
        preset_name="Flutter Machine MdB",
        instance_index=-1,
        label="",
        controllers=(("XY1", "Character"),),
    )
    live_set = make_set("11.3.42")
    info = vst2_info(live_set, "Serum_x64")
    set_state(info, b"FFBS processor half" + editor.encode())
    translation.translate_device(info, FFBS_TARGET)
    payload = read_state(get_element(info, "Preset.Vst3Preset.ControllerState"))
    assert payload == editor.encode() + FABFILTER_CONTROLLER_TRAILER
    assert EditorState.parse(payload) == editor


def test_a_device_saved_with_no_patch_gets_an_empty_controller_state() -> None:
    """There is nothing to build one out of, whatever the target declares."""
    live_set = make_set("11.3.42")
    info = vst2_info(live_set, "Serum_x64")
    assert get_element(info, "Preset.VstPreset.Buffer").text is None
    translation.translate_device(info, FABF_TARGET)
    assert get_element(info, "Preset.Vst3Preset.ControllerState").text is None


@dataclasses.dataclass(frozen=True)
class EchoController:
    """A controller state that hands back what it was given, so a test can see which bytes reach it."""

    def build(self, source: bytes) -> bytes:
        return source


def test_the_controller_state_is_built_from_the_source_not_the_rewritten_processor_state() -> None:
    """The two are halves of one saved chunk, so a rewrite of the first must not reach the second."""
    payload = b"PK\x03\x04payload"
    target = TranslationTarget(
        PluginKind.VST3, "kHs Test", (9, 10, 11, 12), StateTransform.KILOHEARTS, EchoController()
    )
    live_set = make_set("11.3.42")
    info = vst2_info(live_set, "Serum_x64")
    set_state(info, payload)
    translation.translate_device(info, target)
    preset = get_element(info, "Preset.Vst3Preset")
    assert read_state(get_element(preset, "ProcessorState")) == struct.pack("<II", 1, len(payload)) + payload
    assert read_state(get_element(preset, "ControllerState")) == payload


# -- state ------------------------------------------------------------------


def test_state_carries_over_verbatim_by_default() -> None:
    """Serum, Prophet V3, Serato Sample and FabFilter all read their VST2 blob unchanged."""
    live_set = make_set("11.3.42")
    info = vst2_info(live_set, "Serum_x64")
    original = set_state(info, bytes(range(256)) * 4)
    translation.translate_device(info, EFFECT)
    assert get_element(info, "Preset.Vst3Preset.ProcessorState").text == original


def test_kilohearts_state_gains_an_eight_byte_header() -> None:
    """kHs VST3 expects the VST2 zip behind two little-endian uint32: (1, length)."""
    live_set = make_set("11.3.42")
    info = vst2_info(live_set, "Serum_x64")
    payload = b"PK\x03\x04" + bytes(range(64))
    set_state(info, payload)
    translation.translate_device(info, KILOHEARTS)
    state = read_state(get_element(info, "Preset.Vst3Preset.ProcessorState"))
    assert state[:8] == struct.pack("<II", 1, len(payload))
    assert state[8:] == payload


def test_empty_state_stays_empty() -> None:
    """A device saved with no state has nothing to frame."""
    live_set = make_set("11.3.42")
    info = vst2_info(live_set, "Serum_x64")
    assert get_element(info, "Preset.VstPreset.Buffer").text is None
    translation.translate_device(info, KILOHEARTS)
    assert get_element(info, "Preset.Vst3Preset.ProcessorState").text is None


# -- whole set --------------------------------------------------------------


def test_translate_set_reports_what_it_did_and_what_it_could_not() -> None:
    live_set = make_set("11.3.42")
    report = translation.translate_set(live_set, targets={"Effectrix": EFFECT})
    assert sorted(name for _track, name, _to in report.translated) == ["Effectrix", "Serum_x64"]
    assert [target for _track, _name, target in report.translated if _name == "Effectrix"] == ["Test Effect"]
    assert report.translated_count == 2
    # The device already in the target format has no entry, so it is left alone.
    assert [name for _track, name in report.unresolved] == ["Decapitator"]


def test_unresolved_devices_are_reported_and_left_alone() -> None:
    live_set = make_set("11.3.42")
    info = vst2_info(live_set, "Effectrix")
    before = ET.tostring(info)
    report = translation.translate_set(live_set)
    assert "Effectrix" in [name for _track, name in report.unresolved]
    assert ET.tostring(info) == before
    assert info.tag == "VstPluginInfo"


def test_an_entry_naming_a_pair_with_no_translator_leaves_its_device_alone() -> None:
    """The entry is the direction, and (vst, au) is a direction nobody wrote yet."""
    live_set = make_set("11.3.42")
    info = vst2_info(live_set, "Effectrix")
    before = ET.tostring(info)
    au_target = TranslationTarget(PluginKind.AU, "Test AU", (1, 2, 3, 4))
    report = translation.translate_set(live_set, targets={"Effectrix": au_target})
    assert "Effectrix" in [name for _track, name in report.unresolved]
    assert ET.tostring(info) == before


def test_one_table_can_point_two_devices_at_two_formats() -> None:
    """Nothing outside the table chooses a direction, so the table may hold both."""
    live_set = make_set("11.3.42")
    report = translation.translate_set(
        live_set,
        targets={
            "Serum_x64": EFFECT,
            "Effectrix": TranslationTarget(PluginKind.AU, "Test AU", (1, 2, 3, 4)),
        },
    )
    assert [name for _track, name, _to in report.translated] == ["Serum_x64"]
    assert "Effectrix" in [name for _track, name in report.unresolved]


def test_a_translated_device_is_not_met_again_as_its_own_result() -> None:
    """The devices are snapshotted first; a rewritten VST2 must not report twice."""
    live_set = make_set("11.3.42")
    report = translation.translate_set(live_set)
    names = [name for _track, name, _to in report.translated] + [name for _track, name in report.unresolved]
    assert names.count("Serum_x64") == 1
    assert "Serum" not in names


def test_untouched_devices_serialize_identically() -> None:
    """Nothing outside the translated PluginDesc may move by a single byte."""
    live_set = make_set("11.3.42")
    descriptions = list(live_set.root.iter("PluginDesc"))
    before = [ET.tostring(description) for description in descriptions]
    translation.translate_set(live_set)
    after = [ET.tostring(description) for description in descriptions]
    changed = [index for index, (old, new) in enumerate(zip(before, after, strict=True)) if old != new]
    assert len(changed) == 1
    assert b"Serum" in after[changed[0]]


def test_wrapping_device_element_is_untouched() -> None:
    """The PluginDevice around a PluginDesc is the same whichever format is inside."""
    live_set = make_set("11.3.42")
    parents = {child: parent for parent in live_set.root.iter() for child in parent}
    description = parents[vst2_info(live_set, "Serum_x64")]
    wrapper = parents[description]
    assert wrapper.tag == "PluginDevice"
    before = [ET.tostring(child) for child in wrapper if child.tag != "PluginDesc"]
    marks = (wrapper.text, wrapper.tail, description.tail)
    translation.translate_set(live_set)
    assert [ET.tostring(child) for child in wrapper if child.tag != "PluginDesc"] == before
    assert (wrapper.text, wrapper.tail, description.tail) == marks


def test_translating_twice_changes_nothing_the_second_time() -> None:
    live_set = make_set("11.3.42")
    translation.translate_set(live_set)
    settled = ET.tostring(live_set.root)
    report = translation.translate_set(live_set)
    assert report.translated == []
    assert ET.tostring(live_set.root) == settled


def test_report_names_the_track_each_device_sits_on() -> None:
    live_set = make_set("11.3.42")
    track_names = {track.name for track in live_set.tracks.load()}
    report = translation.translate_set(live_set)
    assert {track for track, _name, _to in report.translated} <= track_names


# -- harvesting -------------------------------------------------------------


def test_harvest_set_uids_reads_every_vst3_in_a_set() -> None:
    harvested = translation.harvest_set_uids(make_set("10.1.3"))
    assert set(harvested) == {"Pro-R", "Pro-Q 3", "Pro-L 2"}
    assert all(len(fields) == 4 for fields in harvested.values())


def test_seeded_uid_matches_a_set_that_already_uses_the_vst3() -> None:
    """Cross-check of the seed table against an independent measurement."""
    harvested = translation.harvest_set_uids(make_set("10.1.3"))
    assert translation.KNOWN_TRANSLATIONS["FabFilter Pro-Q 3"].uid_fields == harvested["Pro-Q 3"]


def test_class_id_round_trips_through_uid_fields() -> None:
    """Live's four Uid fields are the class id read as big-endian signed int32."""
    cid = "56535458667358736572756D00000000"
    assert translation.cid_to_uid_fields(cid) == (1448301656, 1718835315, 1701999981, 0)
    assert translation.uid_fields_to_cid(translation.cid_to_uid_fields(cid)) == cid
    assert translation.KNOWN_TRANSLATIONS["Serum_x64"].uid_fields == translation.cid_to_uid_fields(cid)


def write_moduleinfo(root: pathlib.Path, name: str, body: str) -> pathlib.Path:
    bundle = root / f"{name}.vst3" / "Contents"
    bundle.mkdir(parents=True)
    moduleinfo = bundle / "moduleinfo.json"
    moduleinfo.write_text(body, encoding="utf-8")
    return moduleinfo


def test_harvest_moduleinfo_takes_audio_modules_only(tmp_path: pathlib.Path) -> None:
    write_moduleinfo(
        tmp_path,
        "Thing",
        """
        {
          "Name": "Thing",
          "Classes": [
            {"CID": "56535458667358736572756D00000000", "Category": "Audio Module Class", "Name": "Thing"},
            {"CID": "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF", "Category": "Component Controller Class",
             "Name": "Thing Controller"}
          ]
        }
        """,
    )
    assert translation.harvest_moduleinfo_uids([tmp_path]) == {"Thing": (1448301656, 1718835315, 1701999981, 0)}


def test_harvest_moduleinfo_tolerates_trailing_commas(tmp_path: pathlib.Path) -> None:
    """Measured: some vendors ship moduleinfo.json that plain json refuses."""
    write_moduleinfo(
        tmp_path,
        "Loose",
        """
        {
          "Name": "Loose",
          "Classes": [
            {
              "CID": "56535458667358736572756D00000000",
              "Category": "Audio Module Class",
              "Name": "Loose",
            },
          ],
        }
        """,
    )
    assert translation.harvest_moduleinfo_uids([tmp_path]) == {"Loose": (1448301656, 1718835315, 1701999981, 0)}


def test_harvest_moduleinfo_ignores_single_file_plugins(tmp_path: pathlib.Path) -> None:
    """Windows-style single-file .vst3 carries no moduleinfo.json to read."""
    (tmp_path / "Old.vst3").write_bytes(b"")
    assert translation.harvest_moduleinfo_uids([tmp_path]) == {}


# -- config -----------------------------------------------------------------


def test_config_targets_are_parsed_into_targets() -> None:
    parsed = translation.parse_config_targets(
        {"Some Plugin.64": {"name": "Some Plugin", "uid": [1, 2, 3, 4], "state": "kilohearts"}}
    )
    assert parsed == {
        "Some Plugin.64": TranslationTarget(PluginKind.VST3, "Some Plugin", (1, 2, 3, 4), StateTransform.KILOHEARTS)
    }


def test_config_targets_default_to_vst3_and_verbatim() -> None:
    parsed = translation.parse_config_targets({"Old.dll": {"name": "New", "uid": [1, 2, 3, 4]}})
    assert parsed["Old.dll"] == TranslationTarget(PluginKind.VST3, "New", (1, 2, 3, 4), StateTransform.VERBATIM)


def test_config_targets_override_the_seed_table(monkeypatch: pytest.MonkeyPatch) -> None:
    override = TranslationTarget(PluginKind.VST3, "Serum From Config", (1, 2, 3, 4))
    config = AbletoolzConfig(plugin_translation_targets={"Serum_x64": override})
    monkeypatch.setattr(plugins, "load_config", lambda: config)
    live_set = make_set("11.3.42")
    report = live_set.plugins.translate_formats()
    assert [target for _track, _name, target in report.translated] == ["Serum From Config"]


def test_explicit_targets_override_config(monkeypatch: pytest.MonkeyPatch) -> None:
    config = AbletoolzConfig(
        plugin_translation_targets={"Serum_x64": TranslationTarget(PluginKind.VST3, "From Config", (1, 2, 3, 4))}
    )
    monkeypatch.setattr(plugins, "load_config", lambda: config)
    live_set = make_set("11.3.42")
    report = live_set.plugins.translate_formats(targets={"Serum_x64": EFFECT})
    assert [target for _track, _name, target in report.translated] == ["Test Effect"]


def test_seed_table_survives_an_unrelated_config_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    config = AbletoolzConfig(
        plugin_translation_targets={"Effectrix": TranslationTarget(PluginKind.VST3, "Effectrix", (1, 2, 3, 4))}
    )
    monkeypatch.setattr(plugins, "load_config", lambda: config)
    live_set = make_set("11.3.42")
    report = live_set.plugins.translate_formats()
    assert sorted(target for _track, _name, target in report.translated) == ["Effectrix", "Serum"]
