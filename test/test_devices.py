"""Device-chain extraction, checked against ground truth harvested by hand.

Everything asserted here was read off the gunzipped fixtures with raw
ElementTree walks before any of it existed in code, the same way
``expected.json`` and the clip tests are built. What the walk turned up:

* Native devices and hosted ones are siblings in the same ``Devices`` list,
  told apart only by tag: 9.6.0's first track runs ``PluginDevice``,
  ``PluginDevice``, ``PluginDevice``, ``AutoFilter``, ``Compressor2``,
  ``Eq8`` in that order.
* ``Device/@Id`` is chain-local and sparse: 10.0.1's master chain reads
  Eq8=4, PluginDevice=2, PluginDevice=5, PluginDevice=3 in document order,
  so the attribute is an allocation counter, not a position.
* Chains nested inside a rack restart those ids at 0. 10.0.1's
  ``AudioEffectGroupDevice`` Id=3 holds two branches, each a ``Devices`` list
  with one device numbered 0.
* Every one of the 238 devices in the corpus carries ``On/Manual``, and 20 of
  them are switched off.
* The main track is ``MainTrack`` from Live 12 and ``MasterTrack`` before it,
  named "Main" and "Master" respectively, and its chain sits one tab
  shallower than a track's.
* ``PreHearTrack`` (the cue chain) exists in every fixture but 10.1.3 and is
  empty in all of them.
"""

from __future__ import annotations

import gzip
import pathlib
from xml.etree import ElementTree as ET

import pytest

from abletoolz.live_set import AbletonSet
from abletoolz.live_set.devices import DeviceRef

SKELETONS = pathlib.Path(__file__).parent / "version_fixtures" / "skeletons"


def make_set(key: str) -> AbletonSet:
    ableton_set = AbletonSet(SKELETONS / f"{key}.als")
    assert ableton_set.parse()
    return ableton_set


def raw_root(key: str) -> ET.Element:
    """Parse a fixture straight off disk, bypassing abletoolz entirely."""
    return ET.fromstring(gzip.decompress((SKELETONS / f"{key}.als").read_bytes()).decode("utf-8"))


def summary(key: str) -> list[tuple[str, str, tuple[tuple[str, str, bool], ...]]]:
    return [
        (chain.track_type, chain.track_name, tuple((d.tag, d.display_name, d.enabled) for d in chain.devices))
        for chain in make_set(key).devices.inventory()
    ]


def test_12_4_5b_inventory_covers_every_track_and_the_main_chain() -> None:
    assert summary("12.4.5b") == [
        ("MidiTrack", "1-LOW", (("PluginDevice", "LOW", True), ("PluginDevice", "V-Clip", True))),
        ("AudioTrack", "2-Drum fill", (("PluginDevice", "Decapitator", True),)),
        ("AudioTrack", "3-Skylark - Iced", ()),
        ("ReturnTrack", "A-Reverb", (("Reverb", "Reverb", True),)),
        ("ReturnTrack", "B-Delay", (("Delay", "Delay", True),)),
        ("MainTrack", "Main", (("PluginDevice", "FabFilter Pro-L 2", True),)),
    ]


def test_9_6_0_names_vst2_devices_and_reports_the_bypassed_ones() -> None:
    """Pre-10 chains read fine; only writing into them is refused."""
    assert summary("9.6.0")[0] == (
        "MidiTrack",
        "1-Pro-53",
        (
            ("PluginDevice", "Pro-53", True),
            ("PluginDevice", "Permut8", False),
            ("PluginDevice", "FabFilter Timeless 2", True),
            ("AutoFilter", "AutoFilter", True),
            ("Compressor2", "Compressor2", True),
            ("Eq8", "Eq8", True),
        ),
    )
    assert summary("9.6.0")[-1] == ("MasterTrack", "Master", (("PluginDevice", "FabFilter Pro-L", True),))


