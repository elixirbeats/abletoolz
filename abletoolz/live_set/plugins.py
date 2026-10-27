"""Plugin reference scanning and inspection."""

from __future__ import annotations

import dataclasses
import logging
import pathlib
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

from abletoolz.live_set.domain import Domain
from abletoolz.live_set.tracks import track_label
from abletoolz.plugins.container.device import PluginDevice, PluginDevices
from abletoolz.plugins.identity.ids import PluginKind, stored_id
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.framings import detect
from abletoolz.plugins.state.plugin_map import FormatState

if TYPE_CHECKING:
    from abletoolz.live_set.document import AbletonSet
    from abletoolz.plugins.loadable import LoadableOracle

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class PluginRef:
    """One plugin reference found in a set."""

    kind: PluginKind
    name: str | None
    path: pathlib.Path | None
    exists: bool
    alternative: pathlib.Path | None
    track_location: str
    manufacturer: str | None = None


def _preview(data: bytes, max_hex: int, max_decoded: int) -> list[str]:
    hex_preview = data[:max_hex].hex(" ", 1).upper()
    if len(data) > max_hex:
        hex_preview += f" ... ({len(data) - max_hex} more bytes)"
    text = data[:max_decoded].decode("utf-8", errors="replace")
    cleaned = "".join(char if char.isprintable() else "·" for char in text)
    return [f"  hex: {hex_preview}", f"  utf-8: {cleaned}"]


def dump_device(device: PluginDevice, track: str, *, max_hex: int = 256, max_decoded: int = 200) -> str:
    """One device's saved bytes, and what abletoolz knows about them."""
    identity, saved = device.identity, device.saved_state
    stored = stored_id(identity)
    plugin_map = None if stored is None else CATALOG.find(stored)
    lines = [
        f"{identity.name or '<unknown>'} ({device.kind.value}) on {track}",
        f"Stored id: {stored}",
        f"Plugin map: {'not mapped' if plugin_map is None else f'{plugin_map.name} [{plugin_map.id}]'}",
        f"Saved state: {len(saved.state)} bytes, {detect(saved.state).value}",
    ]
    if saved.controller:
        lines.append(f"Controller state: {len(saved.controller)} bytes, {detect(saved.controller).value}")
    lines.extend(_preview(saved.state, max_hex, max_decoded))
    if plugin_map is not None and saved.state:
        try:
            patch = plugin_map.lift(FormatState(saved.state, saved.controller), device.kind)
        except StateTransformError as error:
            lines.append(f"Own patch: not lifted: {error}")
        else:
            if patch != saved.state:
                lines.append(f"Own patch: {len(patch)} bytes, {detect(patch).value}")
                lines.extend(_preview(patch, max_hex, max_decoded))
            file_paths = plugin_map.file_paths
            if file_paths is not None:
                refs = file_paths.read(patch)
                lines.extend(f"File: {ref.kind} {ref.path}" for ref in refs)
                if not refs:
                    lines.append(
                        "File: none" if file_paths.when_empty is None else f"File: none -- {file_paths.when_empty}"
                    )
    return "\n".join(lines)


class Plugins(Domain):
    """Plugin references of one set."""

    def __init__(self, live_set: AbletonSet) -> None:
        super().__init__(live_set)
        self._document: PluginDevices | None = None

    def _devices(self) -> PluginDevices:
        """The set's plugin devices, read once; dropped by anything that moves elements."""
        if self._document is None:
            self._document = PluginDevices(self._root)
        return self._document

    def containing_track(self, element: ET.Element) -> ET.Element | None:
        """The track element holding ``element``, or None when it sits outside every track."""
        return self._devices().track_of(element)

    def track_name(self, element: ET.Element) -> str:
        """Name of the track holding ``element``; the track type when unnamed, '?' outside one."""
        track = self.containing_track(element)
        return "?" if track is None else track_label(track, self.version)

    def scan(self, oracle: LoadableOracle) -> list[PluginRef]:
        """Every plugin reference in the set, as ``oracle`` judges it.

        ``exists`` is whether Live could load the device; ``path`` is where the
        set says the plugin is, or for a reference that stores none (most VST3
        devices, every Audio Unit) where it was found. ``alternative`` is a copy
        found elsewhere when the stored path is gone, or a component bundle
        macOS has not registered.
        """
        refs: list[PluginRef] = []
        for device in self._devices():
            identity, stored = device.identity, device.reference.path
            exists = oracle.loads(device)
            if device.kind is PluginKind.AU:
                found = oracle.find(device)
                path, alternative = (found, None) if exists else (None, found)
            elif stored is None:
                path, alternative = oracle.find(device), None
            else:
                path, alternative = stored, None if stored.exists() else oracle.find(device)
            refs.append(
                PluginRef(
                    kind=device.kind,
                    name=device.reference.file_name if device.kind is PluginKind.VST else identity.name,
                    path=path,
                    exists=exists,
                    alternative=alternative,
                    track_location=self.track_name(device.info),
                    manufacturer=identity.manufacturer if device.kind is PluginKind.AU else None,
                )
            )
        return refs

    def dump(self, max_hex: int = 256, max_decoded: int = 200) -> list[str]:
        """Describe every plugin device's saved bytes, in any format, for working out a new plugin.

        Each device says which plugin map knows it, if any, what its saved
        bytes look like, the plugin's own patch where the map can lift it, and
        the files the patch names.
        """
        dumps: list[str] = []
        for device in self._devices():
            dump = dump_device(device, self.track_name(device.info), max_hex=max_hex, max_decoded=max_decoded)
            dumps.append(dump)
            print(dump)
            print("-" * 80)
        return dumps
