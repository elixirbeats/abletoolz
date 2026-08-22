"""MIDI note authoring, checked against raw XML harvested independently.

Every expectation below was read off the gunzipped fixtures by hand before
any of it was written in code, the same way ``expected.json`` is built. The
findings that shaped the writer:

* ``KeyTrack/@Id`` and ``MidiNoteEvent/@NoteId`` are clip-local. Every clip
  in 10.0.1 restarts KeyTrack ids at 0, every clip in 11.0.0 restarts NoteIds
  at 1, and ``Notes/NoteIdGenerator/NextId`` is that clip's own counter
  (6 for the 11.0.0 clips, which hold ids 1-5). ``KeyTrack/@Id`` does not
  exist before Live 10.
* Live hands out NoteIds in ``(start, pitch)`` order: 12.4.5b's clip reads
  back 1, 2, 3 for the notes at beats 0, 2 and 3.25.
* Pre-11 track clips carry integer velocities. The only float velocities
  anywhere in the 9.x fixtures live in ``GroovePool/Grooves/Groove/Clip``,
  which is a groove template rather than a playable clip.
* An empty session slot is ``ClipSlot/ClipSlot/Value`` with no children.
* ``PerNoteEventStore/EventLists`` is empty in every fixture, so the test
  that proves stale entries are dropped injects one first.
"""

from __future__ import annotations

import gzip
import pathlib
from xml.etree import ElementTree as ET

import pytest

from abletoolz.live_set import AbletonSet
from abletoolz.live_set.clips import ClipLocation, MidiClipRef, MidiNote, parse_pitch_name

SKELETONS = pathlib.Path(__file__).parent / "version_fixtures" / "skeletons"

# C major arpeggio, quarter notes -- the pattern the Live verification files carry.
ARPEGGIO = (
    MidiNote(pitch=60, start=0.0, duration=1.0),
    MidiNote(pitch=64, start=1.0, duration=1.0),
    MidiNote(pitch=67, start=2.0, duration=1.0),
    MidiNote(pitch=72, start=3.0, duration=1.0),
)


def make_set(key: str, tmp_path: pathlib.Path) -> AbletonSet:
    """A writable copy of a fixture, parsed."""
    copy = tmp_path / f"{key}.als"
    copy.write_bytes((SKELETONS / f"{key}.als").read_bytes())
    ableton_set = AbletonSet(copy)
    assert ableton_set.parse()
    return ableton_set


def raw_root(path: pathlib.Path) -> ET.Element:
    """Parse a saved .als straight from disk, bypassing abletoolz entirely."""
    return ET.fromstring(gzip.decompress(path.read_bytes()).decode("utf-8"))


def first_session_clip(ableton_set: AbletonSet) -> MidiClipRef:
    return next(clip for clip in ableton_set.clips.midi() if clip.location is ClipLocation.SESSION)


# --- note replacement -------------------------------------------------------


def test_set_notes_9_0_1_reads_back_with_pre_11_defaults(tmp_path: pathlib.Path) -> None:
    """9.x has no NoteId and stores integer velocities, so that is what comes back."""
    ableton_set = make_set("9.0.1", tmp_path)
    clip = first_session_clip(ableton_set)
    ableton_set.clips.set_notes(clip, ARPEGGIO)

    rewritten = first_session_clip(ableton_set)
    assert rewritten.notes == tuple(
        MidiNote(pitch=pitch, start=start, duration=1.0, velocity=100.0, off_velocity=64.0)
        for pitch, start in ((60, 0.0), (64, 1.0), (67, 2.0), (72, 3.0))
    )


def test_set_notes_11_0_0_keeps_every_expression_field(tmp_path: pathlib.Path) -> None:
    """11.x round-trips probability, velocity deviation and per-note ids."""
    ableton_set = make_set("11.0.0", tmp_path)
    clip = ableton_set.clips.midi()[0]
    notes = (
        MidiNote(pitch=48, start=0.0, duration=0.5, velocity=90.5, off_velocity=12.0, probability=0.25),
        MidiNote(pitch=55, start=1.0, duration=0.25, velocity=101.0, enabled=False, velocity_deviation=-7.5),
    )
    ableton_set.clips.set_notes(clip, notes)

    rewritten = ableton_set.clips.midi()[0]
    assert rewritten.notes == (
        MidiNote(48, 0.0, 0.5, velocity=90.5, off_velocity=12.0, probability=0.25, note_id=1),
        MidiNote(55, 1.0, 0.25, velocity=101.0, enabled=False, velocity_deviation=-7.5, note_id=2),
    )


