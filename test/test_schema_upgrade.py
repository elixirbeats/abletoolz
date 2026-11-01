"""Moving a Live 10.0 or Live 9 document to the first schema that holds a VST3 device."""

from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest
from conftest import SKELETONS

from abletoolz.formats import live_document
from abletoolz.live_set.xml_edit import owns_pointee_id
from abletoolz.schema.live_schemas import live_schemas
from abletoolz.schema.upgrade import VST3_SCHEMA, preview, upgrade, vst3_upgrade

LIVE_10_0 = """<Ableton MajorVersion="5" MinorVersion="10.0_370" SchemaChangeCount="11" Creator="Ableton Live 10.0.6">
<LiveSet><Tracks><MidiTrack Id="1"><DeviceChain><MainSequencer><ClipSlotList><ClipSlot Id="0"><ClipSlot><Value>
<MidiClip Id="0" Time="0"><WarpMarkers><WarpMarker Id="0" SecTime="0" BeatTime="0" /></WarpMarkers></MidiClip>
<AudioClip Id="1" Time="0"><WarpMarkers><WarpMarker Id="0" SecTime="0" BeatTime="0" /></WarpMarkers></AudioClip>
</Value></ClipSlot></ClipSlot></ClipSlotList></MainSequencer></DeviceChain></MidiTrack></Tracks></LiveSet></Ableton>"""

# A Live 9.6 set's shape: a MIDI track with automated volume, a pan holding only the value before
# the timeline, a lane, two sends and a Drum Rack showing zones.
LIVE_9 = """<Ableton MajorVersion="4" MinorVersion="9.5_326" SchemaChangeCount="1" Creator="Ableton Live 9.6">
<LiveSet><Tracks><MidiTrack Id="7"><Name><EffectiveName Value="Bass" /></Name><ColorIndex Value="3" />
<MidiFoldIn Value="false" /><DeviceChain>
<AutomationLanes><AutomationLanes><AutomationLane><SelectedDevice Value="1" /><SelectedEnvelope Value="2" />
<FadeViewVisible Value="false" /></AutomationLane></AutomationLanes>
<PermanentLanesAreVisible Value="true" /></AutomationLanes>
<Mixer><Sends><TrackSendHolder><Send><Manual Value="1" /></Send></TrackSendHolder>
<TrackSendHolder><Send><Manual Value="0.5" /></Send></TrackSendHolder></Sends>
<Volume><ArrangerAutomation><Events><FloatEvent Time="-63072000" Value="0.5" /><FloatEvent Time="4" Value="1" />
</Events></ArrangerAutomation><Manual Value="0.5" /><AutomationTarget Id="40" />
<ModulationTarget Id="41" /><ModulationAutomation><Events /></ModulationAutomation></Volume>
<Pan><ArrangerAutomation><Events><FloatEvent Time="-63072000" Value="0.25" /></Events></ArrangerAutomation>
<Manual Value="0" /><AutomationTarget Id="42" /></Pan></Mixer>
<DeviceChain><Devices><DrumGroupDevice><MidiTargetShowsZonesInsteadOfNoteNames Value="true" />
</DrumGroupDevice></Devices></DeviceChain>
</DeviceChain></MidiTrack></Tracks></LiveSet></Ableton>"""


def test_a_live_10_0_document_moves_up_dropping_only_its_midi_clips_warp_markers() -> None:
    root = ET.fromstring(LIVE_10_0)
    move = upgrade(root, VST3_SCHEMA)
    assert (root.get("MinorVersion"), root.get("SchemaChangeCount"), root.get("MajorVersion")) == (
        VST3_SCHEMA,
        "10",
        "5",
    )
    midi, audio = root.find(".//MidiClip"), root.find(".//AudioClip")
    assert midi is not None and audio is not None
    assert (midi.find("WarpMarkers"), len(audio.findall("WarpMarkers/WarpMarker"))) == (None, 1)
    assert (move.envelopes, move.removed) == (0, ("MidiClip.WarpMarkers",))


def test_a_class_the_newer_schema_replaced_refuses_the_move_and_changes_nothing() -> None:
    """Live 10.1 replaced CrossDelay with Delay, converting it on reading; nothing here knows how."""
    root = ET.fromstring(LIVE_10_0.replace("<Tracks>", "<CrossDelay /><Tracks>"))
    with pytest.raises(ValueError, match="CrossDelay"):
        upgrade(root, "10.0_377")
    assert root.get("MinorVersion") == "10.0_370"


def test_live_10_0_and_live_9_documents_are_offered_the_move() -> None:
    offered = [vst3_upgrade(version) for version in ("10.0_370", "9.0_305", "9.5_326", "9.5_327")]
    assert offered == [VST3_SCHEMA] * 4
    assert (vst3_upgrade("10.0_377"), vst3_upgrade("8.2_270"), vst3_upgrade("9.9_999")) == (None, None, None)


