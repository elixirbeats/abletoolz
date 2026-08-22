"""``apply_ops``, checked against the same fixtures/knowledge as the write-domain tests.

Findings that shaped the resolver, independent of anything already proven by
``test_clips_write.py``/``test_devices_write.py`` (which cover the domain
writers this module calls):

* 12.4.5b's "1-LOW" track holds its only MIDI clip in session slot 1 (slot 0
  is empty) -- the same fact ``test_clone_clip_rejects_an_occupied_slot``
  already relies on, cross-checked here through the ops surface instead.
* Track selection by name has to raise on ambiguity rather than guess:
  nothing in the corpus carries duplicate track names, so the ambiguity
  tests rename a track in memory to manufacture one, the same technique
  ``test_devices_write.py`` uses to manufacture ``PointeeId`` references
  that don't occur naturally either.
"""

from __future__ import annotations

import pathlib
from xml.etree import ElementTree as ET

import pydantic
import pytest

from abletoolz.live_set import AbletonSet
from abletoolz.live_set.apply_ops import OpsDocument, TrackByName, _resolve_track, apply_ops
from abletoolz.live_set.clips import ClipLocation, MidiClipRef, encode_note

SKELETONS = pathlib.Path(__file__).parent / "version_fixtures" / "skeletons"


def make_set(key: str, tmp_path: pathlib.Path) -> AbletonSet:
    """A writable copy of a fixture, parsed."""
    copy = tmp_path / f"{key}.als"
    copy.write_bytes((SKELETONS / f"{key}.als").read_bytes())
    ableton_set = AbletonSet(copy)
    assert ableton_set.parse()
    return ableton_set


def first_session_clip(ableton_set: AbletonSet) -> MidiClipRef:
    return next(clip for clip in ableton_set.clips.midi() if clip.location is ClipLocation.SESSION)


def track_id(ableton_set: AbletonSet, name: str) -> int:
    track = next(t for t in ableton_set.tracks.load() if t.name == name)
    assert track.id is not None
    return int(track.id)


# --- set_notes ---------------------------------------------------------


