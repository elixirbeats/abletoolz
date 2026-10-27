"""Chain grafting, checked against raw XML harvested independently.

The findings that shaped the writer, all read off the gunzipped fixtures
before any of it was written in code:

* A device owns a lot of the set-global id space -- a bare ``Reverb`` owns 54
  ``AutomationTarget``/``ModulationTarget``/``Pointee`` ids, 10.0.1's rack
  owns 196, 11.0.12's owns 1556 -- so renumbering is the whole job, and it
  has to walk the entire subtree rather than the top level.
* ``LiveSet/NextPointeeId`` exists from 10.0 on and always sits above every
  id in the set. Live 9 sets own the same kind of ids (3983 of them in 9.0.1)
  but carry no such element, which is why grafting into them is refused.
* No ``PointeeId`` reference in the corpus lives inside a device subtree.
  They live in ``AutomationEnvelopes``/``ClipEnvelope`` targets and name
  mixer parameters (Tempo, TimeSignature, Speaker), so the test that proves
  an internal reference is remapped has to inject one first.
* A track's ``Devices`` list is indented seven tabs deep and the main track's
  six, so a graft between the two has to re-indent.
"""

from __future__ import annotations

import pathlib
from xml.etree import ElementTree as ET

import pytest
from conftest import raw_root, skeleton_set, writable_set

from abletoolz.live_set import AbletonSet
from abletoolz.live_set.devices import TrackDevices
from abletoolz.live_set.tracks import AbletonTrack
from abletoolz.live_set.xml_edit import owns_pointee_id

OWNER_TAGS = {"AutomationTarget", "ModulationTarget", "Pointee"}


def track_of(ableton_set: AbletonSet, name: str) -> AbletonTrack:
    return ableton_set.tracks.named(name, main=True)


def chain_of(ableton_set: AbletonSet, track_name: str) -> TrackDevices:
    return next(chain for chain in ableton_set.devices.inventory() if chain.track_name == track_name)


def owned_ids(element: ET.Element) -> list[int]:
    return [
        int(node.attrib["Id"])
        for node in element.iter()
        if node.tag in OWNER_TAGS and node.attrib.get("Id", "").isdigit()
    ]


def counter_of(ableton_set: AbletonSet) -> ET.Element:
    counter = ableton_set.root.find("LiveSet/NextPointeeId")
    assert counter is not None
    return counter


def devices_element(ableton_set: AbletonSet, track_name: str) -> ET.Element:
    """The target's ``Devices`` list, found by raw walk rather than through the API."""
    for candidate in ableton_set.root.iter():
        name = candidate.find("Name/EffectiveName")
        user = candidate.find("Name/UserName")
        found = (user.get("Value") if user is not None else "") or (name.get("Value") if name is not None else "")
        if found == track_name:
            devices = candidate.find("DeviceChain/DeviceChain/Devices")
            if devices is not None:
                return devices
    raise AssertionError(f"no chain for {track_name!r}")


# --- grafting inside one set ------------------------------------------------


def test_graft_appends_a_chain_onto_an_empty_track(tmp_path: pathlib.Path) -> None:
    ableton_set = writable_set("12.4.5b", tmp_path)
    grafted = ableton_set.devices.graft_chain(
        chain_of(ableton_set, "A-Reverb"), track_of(ableton_set, "3-Skylark - Iced")
    )

    assert [(ref.tag, ref.display_name, ref.enabled) for ref in grafted] == [("Reverb", "Reverb", True)]
    assert [device.tag for device in chain_of(ableton_set, "3-Skylark - Iced").devices] == ["Reverb"]
    assert [device.tag for device in chain_of(ableton_set, "A-Reverb").devices] == ["Reverb"]


def test_graft_appends_after_the_devices_already_there(tmp_path: pathlib.Path) -> None:
    ableton_set = writable_set("12.4.5b", tmp_path)
    ableton_set.devices.graft_chain(chain_of(ableton_set, "B-Delay"), track_of(ableton_set, "1-LOW"))
    assert [device.display_name for device in chain_of(ableton_set, "1-LOW").devices] == ["LOW", "V-Clip", "Delay"]


def test_replace_clears_the_target_chain_first(tmp_path: pathlib.Path) -> None:
    ableton_set = writable_set("12.4.5b", tmp_path)
    ableton_set.devices.graft_chain(chain_of(ableton_set, "A-Reverb"), track_of(ableton_set, "1-LOW"), mode="replace")
    assert [device.display_name for device in chain_of(ableton_set, "1-LOW").devices] == ["Reverb"]


