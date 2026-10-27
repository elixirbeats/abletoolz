"""Settings members resolved against the Live version of the set they are used in."""

from __future__ import annotations

import pathlib
from xml.etree import ElementTree as ET

import pytest
from conftest import skeleton_set

from abletoolz.alc import AlcClip
from abletoolz.live_set.apply_ops import OpsDocument, apply_ops
from abletoolz.live_set.settings import every, resolve
from abletoolz.schema.objects import Clip, Mixer, Track, Transport
from abletoolz.schema.settings import NotOnThisElement, path_for, spellings

ALC = pathlib.Path(__file__).parent / "alc_fixtures" / "audeka_live_saved.alc"


@pytest.mark.parametrize(
    ("version", "expected"),
    [((9, 6, 0), "ColorIndex"), ((10, 1, 30), "ColorIndex"), ((11, 0, 0), "Color"), ((12, 4, 5), "Color")],
)
def test_one_member_names_both_spellings_of_a_renamed_setting(version: tuple[int, int, int], expected: str) -> None:
    assert path_for(Track.COLOR, version, "AudioTrack") == expected


@pytest.mark.parametrize(
    ("version", "expected"),
    [((10, 1, 30), "FollowActionA"), ((11, 0, 0), "FollowAction/FollowActionA")],
)
def test_a_moved_setting_resolves_to_where_that_version_kept_it(version: tuple[int, int, int], expected: str) -> None:
    assert path_for(Clip.FOLLOW_ACTION_A, version, "MidiClip") == expected


def test_a_list_written_without_ids_is_addressed_by_position() -> None:
    assert path_for(Mixer.SEND, (9, 6, 0), "AudioTrack") == "Sends/TrackSendHolder[{index_position}]/Send"
    assert path_for(Mixer.SEND, (12, 4, 5), "AudioTrack") == "Sends/TrackSendHolder[@Id='{index}']/Send"


def test_spellings_run_newest_first_and_end_at_the_floor() -> None:
    assert spellings(Track.COLOR) == (((11, 0, 0), "Color"), ((0, 0, 0), "ColorIndex"))
    assert spellings(Mixer.VOLUME) == (((0, 0, 0), "Volume"),)


def test_a_setting_another_track_type_owns_is_refused_by_name() -> None:
    with pytest.raises(NotOnThisElement, match="ReturnTrack"):
        path_for(Track.IS_ARMED, (12, 4, 5), "ReturnTrack")


def test_tempo_lives_on_the_main_mixer_whatever_its_element_was_called() -> None:
    assert path_for(Mixer.TEMPO, (11, 3, 42), "MasterTrack") == "Tempo"
    with pytest.raises(NotOnThisElement):
        path_for(Mixer.TEMPO, (11, 3, 42), "AudioTrack")


def test_where_the_corpus_is_silent_the_lookup_goes_ahead() -> None:
    """No set older than 9.0.1 was measured, so a member first seen there is not refused for 8.2."""
    assert path_for(Track.TRACK_GROUP_ID, (8, 2, 0), "AudioTrack") == "TrackGroupId"


@pytest.mark.parametrize("key", ["9.6.0", "10.0.6", "11.3.42", "12.4.5b"])
def test_track_color_reads_on_every_version(key: str) -> None:
    live_set = skeleton_set(key)
    track = live_set.tracks.load()[0]
    setting = track.setting(Track.COLOR)
    assert setting.path == ("Color" if live_set.version_tuple >= (11, 0, 0) else "ColorIndex")
    assert setting.value == track.color


def test_a_setting_reports_where_it_landed() -> None:
    track = skeleton_set("10.0.6").tracks.load()[0]
    assert repr(track.setting(Track.COLOR)) == "Track.COLOR → ColorIndex (Live 10.0.6)"


