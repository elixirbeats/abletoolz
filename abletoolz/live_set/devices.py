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
* The set-global pointee space (``Pointee``, every ``*AutomationTarget``/
  ``*ModulationTarget`` and ``ControllerTargets.<N>``, counted by
  ``LiveSet/NextPointeeId``) is where the real work is: a device owns one id
  per automatable parameter -- a single ``Reverb`` owns 54 of them -- so
  every graft renumbers hundreds of ids. Live 9 sets
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
import enum
from collections.abc import Iterator
from typing import TYPE_CHECKING, Literal
from xml.etree import ElementTree as ET

from abletoolz import schema
from abletoolz.live_set.xml_edit import (
    number_value,
    owns_pointee_id,
    renumber_pointee_ids,
    shift_indentation,
    strip_remote_bindings,
    tab_depth,
)
from abletoolz.misc import ElementNotFound, get_element
from abletoolz.plugin_parsers import PluginData
from abletoolz.schema import Attr, Tag, path
from abletoolz.schema.devices import SETTINGS_OF, Device, ValueKind, is_templated, kind_of
from abletoolz.schema.versioning import Version

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
        if is_templated(name) and not indices:
            raise ValueError(f"{settings.__name__}.{name.name} is in a list; say which one, e.g. index=0")
        setting_path = name.format(**indices)
        element = self.device_element.find(setting_path)
        if element is None:
            raise ElementNotFound(f"{self.display_name} has no {setting_path} (the set's Live version may predate it)")
        # A parameter keeps its value in Manual; a setting that later became one did not.
        manual = element.find(Tag.MANUAL)
        return Setting(path=setting_path, kind=kind_of(name), element=element if manual is None else manual)


@dataclasses.dataclass(frozen=True, slots=True)
class Setting:
    """One setting of a native device, read and written as the value it is.

    ``kind`` is what the census saw the setting hold. Whole numbers are what
    Live writes for any number that sits on one, so INT and FLOAT both read as
    a number and take any number on write.
    """

    path: str
    kind: ValueKind
    element: ET.Element = dataclasses.field(repr=False, compare=False)

    @property
    def raw(self) -> str:
        """The ``Value`` attribute exactly as the set holds it."""
        value = self.element.get(Attr.VALUE)
        if value is None:
            raise ElementNotFound(f"{self.path} has no Value attribute")
        return value

    @property
    def value(self) -> bool | int | float | str:
        raw = self.raw
        if self.kind is ValueKind.BOOL:
            return raw == "true"
        if self.kind in (ValueKind.INT, ValueKind.FLOAT):
            number = float(raw)
            return int(number) if number.is_integer() else number
        return raw

    def encode(self, value: bool | float | str) -> str:
        """``value`` as Live writes it, refusing one of the wrong kind."""
        if self.kind is ValueKind.BOOL:
            if not isinstance(value, bool):
                raise TypeError(f"{self.path} is a switch; give True or False, not {value!r}")
            return "true" if value else "false"
        if self.kind in (ValueKind.INT, ValueKind.FLOAT):
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise TypeError(f"{self.path} is a number, not {value!r}")
            return number_value(value)
        if not isinstance(value, str):
            raise TypeError(f"{self.path} holds text, not {value!r}")
        return value

    def set(self, value: bool | float | str) -> None:
        """Write ``value`` the way Live would."""
        self.element.set(Attr.VALUE, self.encode(value))


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
    return get_element(track_root, path(Tag.DEVICE_CHAIN, Tag.DEVICE_CHAIN))