def test_grafted_devices_take_ids_past_whatever_the_chain_uses(tmp_path: pathlib.Path) -> None:
    """10.0.1's Bass chain already numbers up to 6, so the copy starts at 7."""
    ableton_set = writable_set("10.0.1", tmp_path)
    ableton_set.devices.graft_chain(chain_of(ableton_set, "Kicks"), track_of(ableton_set, "Bass"))
    ids = [device.device_element.get("Id") for device in chain_of(ableton_set, "Bass").devices]
    assert ids == ["1", "2", "3", "4", "5", "6", "7", "8", "9"]


def test_replacing_a_chain_numbers_the_copy_from_zero(tmp_path: pathlib.Path) -> None:
    ableton_set = writable_set("10.0.1", tmp_path)
    ableton_set.devices.graft_chain(chain_of(ableton_set, "Kicks"), track_of(ableton_set, "Bass"), mode="replace")
    ids = [device.device_element.get("Id") for device in chain_of(ableton_set, "Bass").devices]
    assert ids == ["0", "1", "2"]


def test_a_rack_carries_its_nested_chains_and_their_own_id_space(tmp_path: pathlib.Path) -> None:
    ableton_set = writable_set("10.0.1", tmp_path)
    grafted = ableton_set.devices.graft_chain(chain_of(ableton_set, "Drum Bus"), track_of(ableton_set, "3-Audio"))
    rack = next(ref for ref in grafted if ref.tag == "AudioEffectGroupDevice")
    nested = [(device.tag, device.get("Id")) for branch in rack.device_element.iter("Devices") for device in branch]
    assert nested == [("Saturator", "0"), ("Eq8", "0")]
    assert rack.device_element.get("Id") == "3"  # the target chain was using 0, so the copy starts at 1


# --- grafting between sets --------------------------------------------------


def test_graft_from_another_set_of_the_same_major(tmp_path: pathlib.Path) -> None:
    target = writable_set("12.4.5b", tmp_path)
    donor_set = skeleton_set("12.2.6")
    donor = chain_of(donor_set, "2-100_SoulShufflebreak_01_SP_4")

    grafted = target.devices.graft_chain(donor, track_of(target, "3-Skylark - Iced"))
    assert [ref.tag for ref in grafted] == ["OriginalSimpler"]
    assert [device.tag for device in chain_of(target, "3-Skylark - Iced").devices] == ["OriginalSimpler"]


def test_the_donor_set_is_left_untouched(tmp_path: pathlib.Path) -> None:
    target = writable_set("12.4.5b", tmp_path)
    donor_set = skeleton_set("12.2.6")
    donor = chain_of(donor_set, "A-Reverb")
    before = ET.tostring(donor.devices[0].device_element, encoding="unicode")
    donor_counter = counter_of(donor_set).get("Value")

    target.devices.graft_chain(donor, track_of(target, "3-Skylark - Iced"))
    assert ET.tostring(donor.devices[0].device_element, encoding="unicode") == before
    assert counter_of(donor_set).get("Value") == donor_counter


def test_a_cross_major_graft_is_refused(tmp_path: pathlib.Path) -> None:
    target = writable_set("12.4.5b", tmp_path)
    donor = chain_of(skeleton_set("11.2.10"), "A-Reverb")
    with pytest.raises(ValueError, match="Live 11.x chain into a Live 12.x set"):
        target.devices.graft_chain(donor, track_of(target, "3-Skylark - Iced"))


def test_grafting_into_a_pre_10_set_is_refused(tmp_path: pathlib.Path) -> None:
    """9.x owns pointee ids but has no allocator to take fresh ones from."""
    target = writable_set("9.6.0", tmp_path)
    assert target.root.find("LiveSet/NextPointeeId") is None
    assert owned_ids(target.root)  # ...yet the ids are there, 4136 of them
    donor = chain_of(skeleton_set("9.0.1"), "2-Drum Rack")
    with pytest.raises(ValueError, match="no LiveSet/NextPointeeId"):
        target.devices.graft_chain(donor, track_of(target, "9-Audio"))