@pytest.mark.parametrize("key", ["9.6.0", "12.4.5b"])
def test_every_send_is_found_with_or_without_ids(key: str) -> None:
    live_set = skeleton_set(key)
    track = next(track for track in live_set.tracks.load() if track.every(Mixer.SEND))
    sends = track.every(Mixer.SEND)
    paths = [send.path for send in sends]
    if live_set.version_tuple < (10, 0, 0):
        assert paths[0] == "Sends/TrackSendHolder[1]/Send"
    else:
        assert paths[0] == "Sends/TrackSendHolder[@Id='0']/Send"
    assert track.setting(Mixer.SEND, index=0).element is sends[0].element


def test_a_spelling_the_version_did_not_predict_is_still_found() -> None:
    """Where the corpus has no sets of a version, the switch point is a measurement, not a fact."""
    clip = ET.fromstring('<MidiClip><Color Value="7" /></MidiClip>')
    setting = resolve(clip, Clip.COLOR, (10, 1, 30))
    assert setting.path == "Color"
    assert setting.value == 7


def test_every_on_an_element_without_the_setting_is_empty() -> None:
    assert every(ET.fromstring("<Mixer />"), Mixer.SEND, (12, 4, 5), owner_tag="AudioTrack") == []


def test_tempo_reads_from_the_main_mixer() -> None:
    live_set = skeleton_set("12.4.5b")
    assert live_set.tracks.main().setting(Mixer.TEMPO).value == live_set.transport.bpm()


def test_the_ops_document_sets_a_mixer_setting_on_every_track() -> None:
    live_set = skeleton_set("11.0.12")
    document = OpsDocument.model_validate(
        {"ops": [{"op": "set_track_setting", "setting": "Mixer.VOLUME", "value": 0.5}]}
    )
    (result,) = apply_ops(live_set, document.ops)
    assert result.op == "set_track_setting"
    assert {track.setting(Mixer.VOLUME).value for track in live_set.tracks.walk()} == {0.5}


def test_the_ops_document_refuses_a_setting_without_its_enum() -> None:
    live_set = skeleton_set("11.0.12")
    document = OpsDocument.model_validate({"ops": [{"op": "set_track_setting", "setting": "VOLUME", "value": 0.5}]})
    with pytest.raises(ValueError, match="Mixer.VOLUME"):
        apply_ops(live_set, document.ops)


def test_the_ops_document_sets_a_transport_setting() -> None:
    live_set = skeleton_set("12.4.5b")
    document = OpsDocument.model_validate(
        {"ops": [{"op": "set_transport_setting", "setting": "LOOP_ON", "value": True}]}
    )
    apply_ops(live_set, document.ops)
    assert live_set.transport.setting(Transport.LOOP_ON).value is True


def test_an_alc_clip_resolves_against_its_own_version() -> None:
    clip = AlcClip.load(ALC)
    assert clip.setting(Clip.COLOR).path == ("Color" if clip.version >= (11, 0, 0) else "ColorIndex")
    clip.color_index = 5
    assert clip.color_index == 5


def test_the_ops_document_sets_a_clip_setting() -> None:
    live_set = skeleton_set("11.0.12")
    clip = next(clip for clip in live_set.clips.midi() if clip.location.name == "ARRANGEMENT")
    document = OpsDocument.model_validate(
        {
            "ops": [
                {
                    "op": "set_clip_setting",
                    "track": {"name": clip.track_name},
                    "clip": {"start": clip.start_time},
                    "setting": "LOOP_ON",
                    "value": False,
                }
            ]
        }
    )
    (result,) = apply_ops(live_set, document.ops)
    assert result.op == "set_clip_setting"
    assert clip.setting(Clip.LOOP_ON).value is False


@pytest.mark.parametrize(
    ("value", "error"),
    [(2.5, TypeError), (float("inf"), ValueError), (99, ValueError)],
)
def test_track_color_takes_only_a_palette_index(value: float, error: type[Exception]) -> None:
    """Color is an INT setting with the 0-69 range the ``color`` setter has always enforced."""
    setting = skeleton_set("12.4.5b").tracks.load()[0].setting(Track.COLOR)
    with pytest.raises(error):
        setting.encode(value)
    assert setting.encode(69.0) == "69"