def test_set_notes_op_writes_through_to_the_clip(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("12.4.5b", tmp_path)
    doc = OpsDocument.model_validate(
        {
            "ops": [
                {
                    "op": "set_notes",
                    "track": {"name": "1-LOW"},
                    "clip": {"slot": 1},
                    "notes": [[60, 0.0, 1.0], [64, 1.0, 1.0]],
                }
            ]
        }
    )
    results = apply_ops(ableton_set, doc.ops)
    assert len(results) == 1
    assert "set_notes" in results[0]

    rewritten = first_session_clip(ableton_set)
    assert [(n.pitch, n.start, n.duration) for n in rewritten.notes] == [(60, 0.0, 1.0), (64, 1.0, 1.0)]


def test_set_notes_op_selects_the_clip_by_track_id(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("12.4.5b", tmp_path)
    track = next(t for t in ableton_set.tracks.load() if t.name == "1-LOW")
    doc = OpsDocument.model_validate(
        {
            "ops": [
                {"op": "set_notes", "track": {"id": int(track.id)}, "clip": {"slot": 1}, "notes": [[67, 0.0, 0.5]]},
            ]
        }
    )
    apply_ops(ableton_set, doc.ops)
    assert [(n.pitch, n.start, n.duration) for n in first_session_clip(ableton_set).notes] == [(67, 0.0, 0.5)]


def test_set_notes_op_selects_an_arrangement_clip_by_start(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("11.0.0", tmp_path)
    clip = next(c for c in ableton_set.clips.midi() if c.track_name == "Kick" and c.start_time == 224.0)
    doc = OpsDocument.model_validate(
        {
            "ops": [
                {
                    "op": "set_notes",
                    "track": {"name": "Kick"},
                    "clip": {"start": 224.0},
                    "notes": [[60, 0.0, 0.5]],
                }
            ]
        }
    )
    apply_ops(ableton_set, doc.ops)
    rewritten = next(c for c in ableton_set.clips.midi() if c.clip_element is clip.clip_element)
    assert [(n.pitch, n.start, n.duration) for n in rewritten.notes] == [(60, 0.0, 0.5)]


# --- clone_clip ----------------------------------------------------------


def test_clone_clip_op_lands_in_the_first_empty_slot(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("12.4.5b", tmp_path)
    doc = OpsDocument.model_validate(
        {
            "ops": [
                {
                    "op": "clone_clip",
                    "track": {"name": "1-LOW"},
                    "donor": {"slot": 1},
                    "slot": None,
                    "name": "clone via ops",
                    "length": 4.0,
                    "notes": [[60, 0.0, 1.0]],
                }
            ]
        }
    )
    apply_ops(ableton_set, doc.ops)

    session_clips = [c for c in ableton_set.clips.midi() if c.location is ClipLocation.SESSION]
    assert {c.name for c in session_clips} == {"", "clone via ops"}
    # Slot 0 is the first empty session slot on this track (slot 1 holds the donor);
    # occupying it is what proves the null-slot path found it rather than erroring.
    with pytest.raises(ValueError, match="already holds a clip"):
        ableton_set.clips.clone_clip(next(c for c in session_clips if c.name == ""), slot_index=0, name="x", notes=[])


def test_clone_clip_op_with_explicit_slot(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("12.4.5b", tmp_path)
    doc = OpsDocument.model_validate(
        {
            "ops": [
                {
                    "op": "clone_clip",
                    "track": {"name": "1-LOW"},
                    "donor": {"slot": 1},
                    "slot": 3,
                    "name": "explicit slot",
                    "notes": [],
                }
            ]
        }
    )
    apply_ops(ableton_set, doc.ops)
    clone = next(c for c in ableton_set.clips.midi() if c.name == "explicit slot")
    assert clone.notes == ()


# --- graft_chain -----------------------------------------------------------


def test_graft_chain_op_within_one_set(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("12.4.5b", tmp_path)
    doc = OpsDocument.model_validate(
        {
            "ops": [
                {
                    "op": "graft_chain",
                    "donor_set": None,
                    "donor_track": {"name": "A-Reverb"},
                    "target_track": {"name": "3-Skylark - Iced"},
                    "mode": "append",
                }
            ]
        }
    )
    results = apply_ops(ableton_set, doc.ops)
    assert "graft_chain" in results[0]
    target = next(chain for chain in ableton_set.devices.inventory() if chain.track_name == "3-Skylark - Iced")
    assert [d.tag for d in target.devices] == ["Reverb"]


def test_graft_chain_op_from_another_set(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("12.4.5b", tmp_path)
    donor_path = str(SKELETONS / "12.2.6.als")
    doc = OpsDocument.model_validate(
        {
            "ops": [
                {
                    "op": "graft_chain",
                    "donor_set": donor_path,
                    "donor_track": {"name": "A-Reverb"},
                    "target_track": {"name": "3-Skylark - Iced"},
                    "mode": "append",
                }
            ]
        }
    )
    apply_ops(ableton_set, doc.ops)
    target = next(chain for chain in ableton_set.devices.inventory() if chain.track_name == "3-Skylark - Iced")
    assert [d.tag for d in target.devices] == ["Reverb"]


# --- selector resolution / ambiguity ----------------------------------------


def test_track_selector_by_name_raises_on_no_match(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("12.4.5b", tmp_path)
    with pytest.raises(ValueError, match="No track named"):
        _resolve_track(ableton_set, TrackByName(name="nope"))


def test_track_selector_by_name_raises_on_ambiguity(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("12.4.5b", tmp_path)
    # Manufacture a duplicate name -- none occurs naturally in the corpus.
    for track in ableton_set.tracks.load():
        if track.name == "A-Reverb":
            for user_name in track.track_root.iter("UserName"):
                user_name.set("Value", "B-Delay")
            track.name = "B-Delay"
    with pytest.raises(ValueError, match="2 tracks are named 'B-Delay'; use"):
        _resolve_track(ableton_set, TrackByName(name="B-Delay"))


def test_ops_document_rejects_an_unknown_op() -> None:
    with pytest.raises(pydantic.ValidationError):
        OpsDocument.model_validate({"ops": [{"op": "delete_everything", "track": {"id": 1}}]})


def test_ops_document_rejects_a_selector_with_both_keys() -> None:
    with pytest.raises(pydantic.ValidationError):
        OpsDocument.model_validate(
            {
                "ops": [
                    {
                        "op": "set_notes",
                        "track": {"id": 1, "name": "x"},
                        "clip": {"slot": 0},
                        "notes": [],
                    }
                ]
            }
        )


# --- whole-document validation ----------------------------------------------


def test_a_bad_op_aborts_before_a_good_earlier_op_mutates_anything(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("12.4.5b", tmp_path)
    before = ableton_set.generate_xml()
    doc = OpsDocument.model_validate(
        {
            "ops": [
                {"op": "set_notes", "track": {"name": "1-LOW"}, "clip": {"slot": 1}, "notes": [[60, 0.0, 1.0]]},
                {"op": "set_notes", "track": {"name": "does-not-exist"}, "clip": {"slot": 0}, "notes": []},
            ]
        }
    )
    with pytest.raises(ValueError, match="No track named 'does-not-exist'"):
        apply_ops(ableton_set, doc.ops)
    assert ableton_set.generate_xml() == before


def test_an_out_of_range_slot_is_rejected_before_any_mutation(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("12.4.5b", tmp_path)
    before = ableton_set.generate_xml()
    doc = OpsDocument.model_validate(
        {
            "ops": [
                {"op": "set_notes", "track": {"name": "1-LOW"}, "clip": {"slot": 1}, "notes": [[60, 0.0, 1.0]]},
                {
                    "op": "clone_clip",
                    "track": {"name": "1-LOW"},
                    "donor": {"slot": 1},
                    "slot": 99,
                    "name": "nope",
                    "notes": [],
                },
            ]
        }
    )
    with pytest.raises(ValueError, match="no slot 99"):
        apply_ops(ableton_set, doc.ops)
    assert ableton_set.generate_xml() == before


# --- byte fidelity -----------------------------------------------------


def test_set_notes_op_rewriting_the_same_notes_is_byte_identical(tmp_path: pathlib.Path) -> None:
    """Strongest fidelity check available, run through the ops surface: writing
    a clip's own notes back through set_notes reproduces Live's XML exactly."""
    ableton_set = make_set("9.0.1", tmp_path)
    donor = first_session_clip(ableton_set)
    assert donor.track_name is not None
    before = ableton_set.generate_xml()
    notes = [encode_note(note, extended=False) for note in donor.notes]

    doc = OpsDocument.model_validate(
        {
            "ops": [
                {
                    "op": "set_notes",
                    "track": {"id": track_id(ableton_set, donor.track_name)},
                    "clip": {"slot": 0},
                    "notes": notes,
                }
            ]
        }
    )
    apply_ops(ableton_set, doc.ops)
    assert ableton_set.generate_xml() == before


def test_graft_chain_leaves_untouched_chains_byte_identical(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("12.4.5b", tmp_path)
    before = {
        chain.track_name: ET.tostring(chain.devices[0].device_element, encoding="unicode") if chain.devices else ""
        for chain in ableton_set.devices.inventory()
        if chain.track_name != "3-Skylark - Iced"
    }
    doc = OpsDocument.model_validate(
        {
            "ops": [
                {
                    "op": "graft_chain",
                    "donor_set": None,
                    "donor_track": {"name": "A-Reverb"},
                    "target_track": {"name": "3-Skylark - Iced"},
                    "mode": "append",
                }
            ]
        }
    )
    apply_ops(ableton_set, doc.ops)
    after = {
        chain.track_name: ET.tostring(chain.devices[0].device_element, encoding="unicode") if chain.devices else ""
        for chain in ableton_set.devices.inventory()
        if chain.track_name != "3-Skylark - Iced"
    }
    assert before == after


# --- arrangement sections ---------------------------------------------------
#
# 11.0.12 is the fixture with a real arrangement: "8-Drums to MIDI" holds
# MIDI clips at 96-120.5, 128-144, 144-152, 152-168, 168-190, 192-208,
# 208-254, 256-272 and 272-316, and "snr2" holds audio clips at 64-120.5,
# 128-190, 192-252.875, 256-320, 320-380.5 and up. The 12.x skeletons carry
# no arrangement at all.


def test_place_clip_op_round_trips(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("11.0.12", tmp_path)
    doc = OpsDocument.model_validate(
        {
            "ops": [
                {
                    "op": "place_clip",
                    "donor_track": {"name": "8-Drums to MIDI"},
                    "donor": {"start": 128.0},
                    "target_track": {"name": "8-Drums to MIDI"},
                    "at": 800.0,
                }
            ]
        }
    )
    results = apply_ops(ableton_set, doc.ops)
    assert results == ["place_clip: placed 'p01587' on track '8-Drums to MIDI' at beat 800.0"]
    starts = [clip.start_time for clip in ableton_set.clips.midi() if clip.location is ClipLocation.ARRANGEMENT]
    assert 800.0 in starts


def test_place_clip_op_accepts_an_audio_donor(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("11.0.12", tmp_path)
    doc = OpsDocument.model_validate(
        {
            "ops": [
                {
                    "op": "place_clip",
                    "donor_track": {"name": "snr2"},
                    "donor": {"start": 128.0},
                    "target_track": {"name": "snr2"},
                    "at": 900.0,
                }
            ]
        }
    )
    apply_ops(ableton_set, doc.ops)
    assert 900.0 in [clip.start_time for clip in ableton_set.clips.audio()]


def test_place_clip_op_rejects_the_wrong_track_kind_before_running(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("11.0.12", tmp_path)
    doc = OpsDocument.model_validate(
        {
            "ops": [
                {
                    "op": "place_clip",
                    "donor_track": {"name": "8-Drums to MIDI"},
                    "donor": {"start": 128.0},
                    "target_track": {"name": "snr2"},
                    "at": 800.0,
                }
            ]
        }
    )
    with pytest.raises(ValueError, match="needs a MidiTrack"):
        apply_ops(ableton_set, doc.ops)


def test_place_clip_op_rejects_a_cross_major_version_donor(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("11.0.12", tmp_path)
    donor_set = make_set("10.0.6", tmp_path)
    doc = OpsDocument.model_validate(
        {
            "ops": [
                {
                    "op": "place_clip",
                    "donor_set": str(donor_set.path),
                    "donor_track": {"name": "2-Drum Rack"},
                    "donor": {"start": 256.0},
                    "target_track": {"name": "8-Drums to MIDI"},
                    "at": 800.0,
                }
            ]
        }
    )
    with pytest.raises(ValueError, match="donor is from Live 10.x"):
        apply_ops(ableton_set, doc.ops)


def test_copy_section_op_round_trips(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("11.0.12", tmp_path)
    doc = OpsDocument.model_validate(
        {
            "ops": [
                {
                    "op": "copy_section",
                    "src_start": 128.0,
                    "src_end": 192.0,
                    "dest_start": 384.0,
                    "tracks": ["8-Drums to MIDI"],
                }
            ]
        }
    )
    results = apply_ops(ableton_set, doc.ops)
    assert results == ["copy_section: copied 4 clip(s) to beat 384"]
    starts = [clip.start_time for clip in ableton_set.clips.midi()]
    assert [start for start in starts if start >= 384.0] == [384.0, 400.0, 408.0, 424.0]


def test_copy_section_op_rejects_an_unknown_track_before_running(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("11.0.12", tmp_path)
    doc = OpsDocument.model_validate(
        {"ops": [{"op": "copy_section", "src_start": 0.0, "src_end": 64.0, "dest_start": 800.0, "tracks": ["nope"]}]}
    )
    with pytest.raises(ValueError, match="No track named 'nope'"):
        apply_ops(ableton_set, doc.ops)


def test_copy_section_op_rejects_a_backwards_range_before_running(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("11.0.12", tmp_path)
    doc = OpsDocument.model_validate(
        {"ops": [{"op": "copy_section", "src_start": 192.0, "src_end": 128.0, "dest_start": 800.0}]}
    )
    with pytest.raises(ValueError, match="must be after"):
        apply_ops(ableton_set, doc.ops)


def test_remove_section_clips_op_round_trips(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("11.0.12", tmp_path)
    doc = OpsDocument.model_validate(
        {"ops": [{"op": "remove_section_clips", "start": 128.0, "end": 192.0, "tracks": ["8-Drums to MIDI"]}]}
    )
    results = apply_ops(ableton_set, doc.ops)
    assert results[0].count("deleted") == 4
    starts = [clip.start_time for clip in ableton_set.clips.midi() if clip.location is ClipLocation.ARRANGEMENT]
    assert starts == [96.0, 192.0, 208.0, 256.0, 272.0]


def test_remove_section_clips_op_needs_an_explicit_track_list() -> None:
    with pytest.raises(pydantic.ValidationError):
        OpsDocument.model_validate({"ops": [{"op": "remove_section_clips", "start": 0.0, "end": 64.0}]})


def test_a_bad_section_op_aborts_before_an_earlier_one_mutates_anything(tmp_path: pathlib.Path) -> None:
    ableton_set = make_set("11.0.12", tmp_path)
    before = ET.tostring(ableton_set.root, encoding="unicode")
    doc = OpsDocument.model_validate(
        {
            "ops": [
                {
                    "op": "copy_section",
                    "src_start": 128.0,
                    "src_end": 192.0,
                    "dest_start": 384.0,
                    "tracks": ["8-Drums to MIDI"],
                },
                {"op": "remove_section_clips", "start": 0.0, "end": 64.0, "tracks": ["nope"]},
            ]
        }
    )
    with pytest.raises(ValueError, match="No track named 'nope'"):
        apply_ops(ableton_set, doc.ops)
    assert ET.tostring(ableton_set.root, encoding="unicode") == before