def _drop_envelopes(track_root: ET.Element, target_ids: set[str | None]) -> None:
    """Remove the track's automation envelopes whose target is one of ``target_ids``.

    The list closes on its parent's indent through the last child's tail, so
    removing the last envelope hands its tail to the one before it.
    """
    envelopes = track_root.find(path(Tag.AUTOMATION_ENVELOPES, Tag.ENVELOPES))
    if envelopes is None:
        return
    for envelope in list(envelopes):
        target = envelope.find(path(Tag.ENVELOPE_TARGET, Tag.POINTEE_ID))
        if target is None or target.get(Attr.VALUE) not in target_ids:
            continue
        index = list(envelopes).index(envelope)
        if index == len(envelopes) - 1 and index > 0:
            envelopes[index - 1].tail = envelope.tail
        envelopes.remove(envelope)
    if not len(envelopes):
        envelopes.text = None


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
        chains.append(
            self._track_devices(
                main, main.tag, get_element(main, path(Tag.NAME, Tag.EFFECTIVE_NAME), attribute=Attr.VALUE)
            )
        )
        return chains

    def walk(self, kind: str | None = None, *, track_name: str | None = None) -> Iterator[DeviceRef]:
        """Every device in the set, racks walked into, optionally only one kind or one track.

        ``kind`` is a device tag, which :class:`~abletoolz.schema.devices.Device`
        names: ``walk(Device.SAMPLER)``. A rack's chains hold ``Devices`` lists
        of their own, and every one of them is under the track that owns the
        rack, so walking each track's whole subtree reaches all of them in
        document order: the rack, then what it holds. The main track comes last
        and the cue track is not walked, as in :meth:`inventory`.
        """
        tracks = [(track.name, track.track_root) for track in self._set.tracks.load()]
        main = self._main_track()
        tracks.append((get_element(main, path(Tag.NAME, Tag.EFFECTIVE_NAME), attribute=Attr.VALUE), main))
        for name, track_root in tracks:
            if track_name is not None and name != track_name:
                continue
            for devices in track_root.iter(Tag.DEVICES):
                for element in devices:
                    if kind is None or element.tag == kind:
                        yield self._device_ref(element)

    def graft_chain(
        self,
        donor: TrackDevices,
        target_track_name: str,
        *,
        mode: Literal["append", "replace"] = "append",
    ) -> list[DeviceRef]:
        """Copy a whole chain onto another track, in this set or from another one.

        Every device is deep-copied, re-indented to the target's depth, given
        fresh set-global ids, stripped of the donor's key/MIDI remote bindings
        (which crash Live 12 when they travel), and given a chain-local ``Id``
        past whatever the target chain already uses. ``append`` puts the copies
        after the existing devices; ``replace`` clears the chain first.

        Two things ride along unexamined, because the fixture corpus shows
        them but cannot show what Live does with them after a graft. Sidechain
        routing (10.0.6's ``Gate`` names another track by routing id) is
        copied verbatim, so a cross-set graft lands a reference to a track
        that does not exist in the target. And a track's automation lives
        outside its chain, in ``AutomationEnvelopes/Envelopes`` on the track,
        so a graft carries no automation with it.

        ``replace`` drops the target's envelopes that automate a parameter of a
        device it removes, since what they name is gone. Measured 2026-09-26
        recombining two finished songs: two of a base's eleven envelopes named
        parameters of the chain a replace took out, where the fixture corpus
        had shown none.
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

        track_root = self._track_root(target_track_name)
        chain = _inner_chain(track_root)
        devices = get_element(chain, Tag.DEVICES)
        if mode == "replace":
            removed_ids = {
                element.get(Attr.ID)
                for existing in devices
                for element in existing.iter()
                if owns_pointee_id(element.tag) and element.get(Attr.ID) is not None
            }
            for existing in list(devices):
                devices.remove(existing)
            _drop_envelopes(track_root, removed_ids)

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

    def _main_track(self) -> ET.Element:
        return get_element(self._root, path(Tag.LIVE_SET, schema.tag("master_track", self.version)))

    def _track_root(self, track_name: str) -> ET.Element:
        """The named track's element. Duplicate names resolve to the first."""
        for track in self._set.tracks.load():
            if track.name == track_name:
                return track.track_root
        main = self._main_track()
        if get_element(main, path(Tag.NAME, Tag.EFFECTIVE_NAME), attribute=Attr.VALUE) == track_name:
            return main
        raise ValueError(f"No track named {track_name!r} in this set")

    def _track_devices(self, track_root: ET.Element, track_type: str, track_name: str) -> TrackDevices:
        devices = get_element(_inner_chain(track_root), Tag.DEVICES)
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
            enabled=get_element(element, path(Tag.ON, Tag.MANUAL), attribute=Attr.VALUE) == "true",
            device_element=element,
        )

    def _display_name(self, element: ET.Element) -> str:
        """A native device is its own tag; a hosted one names itself inside PluginDesc.

        Each format keeps the name in a different child, and every fixture
        carries exactly one of the three, so the readers in ``plugins.py`` and
        ``plugin_parsers`` do the parsing rather than a fourth copy of it.
        """
        vst3_element = element.find(path(Tag.PLUGIN_DESC, Tag.VST3_PLUGIN_INFO))
        if vst3_element is not None:
            name, _ = self._set.plugins.parse_vst3_element(vst3_element)
            return name or element.tag
        au_element = element.find(path(Tag.PLUGIN_DESC, Tag.AU_PLUGIN_INFO))
        if au_element is not None:
            name, _, _ = self._set.plugins.parse_au_element(au_element)
            return name or element.tag
        vst_element = element.find(path(Tag.PLUGIN_DESC, Tag.VST_PLUGIN_INFO))
        if vst_element is not None:
            return PluginData.from_element(vst_element).plugin_name
        return element.tag
