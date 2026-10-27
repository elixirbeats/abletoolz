"""Live's own schemas, packed, and the version answers settings get from them."""

from __future__ import annotations

import pytest

from abletoolz.schema.devices import SETTINGS_OF, AutoFilter2, Compressor, Sampler, Saturator, ValueKind
from abletoolz.schema.live_schemas import ValueType, live_schemas
from abletoolz.schema.objects import OWNERS, Clip, Mixer
from abletoolz.schema.settings import (
    NotInThisVersion,
    is_parameter,
    kind_of,
    minor_version_of,
    path_for,
    spellings,
)


def test_every_schema_live_ships_is_packed() -> None:
    schemas = live_schemas()
    assert len(schemas.minor_versions) == 203
    assert {"8.1_220", "9.0_305", "10.0_370", "11.0_11300", "12.0_12402"} <= set(schemas.minor_versions)


def test_a_path_walks_from_class_to_class() -> None:
    schemas = live_schemas()
    assert schemas.declares("10.0_370", "Eq8", "Bands.{index}/ParameterA/Freq") is True
    assert schemas.declares("10.0_370", "Eq8", "Bands.{index}/ParameterA/Nope") is False


def test_a_member_s_type_follows_its_schema() -> None:
    """``FileRef.RelativePath`` was a list of path elements until 11.0_403, then a string."""
    schemas = live_schemas()
    assert schemas.member_type("11.0_402", "FileRef", "RelativePath") == "List<RelativePathElement>"
    assert schemas.member_type("11.0_403", "FileRef", "RelativePath") == "String"


def test_saturator_s_waveshaper_flattens_in_one_schema() -> None:
    schemas = live_schemas()
    assert schemas.declares("12.0_12111", "Saturator", "WsCurve") is False
    assert schemas.declares("12.0_12112", "Saturator", "WsCurve") is True


@pytest.mark.parametrize(
    ("minor", "owner", "path"),
    [
        ("13.0_1", "Eq8", "Mode"),
        ("10.0_370", "NoSuchDevice", "Mode"),
        ("10.0_370", "MultiSampleMap", "SampleParts/Nope"),
    ],
)
def test_what_the_schema_cannot_say_is_none(minor: str, owner: str, path: str) -> None:
    assert live_schemas().declares(minor, owner, path) is None


def test_a_release_between_two_of_one_schema_carries_it() -> None:
    assert minor_version_of((10, 1, 20)) == "10.0_377"
    assert minor_version_of((11, 1, 0)) is None  # between 11.0_433 and 11.0_436
    assert minor_version_of((8, 2, 0)) is None


def test_a_release_the_corpus_never_held_is_still_refused_by_its_schema() -> None:
    with pytest.raises(NotInThisVersion, match="10.0_377"):
        path_for(Sampler.ROUND_ROBIN, (10, 1, 20), "MultiSampler")


def test_a_setting_a_real_set_held_outranks_the_schema() -> None:
    """Live 9.0.1 sets hold ``StartWithSongTempo``; Live's last 9.0_305 schema no longer declares it."""
    assert live_schemas().declares("9.0_305", "AudioClip", "StartWithSongTempo") is False
    assert path_for(Clip.START_WITH_SONG_TEMPO, (9, 0, 1), "AudioClip") == "StartWithSongTempo"


def test_two_devices_with_one_path_keep_their_own_spellings() -> None:
    """AutoFilter2 always wrote ``SideChainEq_Freq``; Compressor wrote ``SideChainEq/Freq`` before Live 12.1.5."""
    assert spellings(AutoFilter2.SIDE_CHAIN_EQ_FREQ) == (((0, 0, 0), "SideChainEq_Freq"),)
    assert spellings(Compressor.SIDE_CHAIN_EQ_FREQ) == (
        ((12, 1, 5), "SideChainEq_Freq"),
        ((0, 0, 0), "SideChainEq/Freq"),
    )


def test_an_element_s_class_says_what_value_it_holds() -> None:
    schemas = live_schemas()
    assert schemas.value_type("10.0_370", "Eq8", "Bands.{index}/ParameterA/Freq") == ValueType("float", timeable=True)
    assert schemas.value_type("12.0_12402", "Saturator", "Oversampling") == ValueType("bool", timeable=False)
    assert schemas.value_type("12.0_12402", "Looper", "SavedLengthInBeats") == ValueType("float", timeable=False)
    assert schemas.value_type("10.0_370", "Eq8", "Bands.{index}/ParameterA/Nope") is None


def test_a_setting_s_kind_comes_from_the_newest_schema_that_types_it() -> None:
    """The census saw Drive only on whole numbers; Live's schema types it as an automatable float."""
    assert kind_of(Saturator.DRIVE) is ValueKind.FLOAT
    assert is_parameter(Saturator.DRIVE)
    assert not is_parameter(Mixer.ACTIVE)


def test_every_member_s_kind_and_role_are_answered() -> None:
    for settings_class in (*SETTINGS_OF.values(), *OWNERS):
        for member in settings_class:
            assert isinstance(kind_of(member), ValueKind)
            assert isinstance(is_parameter(member), bool)


def test_a_setting_live_retyped_keeps_the_census_kind() -> None:
    """Oversampling was an enum until 12.0_12112 and a switch after; sets hold both, so it reads as text."""
    schemas = live_schemas()
    assert schemas.value_type("12.0_12111", "Saturator", "Oversampling") == ValueType("int", timeable=False)
    assert kind_of(Saturator.OVERSAMPLING) is ValueKind.STR


def test_a_mixer_setting_is_looked_up_inside_its_track() -> None:
    assert path_for(Mixer.TEMPO, (11, 3, 42), "MasterTrack") == "Tempo"
    assert path_for(Mixer.TEMPO, (12, 4, 5), "MainTrack") == "Tempo"
