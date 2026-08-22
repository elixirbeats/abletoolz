"""Arrangement placement and section copying, checked against harvested XML.

Every expectation below was read off the gunzipped fixtures by hand before
any of it was written in code. The findings that shaped the writer, each
with the fixture that proved it:

* ``Time`` equals ``CurrentStart`` on all 404 arrangement events in the
  corpus (every skeleton plus a real 12.4.5b9 set), so a clip occupies
  ``[Time, CurrentEnd)``.
* ``Events`` children are always in non-decreasing ``Time`` order --
  including 11.0.12's MIDI track, whose ids run 0, 9, 5, 10, 11, 13 down the
  list while its times run 96, 128, 144, 152, 168, 192.
* ``Id`` is unique inside one ``Events`` list, sparse, and starts wherever
  Live felt like: 0 in 10.0.6 and 11.0.12, 1 in 10.1.3. 9.0.1 clips have no
  ``Id`` attribute at all.
* Every ``LoopOn="false"`` event in the corpus has
  ``LoopEnd - LoopStart == CurrentEnd - CurrentStart``, 0 exceptions.
* Automation events (``FloatEvent``/``BoolEvent``/``EnumEvent``) are leaves
  carrying ``Id``, ``Time``, ``Value`` and nothing else -- no curve or shape
  attributes exist anywhere in the corpus. 11.0.0's group track holds 71
  FloatEvents, the densest envelope available; 10.1.3 writes its 57
  BoolEvents on a single line, which is why insertion re-indents one seam
  instead of the whole list.
* Only 11.0.0, 11.0.12, 10.x and 9.x skeletons carry arrangements at all;
  the 11.2+ and 12.x skeletons are empty, so section tests live on the
  older ones.
"""

from __future__ import annotations

import gzip
import pathlib
from xml.etree import ElementTree as ET

import pytest

from abletoolz.live_set import AbletonSet
from abletoolz.live_set.clips import AudioClipRef, ClipLocation, MidiClipRef
from abletoolz.live_set.sections import SectionBoundary, SectionMode

SKELETONS = pathlib.Path(__file__).parent / "version_fixtures" / "skeletons"


def make_set(key: str, tmp_path: pathlib.Path) -> AbletonSet:
    """A writable copy of a fixture, parsed."""
    copied = tmp_path / f"{key}.als"
    copied.write_bytes((SKELETONS / f"{key}.als").read_bytes())
    ableton_set = AbletonSet(copied)
    assert ableton_set.parse()
    return ableton_set


def arrangement_midi(ableton_set: AbletonSet, track_name: str) -> list[MidiClipRef]:
    return [
        clip
        for clip in ableton_set.clips.midi()
        if clip.location is ClipLocation.ARRANGEMENT and clip.track_name == track_name
    ]


def arrangement_audio(ableton_set: AbletonSet, track_name: str) -> list[AudioClipRef]:
    return [
        clip
        for clip in ableton_set.clips.audio()
        if clip.location is ClipLocation.ARRANGEMENT and clip.track_name == track_name
    ]


def events_of(ableton_set: AbletonSet, track_name: str, path: str) -> ET.Element:
    track = next(candidate for candidate in ableton_set.tracks.load() if candidate.name == track_name)
    found = track.track_root.find(path)
    assert found is not None
    return found


def midi_events(ableton_set: AbletonSet, track_name: str) -> ET.Element:
    return events_of(ableton_set, track_name, "DeviceChain/MainSequencer/ClipTimeable/ArrangerAutomation/Events")


def times_of(events: ET.Element) -> list[float]:
    return [float(child.get("Time", "0")) for child in events]


def raw_xml(path: pathlib.Path) -> str:
    return gzip.decompress(path.read_bytes()).decode("utf-8")


# --- place_clip -------------------------------------------------------------


