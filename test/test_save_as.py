"""Writing a set for an older Live release, against the version skeletons."""

from __future__ import annotations

import pathlib
from xml.etree import ElementTree as ET

import pytest
from conftest import SKELETONS, copy_set

from abletoolz.formats import live_document
from abletoolz.live_set import AbletonSet
from abletoolz.live_set.save_as import NewerTarget, UnknownTarget, convert, resolve_target
from abletoolz.schema import structure
from abletoolz.schema.versioning import Version, parse_creator


def _read(name: str) -> tuple[ET.Element, Version]:
    root = live_document.read(SKELETONS / f"{name}.als")
    creator = root.get("Creator")
    assert creator is not None
    return root, parse_creator(creator)


def test_a_partial_version_resolves_to_the_newest_matching_release() -> None:
    assert resolve_target("11.3").release == max(r for r in structure.RELEASES if r[:2] == (11, 3))
    assert resolve_target("11.3.42").creator == "Ableton Live 11.3.42"
    assert resolve_target("10").release[0] == 10


def test_an_unmeasured_release_is_refused_with_its_measured_neighbours() -> None:
    with pytest.raises(UnknownTarget, match=r"12\.0.*11\.3\.43 and 12\.1\.5"):
        resolve_target("12.0")
    with pytest.raises(UnknownTarget, match="not a Live version"):
        resolve_target("eleven")


def test_the_target_header_is_written_whole() -> None:
    root, version = _read("12.2.6")
    target = resolve_target("11.3.42")
    converted, report = convert(root, version, target)
    assert tuple(converted.attrib.items()) == structure.RELEASES[(11, 3, 42)]
    assert target.schema == ("11.0_11300", 7)
    assert report.target is target


def test_the_original_tree_is_not_touched() -> None:
    root, version = _read("12.2.6")
    before = live_document.to_xml_bytes(root)
    convert(root, version, resolve_target("11.3.42"))
    assert live_document.to_xml_bytes(root) == before


def test_renamed_tags_take_the_target_spelling() -> None:
    root, version = _read("12.2.6")
    converted, report = convert(root, version, resolve_target("11.3.42"))
    assert next(converted.iter("MasterTrack"), None) is not None
    assert next(converted.iter("MainTrack"), None) is None
    assert next(converted.iter("IsSongTempoLeader"), None) is None
    assert {(moved.newer, moved.older) for moved in report.respelled} >= {
        ("MainTrack", "MasterTrack"),
        ("IsSongTempoLeader", "IsSongTempoMaster"),
    }


def test_note_attributes_live_11_always_writes_are_filled_in_its_order() -> None:
    root, version = _read("12.2.6")
    converted, report = convert(root, version, resolve_target("11.3.42"))
    order = structure.ATTRIBUTE_ORDER[("11.0_11300", 7)]["MidiNoteEvent"]
    notes = list(converted.iter("MidiNoteEvent"))
    assert notes
    for note in notes:
        assert tuple(note.attrib) == order
        assert note.get("Probability") == "1"
    assert {added.attribute for added in report.filled if added.tag == "MidiNoteEvent"} == {
        "IsEnabled",
        "Probability",
        "VelocityDeviation",
    }


def test_what_the_target_never_wrote_is_removed_and_reported() -> None:
    # Live 11.3.42 refuses a whole set over one attribute it does not know.
    root, version = _read("12.2.6")
    converted, report = convert(root, version, resolve_target("11.3.42"))
    removed = {item.what for item in report.removed}
    assert "AudioClip/ScaleInformation" in removed
    assert "MidiTrack@SelectedToolPanel" in removed
    assert all(clip.find("ScaleInformation") is None for clip in converted.iter("AudioClip"))
    assert all(track.get("SelectedToolPanel") is None for track in converted.iter("MidiTrack"))
    assert all(item.first_seen > (11, 3, 42) for item in report.removed)


def test_a_renamed_element_is_respelled_not_removed() -> None:
    root, version = _read("12.2.6")
    converted, report = convert(root, version, resolve_target("11.3.42"))
    assert not {item.what for item in report.removed} & {"LiveSet/MainTrack", "Mixer/ViewStateSessionTrackWidth"}
    assert next(converted.iter("ViewStateSesstionTrackWidth"), None) is not None


def test_a_renamed_element_keeps_only_what_its_old_spelling_held() -> None:
    # Live 12's main track carries SelectedToolPanel; Live 11's MasterTrack never did.
    root, version = _read("12.2.6")
    assert next(root.iter("MainTrack")).get("SelectedToolPanel") is not None
    converted, report = convert(root, version, resolve_target("11.3.42"))
    assert not next(converted.iter("MasterTrack")).attrib
    assert "MainTrack@SelectedToolPanel" in {item.what for item in report.removed}


