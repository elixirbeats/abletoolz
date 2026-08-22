"""``describe()``, checked against ground truth harvested independently.

Pattern dedup counts below were computed by grouping ``clips.midi()`` output
by ``(length, notes)`` signature directly (bypassing ``describe()``), the
same independent-verification approach the rest of the suite uses. Findings:

* 11.0.0 carries 47 MIDI clips (all arrangement) that collapse into exactly
  3 patterns: two runs of near-identical drum hits (4 and 4 clips) and one
  39-clip run of an identical pattern re-placed across the timeline -- this
  fixture is the corpus's own version of the token-waste problem this module
  exists to fix.
* 9.0.1 carries 10 MIDI clips (session and arrangement) collapsing into 3
  patterns.
* The main track (``MasterTrack``/``MainTrack``) carries no ``Id`` attribute
  in any fixture, in any version; ``describe()`` reports ``-1`` for it,
  reusing Live's own "no id" convention (``TrackGroupId`` reads ``-1`` for an
  ungrouped track).
* Audio clips exist in session slots in the corpus (12.4.5b's "2-Drum fill"
  track carries two), but the given schema defines a placement shape for
  audio only in ``arrangement``. ``describe()`` follows that literally:
  ``Clips.audio()`` still returns session audio clips for direct API use,
  but ``session`` in the describe document stays MIDI-only.

``set.describe(level)`` returns a typed ``DescribeDocument`` (frozen
dataclasses); assertions below read its fields directly. The JSON wire shape
(exact keys, dict-keyed patterns) is checked separately through
``describe_json``/``to_wire``.
"""

from __future__ import annotations

import pathlib

import pytest

from abletoolz.live_set import AbletonSet
from abletoolz.live_set.clips import MidiNote, encode_note
from abletoolz.live_set.describe import (
    ArrangementAudioPlacement,
    ArrangementMidiPlacement,
    DescribeLevel,
    SessionPlacement,
    describe_json,
    to_wire,
)

SKELETONS = pathlib.Path(__file__).parent / "version_fixtures" / "skeletons"


def make_set(key: str) -> AbletonSet:
    ableton_set = AbletonSet(SKELETONS / f"{key}.als")
    assert ableton_set.parse()
    return ableton_set


# --- dedup --------------------------------------------------------------


def test_11_0_0_deduplicates_47_clips_into_3_patterns() -> None:
    d = make_set("11.0.0").describe(DescribeLevel.PATTERNS)
    assert d.patterns is not None
    assert len(d.patterns) == 3
    assert d.arrangement is not None and d.session is not None
    midi_placements = [entry for entry in d.arrangement if isinstance(entry, ArrangementMidiPlacement)]
    midi_placements += list(d.session)
    assert len(midi_placements) == 47
    counts: dict[str, int] = {}
    for entry in midi_placements:
        counts[entry.pattern] = counts.get(entry.pattern, 0) + 1
    assert sorted(counts.values()) == [4, 4, 39]


def test_9_0_1_deduplicates_10_clips_into_3_patterns() -> None:
    d = make_set("9.0.1").describe(DescribeLevel.PATTERNS)
    assert d.patterns is not None
    assert len(d.patterns) == 3
    assert d.arrangement is not None and d.session is not None
    midi_placements = [entry for entry in d.arrangement if isinstance(entry, ArrangementMidiPlacement)]
    midi_placements += list(d.session)
    assert len(midi_placements) == 10


def test_two_clips_with_identical_notes_but_different_names_share_one_pattern() -> None:
    """Identity is (length, notes); the name lives on the placement, not the pattern."""
    ableton_set = make_set("12.4.5b")
    donor = next(c for c in ableton_set.clips.midi() if c.name == "")
    ableton_set.clips.clone_clip(donor, slot_index=0, name="a copy with a name", notes=list(donor.notes))

    d = ableton_set.describe(DescribeLevel.PATTERNS)
    assert d.patterns is not None and d.session is not None
    assert len(d.patterns) == 1
    names = {entry.name for entry in d.session}
    assert names == {"", "a copy with a name"}


