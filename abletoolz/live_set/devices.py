"""Device chains: reading what a track runs, and transplanting chains between sets.

Devices sit at ``Track/DeviceChain/DeviceChain/Devices/*``, one element per
device, native and hosted side by side: ``Eq8``, ``Reverb``, ``Compressor2``
and ``PluginDevice``/``AuPluginDevice`` are siblings distinguished only by
tag. Every track type carries a chain -- audio, MIDI, return, group -- and so
does the main track, which lives at ``LiveSet/MainTrack`` in Live 12 and
``LiveSet/MasterTrack`` before it (``tracks.main()`` finds either). Its
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
* The set-global pointee space (``Pointee``, every ``*AutomationTarget``/
  ``*ModulationTarget`` and ``ControllerTargets.<N>``, counted by
  ``LiveSet/NextPointeeId``) is where the real work is: a device owns one id
  per automatable parameter -- a single ``Reverb`` owns 54 of them -- so
  every graft renumbers hundreds of ids. Live 9 sets own these ids too but
  carry no ``NextPointeeId`` element, so there is no telling which ids are
  taken, and grafting into a pre-10 set is refused.
* Sidechain routing (``SideChain/RoutedInput/Routable/Target``) names another
  track through Live's own routing ids ("p46251/p59394.19/p39871" in 10.0.6),
  a fourth space that has nothing to do with pointee ids. It is carried
  verbatim -- correct inside one set, and a reference to a track that does
  not exist when the donor came from another one.
"""

from __future__ import annotations

import copy
import dataclasses
import enum
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Literal
from xml.etree import ElementTree as ET

from abletoolz.elements import drop_envelopes, get_element
from abletoolz.live_set.domain import Domain
from abletoolz.live_set.settings import Setting, resolve
from abletoolz.live_set.xml_edit import (
    owns_pointee_id,
    renumber_pointee_ids,
    shift_indentation,
    strip_remote_bindings,
    tab_depth,
)
from abletoolz.plugins.container.device import PluginDevice, is_info
from abletoolz.plugins.identity.ids import PluginKind
from abletoolz.schema import Attr, Tag, path
from abletoolz.schema.devices import SETTINGS_OF, Device
from abletoolz.schema.versioning import Version

if TYPE_CHECKING:
    from abletoolz.live_set.tracks import AbletonTrack


@dataclasses.dataclass(frozen=True, slots=True)
class DeviceRef:
    """One device in a chain.

    ``device_element`` is the live handle the write path needs, kept out of
    ``repr`` and equality so refs compare on what was parsed.
    """

    tag: str
    display_name: str
    enabled: bool
    device_element: ET.Element = dataclasses.field(repr=False, compare=False)
    version: Version = dataclasses.field(repr=False, compare=False)

    def setting(self, name: enum.StrEnum, **indices: int) -> Setting:
        """One of this device's settings, by its name in :mod:`abletoolz.schema.devices`.

        ``name`` has to belong to this device's own settings class -- a
        ``Sampler`` member on a ``Sampler``. A setting that lives in a list (an
        EQ band, a sample part) takes its position as keywords:
        ``setting(EqEight.FREQ, index=3)``. Hosted plugins have no settings
        here; their state is the plugin's own.
        """
        if self.tag not in SETTINGS_OF:
            raise ValueError(f"{self.display_name} ({self.tag}) has no native settings; its state is the plugin's own")
        settings = SETTINGS_OF[Device(self.tag)]
        if type(name) is not settings:
            raise ValueError(
                f"{type(name).__name__}.{name.name} is not a setting of {self.tag}; use {settings.__name__}"
            )
        return resolve(self.device_element, name, self.version, indices, label=self.display_name)


@dataclasses.dataclass(frozen=True, slots=True)
class TrackDevices:
    """One track's whole chain, in the order Live runs it.

    ``version`` travels with the chain because a donor is routinely read from
    a different set than the one it lands in, and ``graft_chain`` has to
    compare the two. ``track`` is the track the chain was read from, kept out
    of ``repr`` and equality the way ``DeviceRef`` keeps its element.
    """

    devices: tuple[DeviceRef, ...]
    version: Version
    track: AbletonTrack = dataclasses.field(repr=False, compare=False)

    @property
    def track_name(self) -> str:
        return self.track.name

    @property
    def track_type(self) -> str:
        return self.track.type


def _inner_chain(track_root: ET.Element) -> ET.Element:
    """The ``DeviceChain`` that holds ``Devices``, doubling as the indent anchor.

    Its own ``text`` is the indentation of ``Devices``, which makes every
    other depth in the graft derivable from one element.
    """
    return get_element(track_root, path(Tag.DEVICE_CHAIN, Tag.DEVICE_CHAIN))


