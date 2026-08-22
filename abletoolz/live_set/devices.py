"""Device chains: reading what a track runs, and transplanting chains between sets.

Devices sit at ``Track/DeviceChain/DeviceChain/Devices/*``, one element per
device, native and hosted side by side: ``Eq8``, ``Reverb``, ``Compressor2``
and ``PluginDevice``/``AuPluginDevice`` are siblings distinguished only by
tag. Every track type carries a chain -- audio, MIDI, return, group -- and so
does the main track, which lives at ``LiveSet/MainTrack`` in Live 12 and
``LiveSet/MasterTrack`` before it (``schema.tag("master_track", ...)``). Its
chain is nested one level shallower than a track's, which is why grafting
re-indents rather than copying whitespace along. The cue chain
(``PreHearTrack``) is deliberately not walked: it is not a track anyone
authors.

Three id spaces meet in a device chain and must not be confused.

* ``Device/@Id`` is chain-local: unique inside its own ``Devices`` list,
  handed out by a counter that never reuses a number, so the values are
  sparse and unordered (10.0.1's master chain reads Eq8=4, PluginDevice=2, 5,
  3 in document order). Chains nested inside a rack restart at 0, so a rack's
  contents keep their ids when the rack moves.
* The set-global pointee space (``AutomationTarget``/``ModulationTarget``/
  ``Pointee``, counted by ``LiveSet/NextPointeeId``) is where the real work
  is: a device owns one id per automatable parameter -- a single ``Reverb``
  owns 54 of them -- so every graft renumbers hundreds of ids. Live 9 sets
  own these ids too but carry no ``NextPointeeId`` element, leaving no
  measurable way to tell Live which ids are already taken, so grafting into a
  pre-10 set is refused rather than guessed at.
* Sidechain routing (``SideChain/RoutedInput/Routable/Target``) names another
  track through Live's own routing ids ("p46251/p59394.19/p39871" in 10.0.6),
  a fourth space that has nothing to do with pointee ids. It is carried
  verbatim -- correct inside one set, and a reference to a track that does
  not exist when the donor came from another one.
"""

from __future__ import annotations

import copy
import dataclasses
from typing import TYPE_CHECKING, Literal
from xml.etree import ElementTree as ET

from abletoolz import schema
from abletoolz.live_set.xml_edit import renumber_pointee_ids, shift_indentation, tab_depth
from abletoolz.misc import get_element
from abletoolz.plugin_parsers import PluginData
from abletoolz.versioning import Version

if TYPE_CHECKING:
    from abletoolz.live_set.document import AbletonSet


@dataclasses.dataclass(frozen=True, slots=True)
class DeviceRef:
    """One device in a chain.

    ``device_element`` is the live handle the write path needs, held the same
    way ``MidiClipRef`` holds its clip: out of ``repr`` and out of equality,
    so refs compare on what was parsed rather than on tree identity.
    """

    tag: str
    display_name: str
    enabled: bool
    device_element: ET.Element = dataclasses.field(repr=False, compare=False)


@dataclasses.dataclass(frozen=True, slots=True)
class TrackDevices:
    """One track's whole chain, in the order Live runs it.

    ``version`` travels with the chain because a donor is routinely read from
    a different set than the one it lands in, and ``graft_chain`` has to
    compare the two.
    """

    track_name: str
    track_type: str
    devices: tuple[DeviceRef, ...]
    version: Version


def _inner_chain(track_root: ET.Element) -> ET.Element:
    """The ``DeviceChain`` that holds ``Devices``, doubling as the indent anchor.

    Its own ``text`` is the indentation of ``Devices``, which makes every
    other depth in the graft derivable from one element.
    """
    return get_element(track_root, "DeviceChain.DeviceChain")