# --- levels ---------------------------------------------------------------


def test_structure_has_no_patterns_session_or_arrangement() -> None:
    d = make_set("12.4.5b").describe(DescribeLevel.STRUCTURE)
    assert d.patterns is None
    assert d.session is None
    assert d.arrangement is None
    for track in d.tracks:
        assert track.clips is not None


def test_structure_per_track_clip_counts() -> None:
    d = make_set("12.4.5b").describe(DescribeLevel.STRUCTURE)
    counts = {}
    for track in d.tracks:
        assert track.clips is not None
        counts[track.name] = (track.clips.session, track.clips.arrangement)
    assert counts["1-LOW"] == (1, 0)
    assert counts["2-Drum fill"] == (2, 0)
    assert counts["3-Skylark - Iced"] == (1, 0)
    assert counts["Main"] == (0, 0)


def test_patterns_and_full_have_no_clip_counts_on_tracks() -> None:
    for level in (DescribeLevel.PATTERNS, DescribeLevel.FULL):
        d = make_set("12.4.5b").describe(level)
        for track in d.tracks:
            assert track.clips is None


def test_full_keeps_nuance_patterns_silently_drops() -> None:
    """A disabled note with non-default probability: FULL keeps it, PATTERNS folds it away."""
    ableton_set = make_set("12.4.5b")
    donor = next(c for c in ableton_set.clips.midi() if c.name == "")
    ableton_set.clips.set_notes(donor, [MidiNote(60, 0.0, 1.0, enabled=False, probability=0.5), MidiNote(64, 1.0, 1.0)])

    patterns_view = ableton_set.describe(DescribeLevel.PATTERNS)
    full_view = ableton_set.describe(DescribeLevel.FULL)
    assert patterns_view.patterns is not None and full_view.patterns is not None
    (patterns_pattern,) = patterns_view.patterns
    (full_pattern,) = full_view.patterns

    assert list(patterns_pattern.notes) == [[60, 0.0, 1.0], [64, 1.0, 1.0]]
    assert full_pattern.notes[0] == [60, 0.0, 1.0, 100.0, 64.0, {"prob": 0.5, "off": True}]
    assert full_pattern.notes[1] == [64, 1.0, 1.0]


# --- note array shapes ------------------------------------------------------


@pytest.mark.parametrize(
    ("note", "expected"),
    [
        (MidiNote(60, 0.0, 1.0), [60, 0.0, 1.0]),
        (MidiNote(60, 0.0, 1.0, velocity=90.0), [60, 0.0, 1.0, 90.0]),
        (MidiNote(60, 0.0, 1.0, off_velocity=50.0), [60, 0.0, 1.0, 100.0, 50.0]),
        (MidiNote(60, 0.0, 1.0, velocity=90.0, off_velocity=50.0), [60, 0.0, 1.0, 90.0, 50.0]),
    ],
)
def test_pattern_note_array_shapes(note: MidiNote, expected: list[object]) -> None:
    assert encode_note(note, extended=False) == expected
    assert encode_note(note, extended=True) == expected  # no nuance to add


def test_full_note_array_adds_a_trailing_dict_only_when_needed() -> None:
    plain = MidiNote(60, 0.0, 1.0)
    assert encode_note(plain, extended=True) == [60, 0.0, 1.0]

    nuanced = MidiNote(60, 0.0, 1.0, probability=0.25, velocity_deviation=2.0, enabled=False)
    assert encode_note(nuanced, extended=True) == [
        60,
        0.0,
        1.0,
        100.0,
        64.0,
        {"prob": 0.25, "vdev": 2.0, "off": True},
    ]


# --- audio -------------------------------------------------------------


def test_audio_clips_appear_in_arrangement_with_sample_names() -> None:
    d = make_set("11.0.12").describe(DescribeLevel.PATTERNS)
    assert d.arrangement is not None
    audio_entries = [entry for entry in d.arrangement if isinstance(entry, ArrangementAudioPlacement)]
    assert len(audio_entries) == 9
    assert all(entry.sample.endswith(".wav") for entry in audio_entries)