class Devices(Domain):
    """Device chains of one set: what they hold, and grafting them around."""

    def inventory(self) -> list[TrackDevices]:
        """Every chain in the set, tracks in document order and the main track last.

        Tracks with an empty chain are included -- an empty chain is a fact
        about the track, and it is where a graft usually wants to land.
        """
        return [self.chain(track) for track in self._set.tracks.walk()]

    def chain(self, track: AbletonTrack) -> TrackDevices:
        """One track's chain, read from its own element."""
        devices = get_element(_inner_chain(track.track_root), Tag.DEVICES)
        return TrackDevices(
            devices=tuple(self._device_ref(device) for device in devices), version=self.version, track=track
        )

    def walk(self, kind: str | None = None, *, track: AbletonTrack | None = None) -> Iterator[DeviceRef]:
        """Every device in the set, racks walked into, optionally only one kind or one track.

        ``kind`` is a device tag, which :class:`~abletoolz.schema.devices.Device`
        names: ``walk(Device.SAMPLER)``. A rack's chains hold ``Devices`` lists
        of their own, and every one of them is under the track that owns the
        rack, so walking each track's whole subtree reaches all of them in
        document order: the rack, then what it holds. The main track comes last
        and the cue track is not walked, as in :meth:`inventory`.

        ``track`` has to be one of this set's own tracks.
        """
        tracks = self._set.tracks.walk() if track is None else [self._owned(track)]
        for selected in tracks:
            for devices in selected.track_root.iter(Tag.DEVICES):
                for element in devices:
                    if kind is None or element.tag == kind:
                        yield self._device_ref(element)

    def prepare_graft_chain(
        self,
        donor: TrackDevices,
        target: AbletonTrack,
        *,
        mode: Literal["append", "replace"] = "append",
    ) -> Callable[[], list[DeviceRef]]:
        """Check that ``donor`` can be grafted onto ``target``, and return the graft without running it.

        Everything that can refuse the graft is checked here, so a batch of
        prepared writes can be refused before any of them edits the set.
        :meth:`graft_chain` says what the graft does.
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
        track_root = self._owned(target).track_root

        def run() -> list[DeviceRef]:
            chain = _inner_chain(track_root)
            devices = get_element(chain, Tag.DEVICES)
            if mode == "replace":
                removed_ids = {
                    element.get(Attr.ID)
                    for existing in devices
                    for element in existing.iter()
                    if owns_pointee_id(element.tag) and element.get(Attr.ID) is not None
                }
                del devices[:]
                drop_envelopes(track_root, removed_ids)

            child_indent = (chain.text or "") + "\t"
            next_device_id = (
                max((int(device.attrib[Attr.ID]) for device in devices if Attr.ID in device.attrib), default=-1) + 1
            )
            grafted: list[DeviceRef] = []
            for offset, source in enumerate(donor.devices):
                grafted_element = copy.deepcopy(source.device_element)
                shift_indentation(grafted_element, tab_depth(child_indent) - (tab_depth(grafted_element.text) - 1))
                renumber_pointee_ids(grafted_element, self._root)
                strip_remote_bindings(grafted_element)
                grafted_element.set(Attr.ID, str(next_device_id + offset))
                devices.append(grafted_element)
                grafted.append(self._device_ref(grafted_element))

            # Live closes the list on the parent's indent, so only the last tail differs.
            for index, device in enumerate(devices):
                device.tail = chain.text if index == len(devices) - 1 else child_indent
            devices.text = child_indent if len(devices) else None
            return grafted

        return run

    def graft_chain(
        self,
        donor: TrackDevices,
        target: AbletonTrack,
        *,
        mode: Literal["append", "replace"] = "append",
    ) -> list[DeviceRef]:
        """Copy a whole chain onto ``target``, a track of this set, from this set or another one.

        Every device is deep-copied, re-indented to the target's depth, given
        fresh set-global ids, stripped of the donor's key/MIDI remote bindings
        (which crash Live 12 when they travel), and given a chain-local ``Id``
        past whatever the target chain already uses. ``append`` puts the copies
        after the existing devices; ``replace`` clears the chain first.

        Two things are left unexamined: the fixture corpus shows them but not
        what Live does with them after a graft. Sidechain routing (10.0.6's
        ``Gate`` names another track by routing id) is copied verbatim, so a
        cross-set graft lands a reference to a track that does not exist in
        the target. And a track's automation lives outside its chain, in
        ``AutomationEnvelopes/Envelopes`` on the track, so a graft carries
        none with it.

        ``replace`` drops the target's envelopes, its clips' envelopes too, that
        automate a parameter of a device it removes, since what they name is gone. Measured 2026-09-26
        recombining two finished songs: two of a base's eleven envelopes named
        parameters of the chain a replace took out, where the fixture corpus
        had shown none.
        """
        return self.prepare_graft_chain(donor, target, mode=mode)()

    def _owned(self, track: AbletonTrack) -> AbletonTrack:
        """``track`` as this set holds it, refused when it belongs to another set."""
        for owned in self._set.tracks.walk():
            if owned.track_root is track.track_root:
                return owned
        raise ValueError(f"Track {track.name!r} does not belong to this set")

    def _device_ref(self, element: ET.Element) -> DeviceRef:
        """``On/Manual`` is the enable switch, present on all 238 devices in the corpus."""
        return DeviceRef(
            tag=element.tag,
            display_name=self._display_name(element),
            enabled=get_element(element, path(Tag.ON, Tag.MANUAL), attribute=Attr.VALUE) == "true",
            device_element=element,
            version=self.version,
        )

    def _display_name(self, element: ET.Element) -> str:
        """A native device is its own tag; a hosted one names itself inside PluginDesc.

        The plugin device reader does the parsing, whichever format is inside.
        """
        plugin_desc = element.find(Tag.PLUGIN_DESC)
        info = None if plugin_desc is None else next((child for child in plugin_desc if is_info(child)), None)
        if info is None:
            return element.tag
        device = PluginDevice(info)
        name = device.identity.name
        if device.kind is PluginKind.VST:
            return name or device.reference.file_name or "<unknown>"
        return name or element.tag