def test_place_clip_creates_a_readable_arrangement_event(tmp_path: pathlib.Path) -> None:
    """11.0.0's Kick track: 47 clips, the first spanning 224-226 with 5 notes."""
    ableton_set = make_set("11.0.0", tmp_path)
    donor = arrangement_midi(ableton_set, "Kick")[0]
    assert (donor.start_time, len(donor.notes)) == (224.0, 5)

    placed = ableton_set.clips.place_clip(donor, track="Kick", at=900.0)
    assert isinstance(placed, MidiClipRef)
    assert placed.location is ClipLocation.ARRANGEMENT
    assert placed.start_time == 900.0
    assert [note.pitch for note in placed.notes] == [note.pitch for note in donor.notes]

    read_back = next(clip for clip in arrangement_midi(ableton_set, "Kick") if clip.start_time == 900.0)
    assert read_back.notes == placed.notes
    assert read_back.clip_element.get("Time") == "900"
    current_start = read_back.clip_element.find("CurrentStart")
    current_end = read_back.clip_element.find("CurrentEnd")
    assert current_start is not None and current_start.get("Value") == "900"
    assert current_end is not None and current_end.get("Value") == "902"


def test_place_clip_keeps_the_events_list_time_ordered(tmp_path: pathlib.Path) -> None:
    """The list invariant holds wherever the new clip lands -- front, middle, end."""
    ableton_set = make_set("11.0.0", tmp_path)
    donor = arrangement_midi(ableton_set, "Kick")[0]
    for beat in (900.0, 8.0, 410.0):  # 408-416 is the one gap in the Kick run
        ableton_set.clips.place_clip(donor, track="Kick", at=beat)
    times = times_of(midi_events(ableton_set, "Kick"))
    assert times == sorted(times)
    assert times[0] == 8.0
    assert times[-1] == 900.0
    assert 410.0 in times


def test_place_clip_takes_a_fresh_list_local_id(tmp_path: pathlib.Path) -> None:
    """Ids are sparse and list-local, so max + 1 is the only rule the corpus gives."""
    ableton_set = make_set("11.0.0", tmp_path)
    events = midi_events(ableton_set, "Kick")
    highest = max(int(child.attrib["Id"]) for child in events)
    donor = arrangement_midi(ableton_set, "Kick")[0]

    placed = ableton_set.clips.place_clip(donor, track="Kick", at=900.0)
    assert placed.clip_element.get("Id") == str(highest + 1)
    ids = [child.attrib["Id"] for child in events]
    assert len(set(ids)) == len(ids)


def test_place_clip_writes_no_id_on_a_pre_10_set(tmp_path: pathlib.Path) -> None:
    """9.0.1 arrangement clips carry no Id attribute; the copy must not invent one."""
    ableton_set = make_set("9.0.1", tmp_path)
    donor = arrangement_midi(ableton_set, "2-Drum Rack")[0]
    placed = ableton_set.clips.place_clip(donor, track="2-Drum Rack", at=512.0)
    assert placed.clip_element.get("Id") is None


def test_place_clip_reactivates_but_keeps_loop_state(tmp_path: pathlib.Path) -> None:
    """A one-shot arrangement clip stays a one-shot; a deactivated one comes back on."""
    ableton_set = make_set("11.0.0", tmp_path)
    donor = arrangement_midi(ableton_set, "Kick")[0]
    disabled = donor.clip_element.find("Disabled")
    loop_on = donor.clip_element.find("Loop/LoopOn")
    assert disabled is not None and loop_on is not None
    assert loop_on.get("Value") == "false"  # 11.0.0's Kick clips are one-shots
    disabled.set("Value", "true")

    placed = ableton_set.clips.place_clip(donor, track="Kick", at=900.0)
    placed_disabled = placed.clip_element.find("Disabled")
    placed_loop = placed.clip_element.find("Loop/LoopOn")
    assert placed_disabled is not None and placed_disabled.get("Value") == "false"
    assert placed_loop is not None and placed_loop.get("Value") == "false"