def _with_live_12_device(root: ET.Element) -> ET.Element:
    """Put an Auto Filter (Live 12.2) on the first audio track, with one parameter automated."""
    track = next(root.iter("AudioTrack"))
    chain = track.find(".//Devices")
    assert chain is not None
    device = ET.SubElement(chain, "AutoFilter2", {"Id": "7"})
    ET.SubElement(device, "UserName", {"Value": "Wobble"})
    ET.SubElement(ET.SubElement(device, "Frequency"), "AutomationTarget", {"Id": "987654"})
    envelopes = track.find("AutomationEnvelopes/Envelopes")
    assert envelopes is not None
    envelope = ET.SubElement(envelopes, "AutomationEnvelope", {"Id": "0"})
    ET.SubElement(ET.SubElement(envelope, "EnvelopeTarget"), "PointeeId", {"Value": "987654"})
    return root


def test_a_device_the_target_lacks_becomes_a_named_rack_of_its_kind(caplog: pytest.LogCaptureFixture) -> None:
    root, version = _read("12.2.6")
    converted, report = convert(_with_live_12_device(root), version, resolve_target("11.3.42"))

    assert next(converted.iter("AutoFilter2"), None) is None
    rack = next(rack for rack in converted.iter("AudioEffectGroupDevice") if rack.get("Id") == "7")
    name = rack.find("UserName")
    assert name is not None and name.get("Value") == "Missing: Wobble"
    assert [(item.device, item.name, item.rack) for item in report.replaced] == [
        ("AutoFilter2", "Wobble", "AudioEffectGroupDevice")
    ]
    assert "has no AutoFilter2" in caplog.text


def test_automation_of_a_removed_device_goes_with_it() -> None:
    root, version = _read("12.2.6")
    converted, report = convert(_with_live_12_device(root), version, resolve_target("11.3.42"))
    pointees = {pointee.get("Value") for pointee in converted.iter("PointeeId")}
    assert "987654" not in pointees
    assert report.orphaned_envelopes == 1


def test_automation_of_a_removed_controller_target_goes_with_it() -> None:
    root, version = _read("12.2.6")
    root = _with_live_12_device(root)
    track = next(root.iter("AudioTrack"))
    frequency = next(track.iter("Frequency"))
    ET.SubElement(frequency, "ControllerTargets.0", {"Id": "987655"})
    envelopes = track.find("AutomationEnvelopes/Envelopes")
    assert envelopes is not None
    envelope = ET.SubElement(envelopes, "AutomationEnvelope", {"Id": "1"})
    ET.SubElement(ET.SubElement(envelope, "EnvelopeTarget"), "PointeeId", {"Value": "987655"})

    converted, report = convert(root, version, resolve_target("11.3.42"))
    assert "987655" not in {pointee.get("Value") for pointee in converted.iter("PointeeId")}
    assert report.orphaned_envelopes == 2


def test_a_newer_target_is_live_s_job() -> None:
    root, version = _read("11.3.42")
    with pytest.raises(NewerTarget, match="open it in Live 12.2.6"):
        convert(root, version, resolve_target("12.2.6"))


def test_a_set_is_written_beside_itself_and_left_alone(tmp_path: pathlib.Path) -> None:
    source = copy_set(tmp_path, "11.3.42", "song.als")
    live_set = AbletonSet(source)
    live_set.parse()
    live_set.load_version()

    path, report = live_set.save_as(resolve_target("11.3.41"))

    assert path == tmp_path / "song [Live 11.3.41].als"
    assert live_document.read(path).get("Creator") == "Ableton Live 11.3.41"
    assert source.read_bytes() == (SKELETONS / "11.3.42.als").read_bytes()
    assert live_set.root.get("Creator") == "Ableton Live 11.3.42"
    assert report.source == (11, 3, 42)
    again, _ = live_set.save_as(resolve_target("11.3.41"))
    assert again == tmp_path / "song [Live 11.3.41]__1.als"


def test_a_live_12_set_is_written_for_live_11_and_its_gaps_are_warned_about(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    source = tmp_path / "song.als"
    original = (SKELETONS / "12.2.6.als").read_bytes()
    source.write_bytes(original)
    live_set = AbletonSet(source)
    assert live_set.parse()
    _with_live_12_device(live_set.root)
    before = ET.tostring(live_set.root)

    path, report = live_set.save_as(resolve_target("11.3.42"), tmp_path / "output")

    assert path == tmp_path / "output" / "song [Live 11.3.42].als"
    assert live_document.read(path).get("Creator") == "Ableton Live 11.3.42"
    assert [item.rack for item in report.replaced] == ["AudioEffectGroupDevice"]
    assert ("AudioEffectGroupDevice", "Pointee", 1) in report.missing
    assert "very experimental" in caplog.text
    assert "is now an empty" in caplog.text
    assert "AudioEffectGroupDevice/Pointee" in caplog.text
    assert source.read_bytes() == original
    assert ET.tostring(live_set.root) == before


def test_a_set_with_nothing_missing_warns_about_nothing_missing(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    source = copy_set(tmp_path, "11.3.42", "song.als")
    live_set = AbletonSet(source)
    assert live_set.parse()

    _, report = live_set.save_as(resolve_target("11.3.41"))

    assert not report.missing
    assert "stay missing" not in caplog.text