def test_set_notes_12_4_5b_allocates_note_ids_in_start_pitch_order(tmp_path: pathlib.Path) -> None:
    """Live's own numbering: earliest note gets id 1, ties broken by pitch."""
    ableton_set = make_set("12.4.5b", tmp_path)
    clip = first_session_clip(ableton_set)
    ableton_set.clips.set_notes(clip, ARPEGGIO)

    rewritten = first_session_clip(ableton_set)
    assert [(note.pitch, note.note_id) for note in rewritten.notes] == [(60, 1), (64, 2), (67, 3), (72, 4)]
    notes_element = rewritten.clip_element.find("Notes")
    assert notes_element is not None
    next_id = notes_element.find("NoteIdGenerator/NextId")
    assert next_id is not None
    assert next_id.get("Value") == "5"


def test_set_notes_rebuilds_one_key_track_per_pitch_sorted_by_time(tmp_path: pathlib.Path) -> None:
    """Chord plus a repeated pitch: two KeyTracks, ids from 0, notes in time order."""
    ableton_set = make_set("12.4.5b", tmp_path)
    clip = first_session_clip(ableton_set)
    ableton_set.clips.set_notes(
        clip,
        [
            MidiNote(67, 2.0, 0.5),
            MidiNote(60, 0.0, 0.5),
            MidiNote(60, 1.0, 0.5),
        ],
    )
    key_tracks = clip.clip_element.findall("Notes/KeyTracks/KeyTrack")
    assert [track.get("Id") for track in key_tracks] == ["0", "1"]
    assert [track.find("MidiKey").get("Value") for track in key_tracks if track.find("MidiKey") is not None] == [
        "60",
        "67",
    ]
    starts = [event.get("Time") for event in key_tracks[0].findall("Notes/MidiNoteEvent")]
    assert starts == ["0", "1"]