def test_grafting_onto_another_set_s_track_is_refused(tmp_path: pathlib.Path) -> None:
    ableton_set = writable_set("12.4.5b", tmp_path)
    foreign = track_of(skeleton_set("12.4.5b"), "3-Skylark - Iced")
    with pytest.raises(ValueError, match="does not belong to this set"):
        ableton_set.devices.graft_chain(chain_of(ableton_set, "A-Reverb"), foreign)


# --- remote bindings --------------------------------------------------------


def test_a_graft_leaves_the_donors_key_midi_bindings_behind(tmp_path: pathlib.Path) -> None:
    """Live 12 dies loading a document whose device arrived carrying these; the donor keeps its own."""
    ableton_set = writable_set("11.0.12", tmp_path)
    donor = chain_of(ableton_set, "8-Drums to MIDI")
    before = sum(len(list(device.device_element.iter("KeyMidi"))) for device in donor.devices)
    assert before == 224

    grafted = ableton_set.devices.graft_chain(donor, track_of(ableton_set, "9-MIDI"))

    assert [device.tag for device in grafted] == ["DrumGroupDevice", "Erosion", "PluginDevice"]
    assert [list(device.device_element.iter("KeyMidi")) for device in grafted] == [[], [], []]
    assert sum(len(list(device.device_element.iter("KeyMidi"))) for device in donor.devices) == before

    # The parameters the bindings hung off travel intact.
    donor_simplers = [element.get("Id") for element in donor.devices[0].device_element.iter("OriginalSimpler")]
    assert [element.get("Id") for element in grafted[0].device_element.iter("OriginalSimpler")] == donor_simplers
    transpose = grafted[0].device_element.findall(".//OriginalSimpler/Pitch/TransposeKey/Manual")
    assert len(transpose) == 16
    assert {element.get("Value") for element in transpose} == {"0"}


# --- the set-global id space ------------------------------------------------


def test_graft_renumbers_every_id_it_owns_and_advances_the_counter(tmp_path: pathlib.Path) -> None:
    ableton_set = writable_set("12.4.5b", tmp_path)
    counter = counter_of(ableton_set)
    before = int(counter.get("Value", ""))
    donor = chain_of(ableton_set, "A-Reverb")
    donor_ids = owned_ids(donor.devices[0].device_element)
    assert len(donor_ids) == 54  # one Reverb, 54 automatable things

    grafted = ableton_set.devices.graft_chain(donor, track_of(ableton_set, "3-Skylark - Iced"))

    new_ids = owned_ids(grafted[0].device_element)
    assert new_ids == list(range(before, before + 54))
    assert int(counter.get("Value", "")) == before + 54
    assert owned_ids(donor.devices[0].device_element) == donor_ids  # donor untouched

    everything = owned_ids(ableton_set.root)
    assert len(everything) == len(set(everything))
    assert int(counter.get("Value", "")) > max(everything)


def test_graft_renumbers_ids_nested_deep_inside_a_rack(tmp_path: pathlib.Path) -> None:
    ableton_set = writable_set("11.0.12", tmp_path)
    counter = counter_of(ableton_set)
    before = int(counter.get("Value", ""))
    donor = chain_of(ableton_set, "B-Bass Send Stack-1")
    assert len(owned_ids(donor.devices[0].device_element)) == 1556

    grafted = ableton_set.devices.graft_chain(donor, track_of(ableton_set, "9-MIDI"))
    assert owned_ids(grafted[0].device_element) == list(range(before, before + 1556))
    assert int(counter.get("Value", "")) == before + 1556

    everything = owned_ids(ableton_set.root)
    assert len(everything) == len(set(everything))


def test_internal_references_are_remapped_and_external_ones_are_not(tmp_path: pathlib.Path) -> None:
    """No fixture device holds a PointeeId, so the donor gets both kinds injected."""
    ableton_set = writable_set("12.4.5b", tmp_path)
    donor = chain_of(ableton_set, "A-Reverb")
    reverb = donor.devices[0].device_element
    switch = reverb.find("On/AutomationTarget")
    assert switch is not None
    inside = switch.attrib["Id"]

    marker = ET.SubElement(reverb, "AbletoolzProbe")
    ET.SubElement(marker, "PointeeId", {"Value": inside})  # names the device's own switch
    ET.SubElement(marker, "PointeeId", {"Value": "8"})  # names the main track's Tempo, outside the copy

    counter = counter_of(ableton_set)
    before = int(counter.get("Value", ""))
    grafted = ableton_set.devices.graft_chain(donor, track_of(ableton_set, "3-Skylark - Iced"))

    copied = grafted[0].device_element.find("AbletoolzProbe")
    assert copied is not None
    internal, external = list(copied)
    copied_switch = grafted[0].device_element.find("On/AutomationTarget")
    assert copied_switch is not None
    assert internal.get("Value") == copied_switch.attrib["Id"]
    assert internal.get("Value") != inside
    assert before <= int(internal.attrib["Value"]) < before + 54
    assert external.get("Value") == "8"


