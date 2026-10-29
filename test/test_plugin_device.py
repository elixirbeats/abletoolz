"""The plugin device module reads every fixture's devices and writes them back untouched."""

from __future__ import annotations

import dataclasses
import pathlib
from xml.etree import ElementTree as ET

import pytest
from conftest import SKELETONS, skeleton

from abletoolz.formats import hex_text
from abletoolz.formats.path_data import SetOperatingSystem
from abletoolz.plugins.container.device import (
    PluginDevice,
    PluginDevices,
    ReferenceForm,
    has_writer,
    is_info,
    kind_of,
    set_supports,
)
from abletoolz.plugins.identity.ids import PluginKind

KEYS = sorted(fixture.stem for fixture in SKELETONS.glob("*.als"))
APPLE_BANDPASS = (1635083896, 1651532147, 1634758764)


def count(root: ET.Element) -> int:
    return sum(is_info(element) for element in root.iter())


# -- the oracle -------------------------------------------------------------


@pytest.mark.parametrize("key", KEYS)
def test_every_device_written_back_from_what_was_read_is_byte_identical(key: str) -> None:
    """Reading a device and writing the same facts back changes nothing, in any version."""
    root = skeleton(key)
    before = ET.tostring(root)
    devices = PluginDevices(root)
    assert len(devices) == count(root)
    for device in devices:
        identity, reference, preset = device.identity, device.reference, device.saved_state
        assert identity.kind is device.kind is kind_of(device.info)
        device.set_state(preset.state)
        if device.kind is PluginKind.VST:
            assert identity.name is not None
            assert identity.unique_id is not None
            device.set_name(identity.name)
            device.set_unique_id(identity.unique_id)
            assert identity.program_count is not None
            device.set_layout(programs=identity.program_count)
            device.set_saved_state_record(unique_id=preset.unique_id, programs=preset.program_count)
            if reference.form is ReferenceForm.PATH and reference.path is not None:
                device.set_reference(reference.path)
        elif device.kind is PluginKind.VST3:
            assert identity.name is not None
            device.set_name(identity.name)
            if preset.controller is not None:
                device.set_controller(preset.controller)
    assert ET.tostring(root) == before


@pytest.mark.parametrize("key", KEYS)
def test_every_device_is_read_whole(key: str) -> None:
    """Every fixture device has a name and sits on a track, and the facts its format carries are read."""
    for device in PluginDevices(skeleton(key)):
        identity, reference, preset = device.identity, device.reference, device.saved_state
        assert identity.name
        track = device.track
        assert track is not None and track.tag.endswith("Track")
        if device.kind is PluginKind.VST:
            assert identity.is_instrument is not None
            assert identity.unique_id is not None
            assert identity.parameter_count is not None and identity.program_count is not None
            assert preset.type is not None and preset.program_number is not None
            assert reference.form is not ReferenceForm.NONE
            assert reference.file_name
            if reference.form is ReferenceForm.DIR:
                assert reference.os is not SetOperatingSystem.UNSET
            assert device.complete
        elif device.kind is PluginKind.VST3:
            assert identity.is_instrument is not None
            assert identity.uid_fields is not None and len(identity.uid_fields) == 4
            assert preset.controller is not None
        else:
            assert identity.au is not None
            assert identity.manufacturer


# -- the forms --------------------------------------------------------------


def test_the_dir_form_joins_folder_and_file_name() -> None:
    """A pre-11 device stores its folder as a blob and its file name beside it."""
    root = skeleton("9.0.1")
    device = next(iter(PluginDevices(root)))
    reference = device.reference
    assert reference.form is ReferenceForm.DIR
    assert reference.path is not None and reference.file_name is not None
    # A Windows path read on any OS: split on the separator the set stored, not the host's.
    assert str(reference.path).split("\\")[-1] == reference.file_name
    assert not reference.stub


def test_the_path_form_is_read_and_rewritten_as_posix() -> None:
    root = skeleton("12.4.5b")
    device = next(device for device in PluginDevices(root) if device.kind is PluginKind.VST)
    reference = device.reference
    assert reference.form is ReferenceForm.PATH
    assert reference.path is not None
    device.set_reference(pathlib.Path("D:/Moved/Plugin.dll"))
    moved = device.reference
    assert moved.path == pathlib.Path("D:/Moved/Plugin.dll")
    assert moved.file_name == "Plugin.dll"


def test_a_dir_form_reference_is_rewritten_in_the_windows_form() -> None:
    root = skeleton("9.0.1")
    device = next(iter(PluginDevices(root)))
    device.set_reference(pathlib.Path("C:/New/Plugin.dll"))
    moved = device.reference
    assert moved.form is ReferenceForm.DIR
    assert moved.os is SetOperatingSystem.WINDOWS_OS
    assert moved.file_name == "Plugin.dll"
    assert moved.path is not None and moved.path.name == "Plugin.dll"