def test_place_clip_resizing_a_non_looping_clip_carries_loop_end(tmp_path: pathlib.Path) -> None:
    """LoopEnd - LoopStart == CurrentEnd - CurrentStart on every non-looping clip."""
    ableton_set = make_set("11.0.0", tmp_path)
    donor = arrangement_midi(ableton_set, "Kick")[0]
    placed = ableton_set.clips.place_clip(donor, track="Kick", at=900.0, length=8.0)

    loop_start = placed.clip_element.find("Loop/LoopStart")
    loop_end = placed.clip_element.find("Loop/LoopEnd")
    current_end = placed.clip_element.find("CurrentEnd")
    assert loop_start is not None and loop_end is not None and current_end is not None
    assert current_end.get("Value") == "908"
    assert float(loop_end.get("Value", "")) - float(loop_start.get("Value", "")) == 8.0


def test_place_clip_leaves_loop_markers_alone_on_a_looping_clip(tmp_path: pathlib.Path) -> None:
    """A looping clip repeats to fill its span; its markers are clip-internal time."""
    ableton_set = make_set("10.0.6", tmp_path)
    donor = arrangement_midi(ableton_set, "2-Drum Rack")[0]
    before = ET.tostring(donor.clip_element.find("Loop"), encoding="unicode")  # type: ignore[arg-type]

    placed = ableton_set.clips.place_clip(donor, track="2-Drum Rack", at=1024.0, length=128.0)
    assert ET.tostring(placed.clip_element.find("Loop"), encoding="unicode") == before  # type: ignore[arg-type]