def test_automation_envelopes_outside_the_chain_keep_their_targets(tmp_path: pathlib.Path) -> None:
    """The main track's Tempo/TimeSignature envelopes name ids no graft touches."""
    ableton_set = writable_set("12.4.5b", tmp_path)
    before = [reference.get("Value") for reference in ableton_set.root.iter("PointeeId")]
    assert before == ["10", "8"]
    ableton_set.devices.graft_chain(chain_of(ableton_set, "A-Reverb"), track_of(ableton_set, "3-Skylark - Iced"))
    assert [reference.get("Value") for reference in ableton_set.root.iter("PointeeId")] == before


# --- pretty-printing and fidelity -------------------------------------------


def test_a_graft_and_its_undo_reproduce_lives_xml_byte_for_byte(tmp_path: pathlib.Path) -> None:
    """Strongest fidelity check available: nothing outside the target list moves."""
    ableton_set = writable_set("11.2.10", tmp_path)
    before = ableton_set.generate_xml()
    counter = counter_of(ableton_set)
    was = counter.get("Value", "")

    grafted = ableton_set.devices.graft_chain(
        chain_of(ableton_set, "A-Reverb"), track_of(ableton_set, "4-GB_DrumHit_01")
    )
    assert ableton_set.generate_xml() != before

    devices = devices_element(ableton_set, "4-GB_DrumHit_01")
    for ref in grafted:
        devices.remove(ref.device_element)
    devices.text = None
    counter.set("Value", was)
    assert ableton_set.generate_xml() == before


def test_untouched_chains_serialize_identically(tmp_path: pathlib.Path) -> None:
    ableton_set = writable_set("12.4.5b", tmp_path)
    before = {
        chain.track_name: ET.tostring(devices_element(ableton_set, chain.track_name), encoding="unicode")
        for chain in ableton_set.devices.inventory()
    }
    ableton_set.devices.graft_chain(chain_of(ableton_set, "A-Reverb"), track_of(ableton_set, "3-Skylark - Iced"))
    after = {
        chain.track_name: ET.tostring(devices_element(ableton_set, chain.track_name), encoding="unicode")
        for chain in ableton_set.devices.inventory()
    }
    assert {name for name in before if before[name] != after[name]} == {"3-Skylark - Iced"}


def test_a_track_chain_grafted_onto_the_main_track_is_re_indented(tmp_path: pathlib.Path) -> None:
    """Track chains sit seven tabs in, the main chain six."""
    ableton_set = writable_set("12.4.5b", tmp_path)
    grafted = ableton_set.devices.graft_chain(chain_of(ableton_set, "A-Reverb"), track_of(ableton_set, "Main"))
    main = devices_element(ableton_set, "Main")
    assert main.text == "\n" + "\t" * 6
    assert [device.tail for device in main] == ["\n" + "\t" * 6, "\n" + "\t" * 5]
    assert grafted[0].device_element.text == "\n" + "\t" * 7
    switch = grafted[0].device_element.find("On")
    assert switch is not None
    assert switch.text == "\n" + "\t" * 8


def test_the_main_chain_grafted_onto_a_track_is_re_indented(tmp_path: pathlib.Path) -> None:
    ableton_set = writable_set("12.4.5b", tmp_path)
    grafted = ableton_set.devices.graft_chain(chain_of(ableton_set, "Main"), track_of(ableton_set, "3-Skylark - Iced"))
    devices = devices_element(ableton_set, "3-Skylark - Iced")
    assert devices.text == "\n" + "\t" * 7
    assert [device.tail for device in devices] == ["\n" + "\t" * 6]
    assert grafted[0].device_element.text == "\n" + "\t" * 8


# --- persistence ------------------------------------------------------------