def test_a_bare_file_name_is_a_stub_with_no_path() -> None:
    info = ET.fromstring('<VstPluginInfo><Path Value="Serum_x64.dll" /><PlugName Value="Serum_x64" /></VstPluginInfo>')
    reference = PluginDevice(info).reference
    assert reference.stub
    assert reference.path is None
    assert reference.file_name == "Serum_x64.dll"
    assert not PluginDevice(info).complete


def test_an_audio_unit_points_at_no_file_and_reads_its_exact_component_identity() -> None:
    root = skeleton("11.3.41")
    device = next(iter(PluginDevices(root)))
    assert device.kind is PluginKind.AU
    assert device.reference.form is ReferenceForm.NONE
    identity = device.identity
    assert (identity.name, identity.manufacturer, identity.au) == ("AUBandpass", "Apple", APPLE_BANDPASS)
    with pytest.raises(ValueError, match="points at no file"):
        device.set_reference(pathlib.Path("/Library/Audio/Plug-Ins/Components/x.component"))


# -- bytes ------------------------------------------------------------------


def test_state_is_rewritten_at_the_depth_it_was_read_from() -> None:
    """Once a buffer holds hex lines, their indentation is what a rewrite keeps."""
    root = skeleton("11.3.42")
    device = next(device for device in PluginDevices(root) if device.kind is PluginKind.VST)
    preset = device.info.find("Preset/VstPreset")
    assert preset is not None
    buffer = preset.find("Buffer")
    assert buffer is not None
    device.set_state(b"\x01")
    assert buffer.text is not None
    depth = hex_text.depth(buffer.text)
    device.set_state(bytes(range(100)))
    assert buffer.text is not None
    assert hex_text.depth(buffer.text) == depth
    assert device.saved_state.state == bytes(range(100))


def test_an_empty_state_is_written_at_the_depth_its_position_gives_it() -> None:
    """A device saved without a patch has no hex line to read a depth from."""
    root = skeleton("11.3.42")
    device = next(
        device for device in PluginDevices(root) if device.kind is PluginKind.VST and not device.saved_state.state
    )
    preset = device.info.find("Preset/VstPreset")
    assert preset is not None and preset.text is not None
    device.set_state(b"\xff")
    buffer = preset.find("Buffer")
    assert buffer is not None and buffer.text is not None
    assert hex_text.depth(buffer.text) == preset.text.count("\t") + 1
    device.set_state(b"")
    assert buffer.text is None


def test_a_compact_document_writes_hex_without_indentation() -> None:
    info = ET.fromstring("<VstPluginInfo><Preset><VstPreset><Buffer/></VstPreset></Preset></VstPluginInfo>")
    PluginDevice(info).set_state(b"\xab\xcd")
    buffer = info.find("Preset/VstPreset/Buffer")
    assert buffer is not None
    assert buffer.text == "\nABCD\n"


# -- another format ---------------------------------------------------------


def test_rewriting_as_vst3_keeps_the_element_in_place() -> None:
    """A rebuilt device is the same element at the same position, read back as the new format."""
    devices = PluginDevices(skeleton("11.3.42"))
    device = next(device for device in devices if device.kind is PluginKind.VST)
    description = devices.parents[device.info]
    index, indent = list(description).index(device.info), device.info.text
    identity = dataclasses.replace(device.identity, kind=PluginKind.VST3, name="Serum", uid_fields=(1, 2, 3, 4))
    preset = dataclasses.replace(device.saved_state, state=b"\x01\x02", controller=b"\x03")
    device.rewrite(PluginKind.VST3, identity, preset)
    assert device.kind is PluginKind.VST3 and device.info.tag == "Vst3PluginInfo"
    assert list(description).index(device.info) == index and device.info.text == indent
    again = PluginDevice(device.info).identity
    assert (again.name, again.uid_fields, again.is_instrument) == ("Serum", (1, 2, 3, 4), identity.is_instrument)
    assert PluginDevice(device.info).saved_state.state == b"\x01\x02"
    assert PluginDevice(device.info).saved_state.controller == b"\x03"


@pytest.mark.parametrize("kind", [PluginKind.VST3, PluginKind.AU])
def test_a_refused_rewrite_leaves_the_device_as_it_was_read(kind: PluginKind) -> None:
    """A preset with no state to carry over is refused before any of the device is rebuilt."""
    devices = PluginDevices(skeleton("11.3.42"))
    device = next(device for device in devices if device.kind is PluginKind.VST)
    preset = child(device.info, "Preset/VstPreset")
    preset.remove(child(preset, "Buffer"))
    before = ET.tostring(device.info)
    identity = dataclasses.replace(
        device.identity, name="x", uid_fields=(1, 2, 3, 4), au=APPLE_BANDPASS, manufacturer="Apple", flags=0
    )
    with pytest.raises(ValueError, match="holds no Buffer"):
        device.rewrite(kind, identity, device.saved_state)
    assert ET.tostring(device.info) == before
    assert device.kind is PluginKind.VST