def test_11_3_41_names_audio_units_from_au_plugin_info() -> None:
    assert summary("11.3.41") == [
        ("MidiTrack", "1-MIDI", (("AuPluginDevice", "AUBandpass", True), ("AuPluginDevice", "AULowpass", True))),
        ("AudioTrack", "2-Audio", (("AuPluginDevice", "AUPitch", True),)),
        ("MidiTrack", "3-Analog Lab V", (("AuPluginDevice", "Analog Lab V", True),)),
        ("MasterTrack", "Master", ()),
    ]


def test_10_0_1_tracks_are_named_the_way_tracks_py_names_them() -> None:
    """A user-renamed track answers to its UserName, not its EffectiveName."""
    names = [chain.track_name for chain in make_set("10.0.1").devices.inventory()]
    assert names == [
        "Drum Bus",
        "Kicks",
        "3-Audio",
        "10-Maschine 2",
        "12-Group",
        "Bass",
        "Kick SC",
        "B-Guitar Rig 5",
        "Master",
    ]


def test_device_ids_are_a_sparse_chain_local_counter() -> None:
    """10.0.1's master chain: ids 4, 2, 5, 3 in the order Live runs them."""
    main = make_set("10.0.1").devices.inventory()[-1]
    assert [device.device_element.get("Id") for device in main.devices] == ["4", "2", "5", "3"]
    assert [device.display_name for device in main.devices] == [
        "Eq8",
        "FabFilter Pro-L.64",
        "iZotope Ozone 4",
        "SPAN",
    ]
    assert [device.enabled for device in main.devices] == [True, False, True, True]


def test_a_rack_nests_whole_chains_that_restart_device_ids() -> None:
    """The rack's branches each hold a Devices list of their own, numbered from 0."""
    drum_bus = make_set("10.0.1").devices.inventory()[0]
    rack = next(device for device in drum_bus.devices if device.tag == "AudioEffectGroupDevice")
    assert rack.device_element.get("Id") == "3"
    nested = [
        (nested_device.tag, nested_device.get("Id"))
        for branch in rack.device_element.iter("Devices")
        for nested_device in branch
    ]
    assert nested == [("Saturator", "0"), ("Eq8", "0")]


@pytest.mark.parametrize(
    ("key", "tag", "name"),
    [
        ("9.0.1", "MasterTrack", "Master"),
        ("10.1.3", "MasterTrack", "Master"),
        ("11.2.10", "MasterTrack", "Master"),
        ("12.2.6", "MainTrack", "Main"),
        ("12.4.5b", "MainTrack", "Main"),
    ],
)
def test_the_main_chain_is_last_and_follows_the_version_rename(key: str, tag: str, name: str) -> None:
    last = make_set(key).devices.inventory()[-1]
    assert (last.track_type, last.track_name) == (tag, name)
    assert raw_root(key).find(f"LiveSet/{tag}") is not None


def test_the_cue_chain_is_not_part_of_the_inventory() -> None:
    """PreHearTrack has a device chain but is not a track anyone authors."""
    assert raw_root("12.4.5b").find("LiveSet/PreHearTrack/DeviceChain/DeviceChain/Devices") is not None
    assert "PreHearTrack" not in {chain.track_type for chain in make_set("12.4.5b").devices.inventory()}


def test_empty_chains_are_reported_rather_than_skipped() -> None:
    empty = [chain.track_name for chain in make_set("11.2.10").devices.inventory() if not chain.devices]
    assert empty == ["1-MIDI", "2-MIDI", "3-DJ SAMPLES VOL 2 (2)", "4-GB_DrumHit_01", "Master"]


def test_every_chain_carries_the_sets_version() -> None:
    assert {chain.version for chain in make_set("11.0.12").devices.inventory()} == {(11, 0, 12)}


def test_device_ref_compares_on_content_not_on_tree_identity() -> None:
    """The element is a handle for the write path, not part of what a ref means."""
    element = ET.Element("Eq8")
    other = ET.Element("Eq8")
    assert DeviceRef("Eq8", "Eq8", True, element) == DeviceRef("Eq8", "Eq8", True, other)
    assert "Element" not in repr(DeviceRef("Eq8", "Eq8", True, element))