def test_graft_survives_save_and_reopen(tmp_path: pathlib.Path) -> None:
    ableton_set = writable_set("12.4.5b", tmp_path)
    donor = chain_of(skeleton_set("12.2.6"), "B-Delay")
    ableton_set.devices.graft_chain(donor, track_of(ableton_set, "3-Skylark - Iced"))
    ableton_set.get_file_times()
    ableton_set.save_set()

    root = raw_root(ableton_set.path)
    grafted = root.findall("LiveSet/Tracks/AudioTrack/DeviceChain/DeviceChain/Devices/Delay")
    assert len(grafted) == 1

    reopened = AbletonSet(ableton_set.path)
    assert reopened.parse()
    assert [device.tag for device in chain_of(reopened, "3-Skylark - Iced").devices] == ["Delay"]
    ids = owned_ids(reopened.root)
    assert len(ids) == len(set(ids))
    assert int(counter_of(reopened).get("Value", "")) > max(ids)


# --- automation on a replaced chain -----------------------------------------


def automate(ableton_set: AbletonSet, track_name: str, target_ids: list[str]) -> ET.Element:
    """Give the track one envelope per target id, the shape Live writes, and answer the list."""
    (track,) = [track for track in ableton_set.tracks.load() if track.name == track_name]
    envelopes = track.track_root.find("AutomationEnvelopes/Envelopes")
    assert envelopes is not None
    for index, target_id in enumerate(target_ids):
        envelope = ET.SubElement(envelopes, "AutomationEnvelope", Id=str(index))
        ET.SubElement(ET.SubElement(envelope, "EnvelopeTarget"), "PointeeId", Value=target_id)
        ET.SubElement(envelope, "Automation")
    return envelopes


def envelope_targets(envelopes: ET.Element) -> list[str | None]:
    return [envelope.find("EnvelopeTarget/PointeeId").get("Value") for envelope in envelopes]  # type: ignore[union-attr]


def a_device_parameter_and_the_track_volume(ableton_set: AbletonSet, track_name: str) -> tuple[str, str]:
    device = devices_element(ableton_set, track_name)[0]
    device_target = next(node for node in device.iter("AutomationTarget"))
    (track,) = [track for track in ableton_set.tracks.load() if track.name == track_name]
    volume = track.track_root.find("DeviceChain/Mixer/Volume/AutomationTarget")
    assert volume is not None
    return device_target.attrib["Id"], volume.attrib["Id"]


def test_replace_drops_automation_of_the_devices_it_removes(tmp_path: pathlib.Path) -> None:
    """Measured recombining two finished songs: a replace left two envelopes naming gone parameters."""
    ableton_set = writable_set("12.4.5b", tmp_path)
    device_param, volume = a_device_parameter_and_the_track_volume(ableton_set, "1-LOW")
    envelopes = automate(ableton_set, "1-LOW", [device_param, volume])
    ableton_set.devices.graft_chain(chain_of(ableton_set, "A-Reverb"), track_of(ableton_set, "1-LOW"), mode="replace")
    assert envelope_targets(envelopes) == [volume]


def test_append_keeps_the_automation_of_the_devices_it_keeps(tmp_path: pathlib.Path) -> None:
    ableton_set = writable_set("12.4.5b", tmp_path)
    device_param, volume = a_device_parameter_and_the_track_volume(ableton_set, "1-LOW")
    envelopes = automate(ableton_set, "1-LOW", [device_param, volume])
    ableton_set.devices.graft_chain(chain_of(ableton_set, "A-Reverb"), track_of(ableton_set, "1-LOW"))
    assert envelope_targets(envelopes) == [device_param, volume]


def test_replace_drops_the_clip_envelopes_of_the_devices_it_removes(tmp_path: pathlib.Path) -> None:
    """10.1.18's "20-Audio" clips hold 3 ClipEnvelopes on its own chain's parameters; a replace took them away."""
    ableton_set = writable_set("10.1.18", tmp_path)
    target = track_of(ableton_set, "20-Audio")
    assert len(list(target.track_root.iter("ClipEnvelope"))) == 3
    ableton_set.devices.graft_chain(chain_of(ableton_set, "21-Audio"), target, mode="replace")
    owned = {node.get("Id") for node in ableton_set.root.iter() if owns_pointee_id(node.tag)}
    targets = [envelope.find("EnvelopeTarget/PointeeId") for envelope in ableton_set.root.iter("ClipEnvelope")]
    assert [found.get("Value") for found in targets if found is not None and found.get("Value") not in owned] == []
    assert not list(target.track_root.iter("ClipEnvelope"))