def test_only_measured_formats_have_a_writer() -> None:
    assert has_writer(PluginKind.VST3) and has_writer(PluginKind.AU)
    assert not has_writer(PluginKind.VST)
    device = next(device for device in PluginDevices(skeleton("11.3.42")) if device.kind is PluginKind.VST3)
    with pytest.raises(ValueError, match="no writer"):
        device.rewrite(PluginKind.VST, device.identity, device.saved_state)
    with pytest.raises(ValueError, match="already a"):
        device.rewrite(PluginKind.VST3, device.identity, device.saved_state)


# -- controls ---------------------------------------------------------------


def test_placeholders_are_not_controls_in_use() -> None:
    """Live writes a ParameterList of unassigned placeholders for every plugin device."""
    devices = PluginDevices(skeleton("11.3.42"))
    device = next(device for device in devices if device.kind is PluginKind.VST)
    assert device.owner is not None and device.owner.tag == "PluginDevice"
    controls = device.controls
    assert controls.owner_known
    assert controls.configured == ()
    assert controls.malformed == 0
    assert not controls.mapped
    assert controls.targets
    assert not controls.referenced
    assert not controls.in_use


def child(parent: ET.Element, path: str) -> ET.Element:
    found = parent.find(path)
    assert found is not None, path
    return found


def test_bound_mapped_and_referenced_parameters_are_in_use() -> None:
    root = skeleton("11.3.42")
    device = next(device for device in PluginDevices(root) if device.kind is PluginKind.VST)
    owner = device.owner
    assert owner is not None and device.ordinal is not None
    parameter = child(owner, "ParameterList")[0]  # the fixture carries placeholders
    child(parameter, "ParameterId").set("Value", "7")
    assert device.controls.configured == (7,)
    ET.SubElement(child(parameter, "ParameterValue"), "KeyMidi")
    assert device.controls.mapped
    target_id = child(parameter, ".//AutomationTarget").get("Id", "")
    ET.SubElement(root, "PointeeId", Value=target_id)
    # The referenced ids are read once per document, so a new pointer needs a new read.
    assert PluginDevices(root)[device.ordinal].controls.referenced == {target_id}
    assert device.controls.in_use


def test_an_audio_unit_is_owned_by_its_own_device_element() -> None:
    device = next(iter(PluginDevices(skeleton("11.3.41"))))
    assert device.owner is not None and device.owner.tag == "AuPluginDevice"
    assert device.controls.owner_known


def test_a_device_read_outside_its_document_has_no_owner_to_ask() -> None:
    info = ET.fromstring('<VstPluginInfo><PlugName Value="x" /></VstPluginInfo>')
    with pytest.raises(ValueError, match="need its document"):
        _ = PluginDevice(info).controls


# -- the document -----------------------------------------------------------


def test_devices_are_numbered_in_document_order_across_formats() -> None:
    root = skeleton("11.0.12")
    devices = PluginDevices(root)
    kinds = [device.kind for device in devices]
    assert PluginKind.VST in kinds and PluginKind.VST3 in kinds
    for ordinal, device in enumerate(devices):
        assert device.ordinal == ordinal
        assert devices[ordinal].info is device.info


@pytest.mark.parametrize(
    ("key", "vst3"),
    [("9.0.1", False), ("10.0.6", False), ("10.1.18", True), ("11.3.42", True), ("12.4.5b", True)],
)
def test_the_floor_is_read_off_the_document(key: str, vst3: bool) -> None:
    devices = PluginDevices(skeleton(key))
    assert devices.supports(PluginKind.VST)
    assert devices.supports(PluginKind.VST3) is vst3


@pytest.mark.parametrize(
    ("kind", "version", "minor_version", "supported"),
    [
        # The schema decides over the creator.
        (PluginKind.VST3, (11, 3, 42), "10.0_377", True),
        (PluginKind.VST3, (11, 3, 42), "10.0_370", False),
        (PluginKind.VST3, (11, 3, 42), "unknown", False),
        (PluginKind.VST3, (10, 0, 6), None, False),
        # An Audio Unit goes into a schema whose AuPluginInfo the writer can fill;
        # one that declares a ParameterCount it has no value for is refused.
        (PluginKind.AU, (11, 2, 10), "11.0_11202", True),
        (PluginKind.AU, (11, 0, 12), "11.0_433", False),
        (PluginKind.AU, (10, 1, 18), "10.0_377", False),
        (PluginKind.AU, (12, 0, 0), "12.0_12000", False),
        # Without a schema Live ships, only the layout measured on 11.3.41.
        (PluginKind.AU, (11, 3, 41), None, True),
        (PluginKind.AU, (11, 2, 10), None, False),
        (PluginKind.AU, (12, 9, 0), "12.0_19999", True),
    ],
)
def test_the_floor_of_a_set_that_says_its_schema_or_only_its_creator(
    kind: PluginKind, version: tuple[int, int, int], minor_version: str | None, supported: bool
) -> None:
    assert set_supports(kind, version, minor_version=minor_version) is supported


def test_only_info_elements_are_plugin_devices() -> None:
    with pytest.raises(ValueError, match="not a plugin info element"):
        PluginDevice(ET.Element("PluginDesc"))