def test_place_clip_refuses_an_occupied_span(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("11.0.0", tmp_path)
    donor = arrangement_midi(ableton_set, "Kick")[0]
    with pytest.raises(ValueError, match="already hold"):
        ableton_set.clips.place_clip(donor, track="Kick", at=240.0)


def test_place_clip_replace_deletes_the_clip_it_lands_on(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("11.0.0", tmp_path)
    donor = arrangement_midi(ableton_set, "Kick")[0]
    before = len(arrangement_midi(ableton_set, "Kick"))

    ableton_set.clips.place_clip(donor, track="Kick", at=240.0, replace=True)
    after = arrangement_midi(ableton_set, "Kick")
    assert len(after) == before  # one deleted, one placed
    assert len([clip for clip in after if clip.start_time == 240.0]) == 1


def test_place_clip_never_trims_a_straddler(tmp_path: pathlib.Path) -> None:
    """Landing across an existing clip's edge is an error, not a trim, either way."""
    ableton_set = make_set("11.0.0", tmp_path)
    donor = arrangement_midi(ableton_set, "Kick")[0]
    with pytest.raises(ValueError, match="never trimmed"):
        ableton_set.clips.place_clip(donor, track="Kick", at=225.0, replace=True)


def test_place_clip_rejects_the_wrong_track_kind(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("11.0.0", tmp_path)
    donor = arrangement_midi(ableton_set, "Kick")[0]
    with pytest.raises(ValueError, match="needs a MidiTrack"):
        ableton_set.clips.place_clip(donor, track="2-Audio", at=900.0)


def test_place_clip_places_an_audio_donor_the_same_way(tmp_path: pathlib.Path) -> None:
    """Audio and MIDI arrangement events are the same shape, so they share the path."""
    ableton_set = make_set("11.0.12", tmp_path)
    donor = arrangement_audio(ableton_set, "snr2")[0]
    placed = ableton_set.clips.place_clip(donor, track="snr2", at=900.0)
    assert isinstance(placed, AudioClipRef)
    assert placed.sample_name == donor.sample_name
    assert placed.start_time == 900.0

    events = events_of(ableton_set, "snr2", "DeviceChain/MainSequencer/Sample/ArrangerAutomation/Events")
    times = times_of(events)
    assert times == sorted(times)
    assert 900.0 in times


def test_place_clip_accepts_a_session_donor(tmp_path: pathlib.Path) -> None:
    """10.0.1's Bass track carries both, so a session clip can go to the timeline."""
    ableton_set = make_set("10.0.1", tmp_path)
    donor = next(clip for clip in ableton_set.clips.midi() if clip.location is ClipLocation.SESSION)
    placed = ableton_set.clips.place_clip(donor, track=str(donor.track_name), at=2000.0, length=16.0)
    assert placed.start_time == 2000.0
    assert placed.location is ClipLocation.ARRANGEMENT
    assert [note.pitch for note in placed.notes] == [note.pitch for note in donor.notes]


def test_place_clip_across_sets_of_the_same_major_version(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("11.0.0", tmp_path)
    other = make_set("11.0.12", tmp_path)
    donor = arrangement_midi(other, "8-Drums to MIDI")[0]

    placed = ableton_set.clips.place_clip(donor, track="Kick", at=900.0)
    assert placed.start_time == 900.0
    assert placed.name == donor.name


def test_place_clip_refuses_a_donor_from_another_major_version(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("11.0.0", tmp_path)
    other = make_set("10.0.6", tmp_path)
    donor = arrangement_midi(other, "2-Drum Rack")[0]
    with pytest.raises(ValueError, match="only known to match within a major version"):
        ableton_set.clips.place_clip(donor, track="Kick", at=900.0)


def test_place_clip_survives_save_and_reopen(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("11.0.0", tmp_path)
    ableton_set.clips.place_clip(arrangement_midi(ableton_set, "Kick")[0], track="Kick", at=900.0)
    ableton_set.get_file_times()
    ableton_set.save_set()

    reopened = AbletonSet(ableton_set.path)
    assert reopened.parse()
    placed = next(clip for clip in arrangement_midi(reopened, "Kick") if clip.start_time == 900.0)
    assert len(placed.notes) == 5


# --- copy_section -----------------------------------------------------------


def test_copy_section_moves_a_whole_stretch(tmp_path: pathlib.Path) -> None:
    """11.0.12's arrangement: 4 MIDI clips and 1 audio clip sit inside 128-192."""
    ableton_set = make_set("11.0.12", tmp_path)
    report = ableton_set.clips.copy_section(128.0, 192.0, 384.0)

    assert [placement.start for placement in report.copied] == [384.0, 384.0, 400.0, 408.0, 424.0]
    assert {placement.track_name for placement in report.copied} == {"snr2", "8-Drums to MIDI"}
    assert report.replaced == 0
    assert report.skipped == ()

    midi_starts = [clip.start_time for clip in arrangement_midi(ableton_set, "8-Drums to MIDI")]
    assert [start for start in midi_starts if start >= 384.0] == [384.0, 400.0, 408.0, 424.0]
    assert 384.0 in [clip.start_time for clip in arrangement_audio(ableton_set, "snr2")]


def test_copy_section_keeps_every_events_list_time_ordered(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("11.0.12", tmp_path)
    # 400 lands mid-list on the audio track (between 320-380.5 and 512) and
    # at the end of the MIDI list, exercising both insertion seams.
    ableton_set.clips.copy_section(128.0, 192.0, 400.0)
    for track_name, path in (
        ("8-Drums to MIDI", "DeviceChain/MainSequencer/ClipTimeable/ArrangerAutomation/Events"),
        ("snr2", "DeviceChain/MainSequencer/Sample/ArrangerAutomation/Events"),
    ):
        times = times_of(events_of(ableton_set, track_name, path))
        assert times == sorted(times)


def test_copy_section_reports_straddlers_instead_of_cutting_them(tmp_path: pathlib.Path) -> None:
    """11.0.12's MIDI clip at 128-144 crosses a section starting at 136."""
    ableton_set = make_set("11.0.12", tmp_path)
    report = ableton_set.clips.copy_section(136.0, 160.0, 700.0)

    boundaries = {
        (straddler.clip_name, straddler.start, straddler.end, straddler.boundary) for straddler in report.skipped
    }
    assert ("p01587", 128.0, 144.0, SectionBoundary.START) in boundaries
    assert ("p01587", 144.0, 152.0, SectionBoundary.END) not in boundaries  # 144-152 is wholly inside
    assert ("p75576", 128.0, 190.0, SectionBoundary.BOTH) in boundaries
    assert all(placement.start >= 700.0 for placement in report.copied)


def test_copy_section_restricted_to_named_tracks(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("11.0.12", tmp_path)
    report = ableton_set.clips.copy_section(128.0, 192.0, 384.0, track_names=["8-Drums to MIDI"])
    assert {placement.track_name for placement in report.copied} == {"8-Drums to MIDI"}
    assert 384.0 not in [clip.start_time for clip in arrangement_audio(ableton_set, "snr2")]


def test_copy_section_rejects_an_unknown_track_name(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("11.0.12", tmp_path)
    with pytest.raises(ValueError, match="No track named"):
        ableton_set.clips.copy_section(128.0, 192.0, 384.0, track_names=["nope"])


def test_copy_section_refuses_an_occupied_destination(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("11.0.12", tmp_path)
    with pytest.raises(ValueError, match="already holds"):
        ableton_set.clips.copy_section(128.0, 192.0, 192.0, track_names=["8-Drums to MIDI"])


def test_refused_copy_section_leaves_the_file_byte_identical(tmp_path: pathlib.Path) -> None:
    """A refusal is a no-op: nothing at all is written before the check runs."""
    ableton_set = make_set("11.0.12", tmp_path)
    ableton_set.get_file_times()
    ableton_set.save_set()
    before = raw_xml(ableton_set.path)

    with pytest.raises(ValueError, match="already holds"):
        ableton_set.clips.copy_section(128.0, 192.0, 192.0, track_names=["8-Drums to MIDI"])
    ableton_set.save_set()
    assert raw_xml(ableton_set.path) == before


def test_copy_section_replace_deletes_what_it_lands_on(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("11.0.12", tmp_path)
    report = ableton_set.clips.copy_section(
        128.0, 192.0, 192.0, track_names=["8-Drums to MIDI"], mode=SectionMode.REPLACE
    )
    assert report.replaced == 2  # 192-208 and 208-254 sit inside 192-256; 256-272 does not
    starts = [clip.start_time for clip in arrangement_midi(ableton_set, "8-Drums to MIDI")]
    assert [start for start in starts if start >= 192.0] == [192.0, 208.0, 216.0, 232.0, 256.0, 272.0]


def test_copy_section_never_trims_at_the_destination(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("11.0.12", tmp_path)
    with pytest.raises(ValueError, match="never trimmed"):
        ableton_set.clips.copy_section(96.0, 128.0, 136.0, track_names=["8-Drums to MIDI"], mode=SectionMode.REPLACE)


def test_copy_section_into_its_own_source_range(tmp_path: pathlib.Path) -> None:
    """Overlapping source and destination still copies what the source started with.

    11.0.0's Kick holds 224-226, 240-242, 256-258, 272-274 in the source
    range; the destination 232-288 swallows three of those same clips, so
    the copy at 248 can only exist if its donor was taken before the
    deletion that removed it.
    """
    ableton_set = make_set("11.0.0", tmp_path)
    before = [clip.start_time for clip in arrangement_midi(ableton_set, "Kick")]
    report = ableton_set.clips.copy_section(224.0, 280.0, 232.0, track_names=["Kick"], mode=SectionMode.REPLACE)
    assert len(report.copied) == 4
    assert report.replaced == 3

    starts = [clip.start_time for clip in arrangement_midi(ableton_set, "Kick")]
    assert [start for start in starts if start not in before] == [232.0, 248.0, 264.0, 280.0]


def test_copy_section_leaves_untouched_tracks_byte_identical(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("11.0.12", tmp_path)
    audio_events = events_of(ableton_set, "snr2", "DeviceChain/MainSequencer/Sample/ArrangerAutomation/Events")
    before = ET.tostring(audio_events, encoding="unicode")

    ableton_set.clips.copy_section(128.0, 192.0, 384.0, track_names=["8-Drums to MIDI"])
    assert ET.tostring(audio_events, encoding="unicode") == before


def test_copy_section_rejects_a_backwards_range(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("11.0.12", tmp_path)
    with pytest.raises(ValueError, match="must be after"):
        ableton_set.clips.copy_section(192.0, 128.0, 384.0)


def test_copy_section_survives_save_and_reopen(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("11.0.12", tmp_path)
    ableton_set.clips.copy_section(128.0, 192.0, 384.0)
    ableton_set.get_file_times()
    ableton_set.save_set()

    reopened = AbletonSet(ableton_set.path)
    assert reopened.parse()
    starts = [clip.start_time for clip in arrangement_midi(reopened, "8-Drums to MIDI")]
    assert [384.0, 400.0, 408.0, 424.0] == [start for start in starts if start >= 384.0]


# --- automation envelopes ---------------------------------------------------


def envelope_events(ableton_set: AbletonSet, track_name: str) -> ET.Element:
    return events_of(ableton_set, track_name, "AutomationEnvelopes/Envelopes/AutomationEnvelope/Automation/Events")


def test_copy_section_copies_track_automation(tmp_path: pathlib.Path) -> None:
    """11.0.0's Drums group holds one envelope of 71 FloatEvents.

    Harvested by hand: events at 224, 224, 290, 290, 296, 296, 298, 298 open
    the list after the ``-63072000`` sentinel, so 224-300 holds exactly 8.
    """
    ableton_set = make_set("11.0.0", tmp_path)
    events = envelope_events(ableton_set, "Drums")
    assert [float(child.get("Time", "")) for child in events][:5] == [-63072000.0, 224.0, 224.0, 290.0, 290.0]
    before = len(events)

    report = ableton_set.clips.copy_section(224.0, 300.0, 1000.0, track_names=["Drums"])
    assert report.envelope_events == 8
    assert len(events) == before + 8

    times = [float(child.get("Time", "")) for child in events]
    assert times == sorted(times)
    assert [time for time in times if time >= 1000.0] == [
        1000.0,
        1000.0,
        1066.0,
        1066.0,
        1072.0,
        1072.0,
        1074.0,
        1074.0,
    ]


def test_copied_automation_keeps_its_values_and_takes_fresh_ids(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("11.0.0", tmp_path)
    events = envelope_events(ableton_set, "Drums")
    highest = max(int(child.attrib["Id"]) for child in events)
    sources = [child for child in events if 224.0 <= float(child.get("Time", "")) < 300.0]
    values = [child.get("Value") for child in sources]

    ableton_set.clips.copy_section(224.0, 300.0, 1000.0, track_names=["Drums"])
    copied = [child for child in events if float(child.get("Time", "")) >= 1000.0]
    assert [child.get("Value") for child in copied] == values
    assert all(int(child.attrib["Id"]) > highest for child in copied)
    ids = [child.attrib["Id"] for child in events]
    assert len(set(ids)) == len(ids)


def test_the_pre_timeline_sentinel_is_never_copied(tmp_path: pathlib.Path) -> None:
    """Every envelope opens on Time=-63072000; no real section can reach it."""
    ableton_set = make_set("11.0.0", tmp_path)
    events = envelope_events(ableton_set, "Drums")
    report = ableton_set.clips.copy_section(0.0, 224.0, 2000.0, track_names=["Drums"])
    assert report.envelope_events == 0
    assert [child.get("Time") for child in events].count("-63072000") == 1


def test_envelope_automation_written_on_one_line_stays_that_way(tmp_path: pathlib.Path) -> None:
    """10.1.3 writes its 57 BoolEvents inline; only the insertion seam changes."""
    ableton_set = make_set("10.1.3", tmp_path)
    events = envelope_events(ableton_set, "kick sparkle")
    assert all(child.tail is None for child in list(events)[:-1])

    ableton_set.clips.copy_section(32.0, 64.0, 2000.0, track_names=["kick sparkle"])
    assert all(child.tail is None for child in list(events)[:-1])
    times = [float(child.get("Time", "")) for child in events]
    assert times == sorted(times)


def test_a_track_with_no_envelope_container_is_left_alone(tmp_path: pathlib.Path) -> None:
    """9.0.1 has no AutomationEnvelopes element at all; automation lives inline."""
    ableton_set = make_set("9.0.1", tmp_path)
    assert ableton_set.root.find("LiveSet/Tracks/MidiTrack/AutomationEnvelopes") is None
    report = ableton_set.clips.copy_section(0.0, 64.0, 512.0, track_names=["2-Drum Rack"])
    assert report.envelope_events == 0
    assert len(report.copied) == 1


# --- remove_section_clips ---------------------------------------------------


def test_remove_section_clips_deletes_only_what_is_inside(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("11.0.12", tmp_path)
    lines = ableton_set.clips.remove_section_clips(128.0, 192.0, track_names=["8-Drums to MIDI"])
    assert len([line for line in lines if line.startswith("deleted")]) == 4

    starts = [clip.start_time for clip in arrangement_midi(ableton_set, "8-Drums to MIDI")]
    assert starts == [96.0, 192.0, 208.0, 256.0, 272.0]


def test_remove_section_clips_reports_and_keeps_straddlers(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("11.0.12", tmp_path)
    lines = ableton_set.clips.remove_section_clips(136.0, 160.0, track_names=["8-Drums to MIDI"])
    assert any("kept" in line and "crosses the section start" in line for line in lines)
    assert 128.0 in [clip.start_time for clip in arrangement_midi(ableton_set, "8-Drums to MIDI")]


def test_remove_section_clips_touches_no_other_track(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("11.0.12", tmp_path)
    audio_events = events_of(ableton_set, "snr2", "DeviceChain/MainSequencer/Sample/ArrangerAutomation/Events")
    before = ET.tostring(audio_events, encoding="unicode")
    ableton_set.clips.remove_section_clips(128.0, 192.0, track_names=["8-Drums to MIDI"])
    assert ET.tostring(audio_events, encoding="unicode") == before


def test_removing_every_clip_leaves_a_self_closing_list(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("11.0.12", tmp_path)
    ableton_set.clips.remove_section_clips(0.0, 10000.0, track_names=["8-Drums to MIDI"])
    events = midi_events(ableton_set, "8-Drums to MIDI")
    assert len(events) == 0
    assert ET.tostring(events, encoding="unicode").startswith("<Events />")


def test_remove_then_place_reuses_the_empty_list(tmp_path: pathlib.Path) -> None:
    """An emptied Events list has no children to derive indentation from."""
    ableton_set = make_set("11.0.12", tmp_path)
    ableton_set.clips.remove_section_clips(0.0, 10000.0, track_names=["8-Drums to MIDI"])

    second = tmp_path / "second"
    second.mkdir()
    source = arrangement_midi(make_set("11.0.12", second), "8-Drums to MIDI")[0]
    ableton_set.clips.place_clip(source, track="8-Drums to MIDI", at=64.0)

    events = midi_events(ableton_set, "8-Drums to MIDI")
    assert len(events) == 1
    assert events.text == (events[0].tail or "") + "\t"


def test_remove_section_clips_rejects_a_backwards_range(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("11.0.12", tmp_path)
    with pytest.raises(ValueError, match="must be after"):
        ableton_set.clips.remove_section_clips(192.0, 128.0, track_names=["8-Drums to MIDI"])