def test_set_notes_with_no_notes_empties_the_clip(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("12.4.5b", tmp_path)
    clip = first_session_clip(ableton_set)
    ableton_set.clips.set_notes(clip, [])
    assert first_session_clip(ableton_set).notes == ()
    assert clip.clip_element.findall("Notes/KeyTracks/KeyTrack") == []


@pytest.mark.parametrize("key", ["9.0.1", "10.0.1"])
def test_rewriting_a_clips_own_notes_reproduces_lives_xml(key: str, tmp_path: pathlib.Path) -> None:
    """Strongest fidelity check available: pre-11 output is byte-identical."""
    ableton_set = make_set(key, tmp_path)
    clip = first_session_clip(ableton_set)
    before = ET.tostring(clip.clip_element, encoding="unicode")
    ableton_set.clips.set_notes(clip, list(clip.notes))
    assert ET.tostring(clip.clip_element, encoding="unicode") == before


# --- version-native XML shape, harvested by raw walk ------------------------


def test_written_xml_matches_9_0_1_native_note_shape(tmp_path: pathlib.Path) -> None:
    """No NoteId, no Probability, integer velocities, no KeyTrack Id."""
    ableton_set = make_set("9.0.1", tmp_path)
    ableton_set.clips.set_notes(
        first_session_clip(ableton_set),
        [MidiNote(60, 0.0, 1.0, velocity=99.6, off_velocity=64.0)],
    )
    ableton_set.get_file_times()
    ableton_set.save_set()

    root = raw_root(ableton_set.path)
    key_track = root.find(
        "LiveSet/Tracks/MidiTrack/DeviceChain/MainSequencer/ClipSlotList/ClipSlot/ClipSlot/Value/MidiClip"
        "/Notes/KeyTracks/KeyTrack"
    )
    assert key_track is not None
    assert key_track.attrib == {}
    event = key_track.find("Notes/MidiNoteEvent")
    assert event is not None
    assert event.attrib == {
        "Time": "0",
        "Duration": "1",
        "Velocity": "100",
        "OffVelocity": "64",
        "IsEnabled": "true",
    }


def test_written_xml_matches_12_4_5b_native_note_shape(tmp_path: pathlib.Path) -> None:
    """12.x writes NoteId and nothing from the 11-era expression trio."""
    ableton_set = make_set("12.4.5b", tmp_path)
    ableton_set.clips.set_notes(first_session_clip(ableton_set), [MidiNote(60, 0.0, 1.0)])
    ableton_set.get_file_times()
    ableton_set.save_set()

    root = raw_root(ableton_set.path)
    events = [
        event
        for clip in root.iterfind(
            "LiveSet/Tracks/MidiTrack/DeviceChain/MainSequencer/ClipSlotList/ClipSlot/ClipSlot/Value/MidiClip"
        )
        for event in clip.iterfind("Notes/KeyTracks/KeyTrack/Notes/MidiNoteEvent")
    ]
    assert len(events) == 1
    assert events[0].attrib == {
        "Time": "0",
        "Duration": "1",
        "Velocity": "100",
        "OffVelocity": "64",
        "NoteId": "1",
    }


def test_12_x_writes_expression_attributes_only_when_non_default(tmp_path: pathlib.Path) -> None:
    """A disabled, probabilistic note keeps its meaning through 11's attribute names."""
    ableton_set = make_set("12.4.5b", tmp_path)
    clip = first_session_clip(ableton_set)
    ableton_set.clips.set_notes(clip, [MidiNote(60, 0.0, 1.0, enabled=False, probability=0.5, velocity_deviation=3.0)])
    event = clip.clip_element.find("Notes/KeyTracks/KeyTrack/Notes/MidiNoteEvent")
    assert event is not None
    assert list(event.attrib) == [
        "Time",
        "Duration",
        "Velocity",
        "VelocityDeviation",
        "OffVelocity",
        "Probability",
        "IsEnabled",
        "NoteId",
    ]
    assert first_session_clip(ableton_set).notes[0] == MidiNote(
        60, 0.0, 1.0, enabled=False, probability=0.5, velocity_deviation=3.0, note_id=1
    )


def test_save_and_reopen_keeps_the_written_notes(tmp_path: pathlib.Path) -> None:
    """Full persistence path: write, gzip, reopen from disk, read the notes back."""
    ableton_set = make_set("11.0.0", tmp_path)
    ableton_set.clips.set_notes(ableton_set.clips.midi()[0], ARPEGGIO)
    ableton_set.get_file_times()
    ableton_set.save_set()

    reopened = AbletonSet(ableton_set.path)
    assert reopened.parse()
    clip = reopened.clips.midi()[0]
    assert [(note.pitch, note.start, note.duration, note.note_id) for note in clip.notes] == [
        (60, 0.0, 1.0, 1),
        (64, 1.0, 1.0, 2),
        (67, 2.0, 1.0, 3),
        (72, 3.0, 1.0, 4),
    ]


# --- per-note event store ---------------------------------------------------


def test_set_notes_drops_stale_per_note_events(tmp_path: pathlib.Path) -> None:
    """Entries keyed by NoteId cannot outlive the notes they described."""
    ableton_set = make_set("12.4.5b", tmp_path)
    clip = first_session_clip(ableton_set)
    event_lists = clip.clip_element.find("Notes/PerNoteEventStore/EventLists")
    assert event_lists is not None
    assert len(event_lists) == 0  # every fixture ships this empty; synthesize an entry
    ET.SubElement(event_lists, "KeyTrack", {"Id": "0", "NoteId": "1"})
    assert len(event_lists) == 1

    ableton_set.clips.set_notes(clip, ARPEGGIO)
    assert len(event_lists) == 0
    assert ET.tostring(event_lists, encoding="unicode").startswith("<EventLists />")


# --- input validation -------------------------------------------------------


def test_overlapping_same_pitch_notes_are_rejected(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("12.4.5b", tmp_path)
    clip = first_session_clip(ableton_set)
    with pytest.raises(ValueError, match="overlap"):
        ableton_set.clips.set_notes(clip, [MidiNote(60, 0.0, 2.0), MidiNote(60, 1.0, 1.0)])


def test_notes_touching_end_to_start_are_allowed(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("12.4.5b", tmp_path)
    clip = first_session_clip(ableton_set)
    ableton_set.clips.set_notes(clip, [MidiNote(60, 0.0, 1.0), MidiNote(60, 1.0, 1.0)])
    assert len(first_session_clip(ableton_set).notes) == 2


def test_duplicate_preset_note_ids_are_rejected(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("12.4.5b", tmp_path)
    clip = first_session_clip(ableton_set)
    with pytest.raises(ValueError, match="unique"):
        ableton_set.clips.set_notes(clip, [MidiNote(60, 0.0, 1.0, note_id=7), MidiNote(64, 0.0, 1.0, note_id=7)])


def test_preset_note_ids_are_honored_and_the_counter_clears_them(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("12.4.5b", tmp_path)
    clip = first_session_clip(ableton_set)
    ableton_set.clips.set_notes(clip, [MidiNote(60, 0.0, 1.0, note_id=9), MidiNote(64, 1.0, 1.0)])

    rewritten = first_session_clip(ableton_set)
    assert [(note.pitch, note.note_id) for note in rewritten.notes] == [(60, 9), (64, 1)]
    next_id = clip.clip_element.find("Notes/NoteIdGenerator/NextId")
    assert next_id is not None
    assert next_id.get("Value") == "10"


def test_out_of_range_pitch_is_rejected(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("12.4.5b", tmp_path)
    clip = first_session_clip(ableton_set)
    with pytest.raises(ValueError, match="MIDI range"):
        ableton_set.clips.set_notes(clip, [MidiNote(128, 0.0, 1.0)])


# --- cloning ----------------------------------------------------------------


def test_clone_clip_lands_in_the_empty_slot_with_its_own_notes(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("12.4.5b", tmp_path)
    donor = first_session_clip(ableton_set)
    donor_xml = ET.tostring(donor.clip_element, encoding="unicode")

    clone = ableton_set.clips.clone_clip(donor, slot_index=3, name="clone test", notes=ARPEGGIO, length=4.0)
    assert clone.name == "clone test"
    assert clone.location is ClipLocation.SESSION
    assert clone.track_name == donor.track_name
    assert clone.length == 4.0
    assert clone.start_time == 0.0

    session = [clip for clip in ableton_set.clips.midi() if clip.location is ClipLocation.SESSION]
    assert [clip.name for clip in session] == ["", "clone test"]
    assert [note.pitch for note in session[1].notes] == [60, 64, 67, 72]
    assert ET.tostring(donor.clip_element, encoding="unicode") == donor_xml


def test_clone_clip_defaults_to_the_donors_length(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("12.4.5b", tmp_path)
    donor = first_session_clip(ableton_set)
    clone = ableton_set.clips.clone_clip(donor, slot_index=0, name="same length", notes=ARPEGGIO)
    assert clone.length == donor.length


def test_clone_clip_normalizes_an_arrangement_donor_into_a_session_slot(tmp_path: pathlib.Path) -> None:
    """11.0.0 only has arrangement clips; the copy has to start at beat 0."""
    ableton_set = make_set("11.0.0", tmp_path)
    donor = ableton_set.clips.midi()[0]
    assert donor.location is ClipLocation.ARRANGEMENT
    assert donor.start_time == 224.0

    clone = ableton_set.clips.clone_clip(donor, slot_index=0, name="from arrangement", notes=ARPEGGIO, length=4.0)
    assert clone.start_time == 0.0
    assert clone.clip_element.get("Time") == "0"
    assert clone.clip_element.get("Id") == "0"
    current_end = clone.clip_element.find("CurrentEnd")
    out_marker = clone.clip_element.find("Loop/OutMarker")
    assert current_end is not None and current_end.get("Value") == "4"
    assert out_marker is not None and out_marker.get("Value") == "4"


def test_clone_clip_reactivates_and_reloops_the_copy(tmp_path: pathlib.Path) -> None:
    """Placement state does not survive the copy: a dead donor still clones alive.

    No fixture session clip is deactivated or non-looping, so the donor is
    mutated into one first -- exactly the shape a real deactivated or
    one-shot arrangement clip has.
    """
    ableton_set = make_set("12.4.5b", tmp_path)
    donor = first_session_clip(ableton_set)
    disabled = donor.clip_element.find("Disabled")
    loop_on = donor.clip_element.find("Loop/LoopOn")
    assert disabled is not None and loop_on is not None
    disabled.set("Value", "true")
    loop_on.set("Value", "false")

    clone = ableton_set.clips.clone_clip(donor, slot_index=0, name="revived", notes=ARPEGGIO, length=4.0)
    cloned_disabled = clone.clip_element.find("Disabled")
    cloned_loop_on = clone.clip_element.find("Loop/LoopOn")
    assert cloned_disabled is not None and cloned_disabled.get("Value") == "false"
    assert cloned_loop_on is not None and cloned_loop_on.get("Value") == "true"

    assert disabled.get("Value") == "true"  # donor untouched
    assert loop_on.get("Value") == "false"


def test_clone_clip_rejects_an_occupied_slot(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("12.4.5b", tmp_path)
    donor = first_session_clip(ableton_set)
    with pytest.raises(ValueError, match="already holds a clip"):
        ableton_set.clips.clone_clip(donor, slot_index=1, name="nope", notes=ARPEGGIO)


def test_clone_clip_rejects_a_slot_that_does_not_exist(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("12.4.5b", tmp_path)
    donor = first_session_clip(ableton_set)
    with pytest.raises(ValueError, match="no slot 99"):
        ableton_set.clips.clone_clip(donor, slot_index=99, name="nope", notes=ARPEGGIO)


def test_clone_clip_survives_save_and_reopen(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("12.4.5b", tmp_path)
    ableton_set.clips.clone_clip(first_session_clip(ableton_set), slot_index=5, name="saved clone", notes=ARPEGGIO)
    ableton_set.get_file_times()
    ableton_set.save_set()

    root = raw_root(ableton_set.path)
    slots = root.findall(
        "LiveSet/Tracks/MidiTrack/DeviceChain/MainSequencer/ClipSlotList/ClipSlot/ClipSlot/Value/MidiClip"
    )
    names = sorted(name.get("Value", "") for clip in slots for name in clip.iterfind("Name"))
    assert names == ["", "saved clone"]

    reopened = AbletonSet(ableton_set.path)
    assert reopened.parse()
    clone = next(clip for clip in reopened.clips.midi() if clip.name == "saved clone")
    assert [note.pitch for note in clone.notes] == [60, 64, 67, 72]


def collect_ids(root: ET.Element) -> list[tuple[str, int]]:
    """Every set-global id in the tree, harvested by raw walk."""
    owners = {"AutomationTarget", "ModulationTarget", "Pointee"}
    return [
        (node.tag, int(node.attrib["Id"]))
        for node in root.iter()
        if node.tag in owners and node.attrib.get("Id", "").isdigit()
    ]


def test_clone_clip_renumbers_the_ids_it_owns_and_advances_the_counter(tmp_path: pathlib.Path) -> None:
    """No fixture clip owns a set-global id, so the donor gets one injected."""
    ableton_set = make_set("12.4.5b", tmp_path)
    donor = first_session_clip(ableton_set)
    envelopes = donor.clip_element.find("Envelopes/Envelopes")
    assert envelopes is not None
    envelope = ET.SubElement(envelopes, "ClipEnvelope", {"Id": "0"})
    ET.SubElement(ET.SubElement(envelope, "EnvelopeTarget"), "PointeeId", {"Value": "8630"})  # a device parameter
    owned = ET.SubElement(envelope, "AutomationTarget", {"Id": "8631"})
    ET.SubElement(ET.SubElement(owned, "Inner"), "PointeeId", {"Value": "8631"})  # points inside the clip

    counter = ableton_set.root.find("LiveSet/NextPointeeId")
    assert counter is not None
    before = int(counter.get("Value", ""))

    clone = ableton_set.clips.clone_clip(donor, slot_index=0, name="renumbered", notes=ARPEGGIO)

    cloned_target = clone.clip_element.find("Envelopes/Envelopes/ClipEnvelope/AutomationTarget")
    assert cloned_target is not None
    assert cloned_target.get("Id") == str(before)
    assert int(counter.get("Value", "")) == before + 1

    internal = clone.clip_element.find("Envelopes/Envelopes/ClipEnvelope/AutomationTarget/Inner/PointeeId")
    external = clone.clip_element.find("Envelopes/Envelopes/ClipEnvelope/EnvelopeTarget/PointeeId")
    assert internal is not None and internal.get("Value") == str(before)
    assert external is not None and external.get("Value") == "8630"  # untouched: it names a device outside the clip

    assert owned.get("Id") == "8631"  # donor untouched

    # The donor's injected envelope is a test fixture, not Live's data; drop it
    # so the set-wide scan sees only ids a real set would carry.
    envelopes.remove(envelope)
    ids = collect_ids(ableton_set.root)
    assert len(ids) == len({identifier for _, identifier in ids})
    assert int(counter.get("Value", "")) > max(identifier for _, identifier in ids)


def test_clone_clip_leaves_the_counter_alone_when_the_copy_owns_nothing(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("12.4.5b", tmp_path)
    counter = ableton_set.root.find("LiveSet/NextPointeeId")
    assert counter is not None
    before = counter.get("Value")
    ableton_set.clips.clone_clip(first_session_clip(ableton_set), slot_index=0, name="plain", notes=ARPEGGIO)
    assert counter.get("Value") == before

    ids = collect_ids(ableton_set.root)
    assert len(ids) == len({identifier for _, identifier in ids})
    assert int(counter.get("Value", "")) > max(identifier for _, identifier in ids)


def test_clone_clip_works_on_a_pre_pointee_set(tmp_path: pathlib.Path) -> None:
    """9.0.1 has no NextPointeeId element at all; nothing to renumber, nothing to break."""
    ableton_set = make_set("9.0.1", tmp_path)
    assert ableton_set.root.find("LiveSet/NextPointeeId") is None
    donor = first_session_clip(ableton_set)
    clone = ableton_set.clips.clone_clip(donor, slot_index=1, name="old school", notes=ARPEGGIO, length=4.0)
    assert clone.notes[0] == MidiNote(60, 0.0, 1.0, velocity=100.0, off_velocity=64.0)
    assert clone.clip_element.get("Id") is None  # 9.x MidiClip carries no Id attribute


# --- authoring ergonomics ---------------------------------------------------


def test_midi_note_defaults_match_a_freshly_drawn_live_note() -> None:
    note = MidiNote(pitch=60, start=0.0, duration=1.0)
    assert (note.velocity, note.off_velocity, note.enabled) == (100.0, 64.0, True)
    assert (note.probability, note.velocity_deviation, note.note_id) == (1.0, 0.0, None)


def test_from_dict_accepts_a_to_dict_note(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("12.4.5b", tmp_path)
    payload = first_session_clip(ableton_set).to_dict()
    notes = payload["notes"]
    assert isinstance(notes, list)
    assert [MidiNote.from_dict(note) for note in notes] == list(first_session_clip(ableton_set).notes)


def test_from_dict_accepts_a_note_name_instead_of_a_pitch() -> None:
    assert MidiNote.from_dict({"note": "E3", "start": 1.0, "duration": 0.5}) == MidiNote(64, 1.0, 0.5)


def test_from_dict_rejects_a_pitch_and_name_that_disagree() -> None:
    with pytest.raises(ValueError, match="different pitches"):
        MidiNote.from_dict({"pitch": 60, "note": "E3", "start": 0.0, "duration": 1.0})


def test_from_dict_needs_a_pitch_or_a_name() -> None:
    with pytest.raises(ValueError, match="'pitch' or 'note'"):
        MidiNote.from_dict({"start": 0.0, "duration": 1.0})


@pytest.mark.parametrize(
    ("name", "pitch"),
    [("C3", 60), ("c3", 60), ("C-2", 0), ("G8", 127), ("C#3", 61), ("Db3", 61), ("A3", 69)],
)
def test_parse_pitch_name(name: str, pitch: int) -> None:
    assert parse_pitch_name(name) == pitch


@pytest.mark.parametrize("name", ["H3", "C", "C99", "", "C3x"])
def test_parse_pitch_name_rejects_nonsense(name: str) -> None:
    with pytest.raises(ValueError):
        parse_pitch_name(name)
