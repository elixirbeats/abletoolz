"""MIDI clip/note extraction, checked against ground truth harvested by hand.

Ground truth below was read directly off the gunzipped fixture XML (grep and
manual XPath), independent of ``abletoolz.live_set.clips`` -- the same
approach ``expected.json`` uses for the rest of the version matrix.

One finding worth recording: the 12.4.5b fixture's second ``MidiClip`` (of
two present in the raw XML) lives at ``GroovePool/Grooves/Groove/Clip``, not
on any track -- it is the source clip Live keeps around for an extracted
groove template, not a session or arrangement clip. ``Clips.midi()``
deliberately walks tracks only (mirroring ``AbletonTrack.clips_clipview``/
``clips_arrangement``, the same session/arrangement split ``tracks.py``
already established), so that clip is correctly excluded and 12.4.5b yields
exactly one real MidiClipRef.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from abletoolz.live_set import AbletonSet
from abletoolz.live_set.clips import ClipLocation, MidiNote, pitch_name

SKELETONS = pathlib.Path(__file__).parent / "version_fixtures" / "skeletons"


def make_set(key: str) -> AbletonSet:
    ableton_set = AbletonSet(SKELETONS / f"{key}.als")
    assert ableton_set.parse()
    return ableton_set


@pytest.mark.parametrize("key", ["10.1.3", "11.3.41"])
def test_zero_notes_fixtures_return_no_clips(key: str) -> None:
    """These fixtures carry MidiTracks but no MidiClip content at all."""
    assert make_set(key).clips.midi() == []


def test_9_0_1_session_and_arrangement_clips() -> None:
    """Two MidiTracks, each with one session clip and four arrangement clips."""
    clips = make_set("9.0.1").clips.midi()
    assert sum(len(c.notes) for c in clips) == 223
    assert {c.location for c in clips} == {ClipLocation.SESSION, ClipLocation.ARRANGEMENT}

    session_clips = [c for c in clips if c.location is ClipLocation.SESSION]
    assert [(c.track_name, c.start_time, c.length, len(c.notes)) for c in session_clips] == [
        ("2-Drum Rack", 0.0, 16.0, 39),
        ("3-Drum Rack", 0.0, 4.0, 24),
    ]
    # Sorted by (start, pitch): pitch 45 and pitch 49 both start at beat 0, 45 sorts first.
    first_note = session_clips[0].notes[0]
    assert first_note == MidiNote(
        pitch=45,
        start=0.0,
        duration=0.5,
        velocity=100.0,
        off_velocity=64.0,
        enabled=True,
        probability=1.0,
        velocity_deviation=0.0,
        note_id=None,
    )

    arrangement_clips = [c for c in clips if c.location is ClipLocation.ARRANGEMENT]
    first_arrangement = next(c for c in arrangement_clips if c.track_name == "2-Drum Rack" and c.start_time == 0.0)
    assert first_arrangement.length == 16.0
    assert len(first_arrangement.notes) == 16
    assert first_arrangement.notes[0] == MidiNote(
        pitch=46,
        start=0.0,
        duration=0.5,
        velocity=100.0,
        off_velocity=64.0,
        enabled=True,
        probability=1.0,
        velocity_deviation=0.0,
        note_id=None,
    )


def test_11_0_0_note_fields_float_velocity_and_note_id() -> None:
    """11.0+ shape: NoteId, Probability, VelocityDeviation, and float velocities."""
    clips = make_set("11.0.0").clips.midi()
    assert sum(len(c.notes) for c in clips) == 219
    clip = next(c for c in clips if c.track_name == "Kick" and c.start_time == 224.0)
    assert clip.location is ClipLocation.ARRANGEMENT
    assert clip.length == 2.0
    assert len(clip.notes) == 5
    assert clip.notes[0] == MidiNote(
        pitch=61,
        start=0.0,
        duration=0.727945492007991968,
        velocity=100.154907,
        off_velocity=0.0,
        enabled=True,
        probability=1.0,
        velocity_deviation=0.0,
        note_id=1,
    )


def test_12_4_5b_excludes_groove_pool_clip() -> None:
    """Only the session clip on the surviving MidiTrack counts; the GroovePool
    clip belongs to no track and is not a real session/arrangement clip."""
    clips = make_set("12.4.5b").clips.midi()
    assert len(clips) == 1
    clip = clips[0]
    assert clip.track_name == "1-LOW"
    assert clip.location is ClipLocation.SESSION
    assert clip.start_time == 0.0
    assert clip.length == 4.0
    assert clip.notes == (
        MidiNote(
            pitch=70,
            start=0.0,
            duration=0.25,
            velocity=100.0,
            off_velocity=64.0,
            enabled=True,
            probability=1.0,
            velocity_deviation=0.0,
            note_id=1,
        ),
        MidiNote(
            pitch=64,
            start=2.0,
            duration=0.25,
            velocity=100.0,
            off_velocity=64.0,
            enabled=True,
            probability=1.0,
            velocity_deviation=0.0,
            note_id=2,
        ),
        MidiNote(
            pitch=70,
            start=3.25,
            duration=0.25,
            velocity=100.0,
            off_velocity=64.0,
            enabled=True,
            probability=1.0,
            velocity_deviation=0.0,
            note_id=3,
        ),
    )


def test_to_dict_is_json_ready_and_flat() -> None:
    """to_dict() gives both pitch number and note name, and round-trips through json."""
    clip = make_set("12.4.5b").clips.midi()[0]
    data = clip.to_dict()
    encoded = json.dumps(data)
    decoded = json.loads(encoded)
    assert decoded["track_name"] == "1-LOW"
    assert decoded["location"] == "session"
    assert decoded["start_time"] == 0.0
    assert decoded["length"] == 4.0
    assert [note["pitch"] for note in decoded["notes"]] == [70, 64, 70]
    assert [note["note"] for note in decoded["notes"]] == ["A#3", "E3", "A#3"]
    assert decoded["notes"][1]["note_id"] == 2
    assert decoded["notes"][0]["enabled"] is True


@pytest.mark.parametrize(
    ("pitch", "name"),
    [
        (60, "C3"),  # Ableton's middle C.
        (0, "C-2"),
        (127, "G8"),
        (61, "C#3"),
        (69, "A3"),
    ],
)
def test_pitch_name(pitch: int, name: str) -> None:
    assert pitch_name(pitch) == name
