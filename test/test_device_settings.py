"""Walking every device in a set, and reading or writing one setting by name.

The question this answers came from a user: change one setting on every
Sampler in a set, without reading the XML to find where it lives. Ground truth
here is a raw ElementTree walk of the skeleton fixtures, independent of
``walk``.
"""

from __future__ import annotations

import gzip
import pathlib
from xml.etree import ElementTree as ET

import pytest

from abletoolz.live_set import AbletonSet
from abletoolz.live_set.apply_ops import OpsDocument, SetDeviceSettingResult, apply_ops
from abletoolz.schema import Tag
from abletoolz.schema.devices import Device, EqEight, Sampler, Simpler
from abletoolz.schema.settings import NotInThisVersion

SKELETONS = pathlib.Path(__file__).parent / "version_fixtures" / "skeletons"


def make_set(key: str) -> AbletonSet:
    live_set = AbletonSet(SKELETONS / f"{key}.als")
    assert live_set.parse()
    return live_set


def raw_device_count(key: str) -> int:
    """Every child of every ``Devices`` list outside the cue track."""
    root = ET.fromstring(gzip.decompress((SKELETONS / f"{key}.als").read_bytes()))
    cue = root.find(f"{Tag.LIVE_SET}/{Tag.PRE_HEAR_TRACK}")
    in_cue = {id(element) for element in cue.iter()} if cue is not None else set()
    return sum(len(devices) for devices in root.iter(Tag.DEVICES) if id(devices) not in in_cue)


@pytest.mark.parametrize("key", sorted(path.stem for path in SKELETONS.glob("*.als")))
def test_walk_reaches_every_device_racks_included(key: str) -> None:
    assert len(list(make_set(key).devices.walk())) == raw_device_count(key)


def test_walk_goes_inside_racks() -> None:
    """9.0.1 holds 13 devices on its tracks and 15 more inside its racks."""
    live_set = make_set("9.0.1")
    on_tracks = sum(len(chain.devices) for chain in live_set.devices.inventory())
    assert (on_tracks, len(list(live_set.devices.walk()))) == (13, 28)


def test_walk_filters_by_kind_and_track() -> None:
    live_set = make_set("10.0.6")
    simplers = list(live_set.devices.walk(Device.SIMPLER))
    assert simplers and all(ref.tag == "OriginalSimpler" for ref in simplers)
    assert list(live_set.devices.walk(Device.SIMPLER, track_name="no such track")) == []


def _same_named_main_track() -> tuple[AbletonSet, ET.Element, ET.Element]:
    live_set = make_set("11.3.42")
    track = next(t for t in live_set.root.iter("MidiTrack") if t.get("Id") == "12")
    main = next(live_set.root.iter("MasterTrack"))
    name = track.find("Name/UserName")
    assert name is not None
    name.set("Value", "Master")
    for root in (track, main):
        devices = root.find("DeviceChain/DeviceChain/Devices")
        assert devices is not None
        eq = ET.SubElement(devices, "Eq8", Id="987654")
        ET.SubElement(ET.SubElement(eq, "On"), "Manual", Value="true")
        ET.SubElement(eq, "AdaptiveQ", Value="false")
    return live_set, track, main


def test_an_explicit_track_id_does_not_change_a_same_named_main_track() -> None:
    live_set, regular, main = _same_named_main_track()
    untouched_main = ET.tostring(main)
    document = OpsDocument.model_validate(
        {
            "ops": [
                {
                    "op": "set_device_setting",
                    "track": {"id": 12},
                    "device": "EQ_EIGHT",
                    "setting": "ADAPTIVE_Q",
                    "value": True,
                }
            ]
        }
    )
    (result,) = apply_ops(live_set, document.ops)
    assert isinstance(result, SetDeviceSettingResult)
    assert result.devices == 1
    value = regular.find("DeviceChain/DeviceChain/Devices/Eq8[@Id='987654']/AdaptiveQ")
    assert value is not None and value.get("Value") == "true"
    assert ET.tostring(main) == untouched_main


def test_a_same_named_main_track_is_ambiguous_before_any_batch_edit() -> None:
    live_set, _, _ = _same_named_main_track()
    before = ET.tostring(live_set.root)
    document = OpsDocument.model_validate(
        {
            "ops": [
                {"op": "set_device_setting", "device": "EQ_EIGHT", "setting": "ADAPTIVE_Q", "value": True},
                {
                    "op": "set_device_setting",
                    "track": {"name": "Master"},
                    "device": "EQ_EIGHT",
                    "setting": "ADAPTIVE_Q",
                    "value": False,
                },
            ]
        }
    )
    with pytest.raises(ValueError, match="tracks are named"):
        apply_ops(live_set, document.ops)
    assert ET.tostring(live_set.root) == before