class Devices:
    """Device chains of one set: what they hold, and grafting them around."""

    def __init__(self, live_set: AbletonSet) -> None:
        self._set = live_set

    @property
    def version(self) -> Version:
        return self._set.version_tuple

    @property
    def _root(self) -> ET.Element:
        return self._set.root

    def inventory(self) -> list[TrackDevices]:
        """Every chain in the set, tracks in document order and the main track last.

        Tracks with an empty chain are included -- an empty chain is a fact
        about the track, and it is where a graft usually wants to land.
        """
        chains = [self._track_devices(track.track_root, track.type, track.name) for track in self._set.tracks.load()]
        main = self._main_track()
        chains.append(self._track_devices(main, main.tag, get_element(main, "Name.EffectiveName", attribute="Value")))
        return chains

    def graft_chain(
        self,
        donor: TrackDevices,
        target_track_name: str,
        *,
        mode: Literal["append", "replace"] = "append",
    ) -> list[DeviceRef]:
        """Copy a whole chain onto another track, in this set or from another one.

        Every device is deep-copied, re-indented to the target's depth, given
        fresh set-global ids, and given a chain-local ``Id`` past whatever the
        target chain already uses. ``append`` puts the copies after the
        existing devices; ``replace`` clears the chain first.

        Two things ride along unexamined, because the fixture corpus shows
        them but cannot show what Live does with them after a graft. Sidechain
        routing (10.0.6's ``Gate`` names another track by routing id) is
        copied verbatim, so a cross-set graft lands a reference to a track
        that does not exist in the target. And a track's automation lives
        outside its chain, in ``DeviceChain/AutomationEnvelopes``, so a graft
        carries no automation with it and ``replace`` leaves the target's own
        envelopes naming devices that are gone -- neither case occurs in the
        corpus, where no envelope names a device parameter at all.
        """
        if donor.version[0] != self.version[0]:
            raise ValueError(
                f"Cannot graft a Live {donor.version[0]}.x chain into a Live {self.version[0]}.x set; "
                "device schemas are only known to match within a major version"
            )
        if self.version < (10, 0, 0):
            raise ValueError(
                f"Live {self.version[0]}.x sets carry no LiveSet/NextPointeeId, so there is no allocator to take "
                "fresh device parameter ids from; grafting into them is unsupported"
            )

        chain = self._chain_of(target_track_name)
        devices = get_element(chain, "Devices")
        if mode == "replace":
            for existing in list(devices):
                devices.remove(existing)

        child_indent = (chain.text or "") + "\t"
        next_device_id = max((int(device.attrib["Id"]) for device in devices if "Id" in device.attrib), default=-1) + 1
        grafted: list[DeviceRef] = []
        for offset, source in enumerate(donor.devices):
            grafted_element = copy.deepcopy(source.device_element)
            shift_indentation(grafted_element, tab_depth(child_indent) - (tab_depth(grafted_element.text) - 1))
            renumber_pointee_ids(grafted_element, self._root)
            grafted_element.set("Id", str(next_device_id + offset))
            devices.append(grafted_element)
            grafted.append(self._device_ref(grafted_element))

        # Live closes the list on the parent's indent, so only the last tail differs.
        for index, device in enumerate(devices):
            device.tail = chain.text if index == len(devices) - 1 else child_indent
        devices.text = child_indent if len(devices) else None
        return grafted

    def _main_track(self) -> ET.Element:
        return get_element(self._root, f"LiveSet.{schema.tag('master_track', self.version)}")

    def _chain_of(self, track_name: str) -> ET.Element:
        """The inner chain of the named track. Duplicate names resolve to the first."""
        for track in self._set.tracks.load():
            if track.name == track_name:
                return _inner_chain(track.track_root)
        main = self._main_track()
        if get_element(main, "Name.EffectiveName", attribute="Value") == track_name:
            return _inner_chain(main)
        raise ValueError(f"No track named {track_name!r} in this set")

    def _track_devices(self, track_root: ET.Element, track_type: str, track_name: str) -> TrackDevices:
        devices = get_element(_inner_chain(track_root), "Devices")
        return TrackDevices(
            track_name=track_name,
            track_type=track_type,
            devices=tuple(self._device_ref(device) for device in devices),
            version=self.version,
        )

    def _device_ref(self, element: ET.Element) -> DeviceRef:
        """``On/Manual`` is the enable switch, present on all 238 devices in the corpus."""
        return DeviceRef(
            tag=element.tag,
            display_name=self._display_name(element),
            enabled=get_element(element, "On.Manual", attribute="Value") == "true",
            device_element=element,
        )

    def _display_name(self, element: ET.Element) -> str:
        """A native device is its own tag; a hosted one names itself inside PluginDesc.

        Each format keeps the name in a different child, and every fixture
        carries exactly one of the three, so the readers in ``plugins.py`` and
        ``plugin_parsers`` do the parsing rather than a fourth copy of it.
        """
        vst3_element = element.find("PluginDesc/Vst3PluginInfo")
        if vst3_element is not None:
            name, _ = self._set.plugins.parse_vst3_element(vst3_element)
            return name or element.tag
        au_element = element.find("PluginDesc/AuPluginInfo")
        if au_element is not None:
            name, _, _ = self._set.plugins.parse_au_element(au_element)
            return name or element.tag
        vst_element = element.find("PluginDesc/VstPluginInfo")
        if vst_element is not None:
            return PluginData.from_element(vst_element).plugin_name
        return element.tag