def test_audio_session_clips_are_not_in_the_session_list() -> None:
    """Real audio clips sit in session slots (12.4.5b's '2-Drum fill'), but the
    given schema has no session shape for audio; describe() omits them there."""
    ableton_set = make_set("12.4.5b")
    assert len(ableton_set.clips.audio()) == 3  # Clips.audio() still finds them
    d = ableton_set.describe(DescribeLevel.FULL)
    assert d.session is not None
    assert all(isinstance(entry, SessionPlacement) for entry in d.session)


# --- track entries -------------------------------------------------------


def test_main_track_reports_sentinel_id_and_type_main() -> None:
    d = make_set("12.4.5b").describe(DescribeLevel.STRUCTURE)
    main = next(track for track in d.tracks if track.type == "main")
    assert main.id == -1
    assert main.group_id is None
    assert main.name == "Main"


def test_group_id_is_null_when_ungrouped_and_the_groups_own_id_otherwise() -> None:
    d = make_set("9.0.1").describe(DescribeLevel.STRUCTURE)
    by_name = {track.name: track for track in d.tracks}
    assert by_name["1-Group"].group_id is None
    assert by_name["1-Group"].type == "group"
    assert by_name["2-Drum Rack"].group_id == by_name["1-Group"].id
    assert by_name["3-Drum Rack"].group_id == by_name["1-Group"].id


def test_track_chain_is_display_names_in_chain_order() -> None:
    d = make_set("12.4.5b").describe(DescribeLevel.STRUCTURE)
    by_name = {track.name: track for track in d.tracks}
    assert by_name["1-LOW"].chain == ("LOW", "V-Clip")
    assert by_name["A-Reverb"].chain == ("Reverb",)


def test_session_slot_matches_the_slot_clone_clip_would_reject_as_occupied() -> None:
    """Cross-check against test_clips_write's own knowledge of this fixture's layout."""
    d = make_set("12.4.5b").describe(DescribeLevel.PATTERNS)
    assert d.session is not None
    assert list(d.session) == [SessionPlacement(track_id=13, slot=1, name="", pattern="p0")]


# --- stability and round-trip -----------------------------------------------


def test_describe_json_is_stable_across_calls() -> None:
    ableton_set = make_set("11.0.0")
    first = describe_json(ableton_set, DescribeLevel.FULL)
    second = describe_json(ableton_set, DescribeLevel.FULL)
    assert first == second


def test_describe_json_is_compact_and_preserves_key_order() -> None:
    ableton_set = make_set("12.4.5b")
    encoded = describe_json(ableton_set, DescribeLevel.STRUCTURE)
    assert ", " not in encoded
    assert encoded.startswith('{"set":{"creator":')


def test_to_wire_matches_the_schemas_exact_keys() -> None:
    d = make_set("12.4.5b").describe(DescribeLevel.FULL)
    wire = to_wire(d)
    assert set(wire) == {"set", "tracks", "patterns", "session", "arrangement"}
    assert set(wire["set"]) == {"creator", "major", "bpm"}
    for track in wire["tracks"]:
        assert set(track) == {"id", "name", "type", "group_id", "chain"}
    for entry in wire["session"]:
        assert set(entry) == {"track_id", "slot", "name", "pattern"}


def test_round_trip_describe_apply_describe_reproduces_the_same_pattern() -> None:
    ableton_set = make_set("12.4.5b")
    clip = next(c for c in ableton_set.clips.midi() if c.name == "")
    before = ableton_set.describe(DescribeLevel.PATTERNS)
    assert before.patterns is not None and before.session is not None
    (pattern,) = before.patterns

    ableton_set.clips.set_notes(clip, list(clip.notes))  # no-op rewrite through the same encoding

    after = ableton_set.describe(DescribeLevel.PATTERNS)
    assert after.patterns == before.patterns
    assert after.session is not None
    assert after.session[0].pattern == pattern.id
    assert pattern.notes  # sanity: the fixture actually has notes to round-trip