def test_device_walk_selects_an_owned_track_and_refuses_ambiguous_or_foreign_targets() -> None:
    live_set, _, main = _same_named_main_track()
    owned = live_set.tracks.main()
    (eq,) = live_set.devices.walk(Device.EQ_EIGHT, track=owned)
    assert eq.device_element is main.find("DeviceChain/DeviceChain/Devices/Eq8[@Id='987654']")
    with pytest.raises(ValueError, match="tracks are named"):
        list(live_set.devices.walk(Device.EQ_EIGHT, track_name="Master"))
    with pytest.raises(ValueError, match="not both"):
        list(live_set.devices.walk(Device.EQ_EIGHT, track=owned, track_name="Master"))
    foreign = make_set("11.3.42").tracks.main()
    with pytest.raises(ValueError, match="does not belong"):
        list(live_set.devices.walk(Device.EQ_EIGHT, track=foreign))


def test_a_sampler_s_volume_reads_as_a_number() -> None:
    (sampler,) = make_set("10.0.1").devices.walk(Device.SAMPLER)
    volume = sampler.setting(Sampler.VOLUME)
    assert (volume.path, volume.value) == ("VolumeAndPan/Volume", -4.5)
    assert sampler.setting(Sampler.ON).value is True


def test_every_sampler_s_volume_can_be_set_and_survives_a_save(tmp_path: pathlib.Path) -> None:
    live_set = make_set("10.0.6")
    for simpler in live_set.devices.walk(Device.SIMPLER):
        simpler.setting(Simpler.VOLUME).set(-6)
    live_set.save_set(output_dir=tmp_path)

    reopened = AbletonSet(tmp_path / "10.0.6.als")
    assert reopened.parse()
    volumes = [simpler.setting(Simpler.VOLUME) for simpler in reopened.devices.walk(Device.SIMPLER)]
    assert volumes and {volume.raw for volume in volumes} == {"-6"}


def test_a_setting_in_a_list_takes_its_position() -> None:
    (eq,) = list(make_set("10.0.1").devices.walk(Device.EQ_EIGHT))[:1]
    assert eq.setting(EqEight.PARAMETER_A_FREQ, index=0).path == "Bands.0/ParameterA/Freq"
    with pytest.raises(ValueError, match="in a list"):
        eq.setting(EqEight.PARAMETER_A_FREQ)


def test_another_device_s_setting_is_refused() -> None:
    """``Sampler.VOLUME`` and ``Simpler.VOLUME`` are the same path; the refusal is by device."""
    (simpler, *_) = make_set("10.0.6").devices.walk(Device.SIMPLER)
    with pytest.raises(ValueError, match="use Simpler"):
        simpler.setting(Sampler.VOLUME)


def test_a_hosted_plugin_has_no_settings_here() -> None:
    plugin = next(make_set("9.6.0").devices.walk(Device.PLUGIN))
    with pytest.raises(ValueError, match="plugin's own"):
        plugin.setting(Sampler.VOLUME)


def test_a_setting_the_set_predates_says_so() -> None:
    """Round robin arrived in Live 12; a Live 10 Sampler has none, and the corpus has later ones without it."""
    (sampler,) = make_set("10.0.1").devices.walk(Device.SAMPLER)
    with pytest.raises(NotInThisVersion, match="first appears in Live 12"):
        sampler.setting(Sampler.ROUND_ROBIN)


@pytest.mark.parametrize(
    ("setting", "value"),
    [(Sampler.VOLUME, True), (Sampler.VOLUME, "loud"), (Sampler.ON, 1)],
)
def test_a_value_of_the_wrong_kind_is_refused(setting: Sampler, value: bool | float | str) -> None:
    (sampler,) = make_set("10.0.1").devices.walk(Device.SAMPLER)
    with pytest.raises(TypeError):
        sampler.setting(setting).set(value)


def test_the_ops_document_sets_every_simpler() -> None:
    live_set = make_set("10.0.6")
    document = OpsDocument.model_validate(
        {"ops": [{"op": "set_device_setting", "device": "SIMPLER", "setting": "VOLUME", "value": -3}]}
    )
    (result,) = apply_ops(live_set, document.ops)
    assert result == SetDeviceSettingResult(device="SIMPLER", setting="VOLUME", value="-3", devices=9)
    assert {ref.setting(Simpler.VOLUME).raw for ref in live_set.devices.walk(Device.SIMPLER)} == {"-3"}


@pytest.mark.parametrize(
    ("op", "message"),
    [
        ({"device": "SAMPLR", "setting": "VOLUME", "value": 0}, "no device"),
        ({"device": "SIMPLER", "setting": "VOLUM", "value": 0}, "no setting"),
        ({"device": "SAMPLER", "setting": "VOLUME", "value": 0}, "no SAMPLER"),
        ({"device": "SIMPLER", "setting": "VOLUME", "value": "loud"}, "is a number"),
    ],
)
def test_a_bad_setting_op_fails_before_anything_changes(op: dict[str, object], message: str) -> None:
    live_set = make_set("10.0.6")
    before = [ref.setting(Simpler.VOLUME).raw for ref in live_set.devices.walk(Device.SIMPLER)]
    good = {"op": "set_device_setting", "device": "SIMPLER", "setting": "VOLUME", "value": -1}
    document = OpsDocument.model_validate({"ops": [good, {"op": "set_device_setting", **op}]})
    with pytest.raises((ValueError, TypeError), match=message):
        apply_ops(live_set, document.ops)
    assert [ref.setting(Simpler.VOLUME).raw for ref in live_set.devices.walk(Device.SIMPLER)] == before
