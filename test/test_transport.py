"""A set's bars follow its time signatures, the changes along the arrangement included."""

from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest
from conftest import skeleton_set

from abletoolz.live_set.transport import Meter
from abletoolz.schema.upgrade import VST3_SCHEMA, upgrade

FOUR_FOUR, SIX_EIGHT, SEVEN_EIGHT = 201, 302, 303


def test_meters_decode_as_live_encodes_them() -> None:
    decoded = [str(Meter.decode(value)) for value in (FOUR_FOUR, 102, SIX_EIGHT, SEVEN_EIGHT)]
    assert decoded == ["4/4", "4/2", "6/8", "7/8"]
    assert Meter.decode(SEVEN_EIGHT).beats == 3.5


def test_an_unautomated_signature_counts_in_4_4() -> None:
    live_set = skeleton_set("12.2.6")
    assert [(beat, str(meter)) for beat, meter in live_set.transport.meters()] == [(0.0, "4/4")]
    assert live_set.transport.bars(10.0) == 2.5


def test_a_change_along_the_arrangement_counts_each_part_in_its_own_meter() -> None:
    live_set = skeleton_set("12.2.6")
    main = live_set.tracks.main().track_root
    target = main.find("DeviceChain/Mixer/TimeSignature/AutomationTarget")
    assert target is not None
    envelope = ET.SubElement(main.find("AutomationEnvelopes/Envelopes"), "AutomationEnvelope")  # type: ignore[arg-type]
    ET.SubElement(ET.SubElement(envelope, "EnvelopeTarget"), "PointeeId", Value=target.get("Id", ""))
    events = ET.SubElement(ET.SubElement(envelope, "Automation"), "Events")
    for time, value in (("-63072000", FOUR_FOUR), ("16", SIX_EIGHT)):
        ET.SubElement(events, "EnumEvent", Time=time, Value=str(value))

    assert live_set.transport.bars(16.0) == 4  # four bars of 4/4
    assert live_set.transport.bars(25.0) == 7  # then three of 6/8, three beats each


@pytest.mark.parametrize("key", ["9.0.1", "9.6.0"])
def test_a_live_9_set_keeps_its_signature_inside_the_parameter(key: str) -> None:
    live_set = skeleton_set(key)
    (event,) = live_set.tracks.main().track_root.iterfind("DeviceChain/Mixer/TimeSignature/*/Events/EnumEvent")
    event.set("Value", str(SEVEN_EIGHT))
    assert live_set.transport.bars(14.0) == 4


@pytest.mark.parametrize("key", ["9.0.1", "9.6.0"])
def test_a_live_9_set_moved_up_to_hold_a_vst3_keeps_its_tempo_and_bars(key: str) -> None:
    before = skeleton_set(key).transport.length()
    live_set = skeleton_set(key)
    upgrade(live_set.root, VST3_SCHEMA)
    assert live_set.transport.length() == before


def test_the_length_is_the_furthest_beat_at_the_tempo() -> None:
    live_set = skeleton_set("12.2.6")
    length = live_set.transport.length()
    end = max(float(element.get("Value", 0)) for element in live_set.root.iter("CurrentEnd"))
    assert length.seconds == pytest.approx(end / length.bpm * 60)
    assert length.bars == int(end / 4)