def test_live_9_arrangement_automation_moves_onto_its_track() -> None:
    root = ET.fromstring(LIVE_9)
    move = upgrade(root, VST3_SCHEMA)
    assert (root.get("MajorVersion"), root.get("MinorVersion")) == ("5", VST3_SCHEMA)
    track = root.find(".//MidiTrack")
    assert track is not None
    assert [child.tag for child in track][:3] == ["Name", "ColorIndex", "AutomationEnvelopes"]
    (envelope,) = track.findall("AutomationEnvelopes/Envelopes/AutomationEnvelope")
    assert envelope.get("Id") == "0"
    assert [p.get("Value") for p in envelope.iter("PointeeId")] == ["40"]
    events = envelope.findall("Automation/Events/FloatEvent")
    assert [(event.get("Id"), event.get("Time"), event.get("Value")) for event in events] == [
        ("0", "-63072000", "0.5"),
        ("1", "4", "1"),
    ]
    # Live 9 writes the value before the timeline into every parameter: alone, it is no automation.
    assert root.find(".//Pan/ArrangerAutomation") is None and root.find(".//Volume/ModulationAutomation") is None
    assert move.envelopes == 1


def test_live_9_lanes_renames_and_lists_take_live_10s_shape() -> None:
    root = ET.fromstring(LIVE_9)
    move = upgrade(root, VST3_SCHEMA)
    (lane,) = root.iter("AutomationLane")
    assert (lane.get("Id"), [child.tag for child in lane]) == ("0", ["SelectedDevice", "SelectedEnvelope"])
    assert root.find(".//DeviceChain/AutomationLanes/PermanentLanesAreVisible") is None
    assert [holder.get("Id") for holder in root.iter("TrackSendHolder")] == ["0", "1"]
    zones = root.find(".//DrumGroupDevice/ShowsZonesInsteadOfNoteNames")
    assert zones is not None and zones.get("Value") == "true"
    assert root.find(".//MidiTrack/MidiFoldIn") is None and "MidiTrack.MidiFoldIn" in move.removed
    counter = root.find("LiveSet/NextPointeeId")
    assert counter is not None and counter.get("Value") == "43"


def test_preview_says_what_upgrade_does_and_changes_nothing() -> None:
    root = ET.fromstring(LIVE_9)
    before = ET.tostring(root)
    said = preview(root, VST3_SCHEMA)
    assert ET.tostring(root) == before
    assert upgrade(root, VST3_SCHEMA) == said


@pytest.mark.parametrize(
    ("version", "device", "rebuilt"),
    [
        ("9.0_305", "<AutoFilter><Cutoff><Manual Value='1' /></Cutoff></AutoFilter>", "AutoFilter"),
        ("9.5_326", "<StereoGain><Panorama><Manual Value='0' /></Panorama></StereoGain>", "StereoGain"),
    ],
)
def test_a_device_live_rebuilt_with_a_legacy_mode_refuses_the_move(version: str, device: str, rebuilt: str) -> None:
    """Live sets a legacy mode when it converts the device itself; nothing here knows its values."""
    text = LIVE_9.replace("9.5_326", version).replace("<DrumGroupDevice", f"{device}<DrumGroupDevice")
    root = ET.fromstring(text)
    assert preview(root, VST3_SCHEMA).refusal == (
        f"the set uses {rebuilt}, which Live's {VST3_SCHEMA} schema rebuilt with a legacy mode nothing here sets"
    )
    with pytest.raises(ValueError, match="legacy mode"):
        upgrade(root, VST3_SCHEMA)
    assert ET.tostring(root) == ET.tostring(ET.fromstring(text))


def test_arrangement_modulation_refuses_the_move() -> None:
    root = ET.fromstring(
        LIVE_9.replace(
            "<Events /></ModulationAutomation>",
            "<Events><FloatEvent Time='0' Value='1' /></Events></ModulationAutomation>",
        )
    )
    assert preview(root, VST3_SCHEMA).refusal == "the set holds arrangement modulation, which Live 10 keeps nowhere"


def test_a_list_numbering_only_some_entries_refuses_the_move() -> None:
    root = ET.fromstring(LIVE_9.replace("<TrackSendHolder>", "<TrackSendHolder Id='4'>", 1))
    assert preview(root, VST3_SCHEMA).refusal == "the set's Sends list numbers only some of its entries"


def test_a_live_9_6_set_becomes_a_document_its_new_schema_declares_throughout() -> None:
    root = live_document.read(SKELETONS / "9.6.0.als")
    move = upgrade(root, VST3_SCHEMA)
    assert move.refusal is None and move.envelopes == 3
    live_document.validate(root)
    schemas = live_schemas()

    def undeclared(element: ET.Element, owner: str | None) -> list[str]:
        found = []
        for child in element:
            kind = None if owner is None else schemas.member_type(VST3_SCHEMA, owner, child.tag)
            if kind is None and owner is not None and schemas.declares_class(VST3_SCHEMA, owner):
                if not schemas.declares_class(VST3_SCHEMA, child.tag):
                    found.append(f"{owner}.{child.tag}")
            held = kind.split("<")[-1].rstrip(">") if kind is not None else child.tag
            found += undeclared(child, held if schemas.declares_class(VST3_SCHEMA, held) else None)
        return found

    assert undeclared(root, None) == []
    pointees = [int(e.get("Value", "")) for e in root.iter("PointeeId")]
    owned = {e.get("Id") for e in root.iter() if owns_pointee_id(e.tag)}
    assert all(str(value) in owned for value in pointees)
    counter = root.find("LiveSet/NextPointeeId")
    assert counter is not None and int(counter.get("Value", "")) > max(int(i or 0) for i in owned)
