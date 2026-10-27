"""Files on disk that plugin devices' patches point at, and relinking them.

A few plugins keep the path of the sample or kit they play inside their own
patch (see :class:`~abletoolz.plugins.state.plugin_map.FilePaths`). When the
file moves, the device still loads and plays nothing, and Live does not say so:
it only relinks its own sample references. This is the check and fix
:mod:`abletoolz.live_set.samples` does for Live's references, through the same
lookup (:mod:`abletoolz.samples.relink`). Audio a patch points at is found in
the sample database and may be collected into the project like any sample; a
file only the plugin loads, such as a Kontakt instrument, is found in the plugin
files database and relinked where it is, since it belongs with its library and
a copy on its own would break. A patch usually stores nothing about the file
beside its path, so a name that several files share is reported rather than
guessed at.

A device is found by the id it stores, in whichever format it is, and its patch
is lifted out of that format by its plugin's map; a relinked patch is lowered
back into the same format. A plugin whose map says it rewrites nothing is
reported and never written.
"""

from __future__ import annotations

import dataclasses
import logging
import pathlib
from collections.abc import Iterator
from typing import TYPE_CHECKING

from abletoolz.ansi import G, Y
from abletoolz.plugins.container.device import PluginDevice, PluginDevices
from abletoolz.plugins.identity.ids import PluginKind, stored_id
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.plugin_map import FilePaths, FileRef, FormatState, PluginMap
from abletoolz.samples import relink
from abletoolz.samples.catalog import SampleCatalog
from abletoolz.samples.discovery import AUDIO_SUFFIXES

if TYPE_CHECKING:
    from abletoolz.live_set.document import AbletonSet

logger = logging.getLogger(__name__)


def on_disk(stored: str) -> pathlib.Path:
    """A path as a patch stores it, readable on this OS; plugins write either separator."""
    return pathlib.Path(stored.replace("\\", "/"))


@dataclasses.dataclass(frozen=True)
class PluginFile:
    """One file a plugin device points at, or, with ``ref`` None, a note about a patch.

    ``note`` says what an empty patch means for that plugin, or why a patch
    could not be read at all. ``rewrites`` is whether abletoolz can relink this
    plugin's files.
    """

    track: str
    device: str
    plugin: str
    ref: FileRef | None
    exists: bool
    rewrites: bool
    note: str | None = None

    @property
    def name(self) -> str:
        """The file's own name, which is what a missing-sample count is keyed by."""
        return self.device if self.ref is None else on_disk(self.ref.path).name


@dataclasses.dataclass(frozen=True)
class _Device:
    device: PluginDevice
    plugin_map: PluginMap
    file_paths: FilePaths
    patch: bytes
    refs: tuple[FileRef, ...]
    track: str

    @property
    def name(self) -> str:
        return self.device.identity.name or self.plugin_map.name


class PluginFiles:
    """The files plugin devices in one set point at."""

    def __init__(self, live_set: AbletonSet) -> None:
        self._set = live_set

    def _devices(self) -> Iterator[_Device | PluginFile]:
        """Every device whose plugin keeps file paths, read; or a note saying why one could not be."""
        for device in PluginDevices(self._set.root):
            stored = stored_id(device.identity)
            plugin_map = None if stored is None else CATALOG.find(stored)
            if plugin_map is None or plugin_map.file_paths is None or device.kind not in plugin_map.formats:
                continue
            saved = device.saved_state
            if not saved.state:
                continue
            track = self._set.plugins.track_name(device.info)
            try:
                patch = plugin_map.lift(FormatState(saved.state, saved.controller), device.kind)
                refs = plugin_map.file_paths.read(patch)
            except StateTransformError as error:
                name = device.identity.name or plugin_map.name
                note = f"patch could not be read, so its files were not checked: {error}"
                yield PluginFile(track, name, plugin_map.name, None, False, False, note)
                continue
            yield _Device(device, plugin_map, plugin_map.file_paths, patch, refs, track)

    def _resolve(self, stored: str) -> pathlib.Path:
        """Where a stored path points on this machine: a relative one is relative to the project."""
        path = on_disk(stored)
        project_root = self._set.project_root
        return path if path.is_absolute() or project_root is None else project_root / path

    def iterate(self) -> Iterator[PluginFile]:
        """Every file every plugin device points at, and a note for every patch worth one."""
        for found in self._devices():
            if isinstance(found, PluginFile):
                yield found
                continue
            if not found.refs:
                note = found.file_paths.when_empty
                if note is not None:
                    yield PluginFile(found.track, found.name, found.plugin_map.name, None, False, False, note)
                continue
            for ref in found.refs:
                exists = self._resolve(ref.path).exists()
                yield PluginFile(found.track, found.name, found.plugin_map.name, ref, exists, found.file_paths.rewrites)

    def check(self) -> list[PluginFile]:
        """Every file a plugin device points at that is not on disk."""
        return [found for found in self.iterate() if found.ref is not None and not found.exists]

    def notes(self) -> list[PluginFile]:
        """Every patch that points at no file where that means something, or could not be read."""
        return [found for found in self.iterate() if found.ref is None]

    def fix(
        self,
        db: SampleCatalog,
        collector: relink.Collector | None = None,
        plugin_files_db: SampleCatalog | None = None,
        refused: list[relink.Refusal] | None = None,
    ) -> list[relink.Relink]:
        """Relink every missing file a plugin can be relinked for: audio through ``db``, the rest through
        ``plugin_files_db``.

        With a ``collector`` found audio is collected into the project's
        Samples/Imported folder, the way Live's collect and save does, and the
        plugin is pointed at the copy's absolute path, made when the set is saved. A
        file only the plugin loads is never collected but relinked where it was
        found; without ``plugin_files_db`` it is reported, not relinked. A
        device is rewritten in its own format, or left exactly as it was. A
        file left as it was for any reason but not being found goes into
        ``refused``.
        """
        refused = [] if refused is None else refused
        relinks: list[relink.Relink] = []
        for found in self._devices():
            if isinstance(found, PluginFile):
                logger.warning("%s%s on %s: %s", Y, found.device, found.track, found.note)
                continue
            patch, relinked = found.patch, []
            for ref in found.refs:
                stored = self._resolve(ref.path)
                if stored.exists():
                    continue
                if not found.file_paths.rewrites:
                    logger.warning("%s%s on %s: cannot relink %s here", Y, found.name, found.track, ref.path)
                    refused.append(
                        relink.Refusal(
                            f"{ref.path} for {found.name} on {found.track}",
                            "this plugin's files are reported, not relinked",
                        )
                    )
                    continue
                audio = stored.suffix.lower() in AUDIO_SUFFIXES
                searched = db if audio else plugin_files_db
                if searched is None:
                    logger.warning(
                        "%s%s on %s: no plugin files database to find %s in; run --db over its library folder",
                        Y,
                        found.name,
                        found.track,
                        stored.name,
                    )
                    refused.append(
                        relink.Refusal(
                            f"{stored.name} for {found.name} on {found.track}",
                            "no plugin files database to find it in; run --db over its library folder",
                        )
                    )
                    continue
                located = relink.locate(
                    searched, _evidence(ref, stored), f"{stored.name} for {found.name} on {found.track}", refused
                )
                if located is None:
                    continue
                target = located if collector is None or not audio else collector.plan(located, relink.COLLECT_FOLDER)
                if target is None:
                    continue
                written_path = target.absolute()
                try:
                    patch = found.file_paths.rewrite(patch, ref, written_path)
                except StateTransformError as error:
                    logger.warning("%s%s on %s: %s", Y, found.name, found.track, error)
                    refused.append(relink.Refusal(f"{stored.name} for {found.name} on {found.track}", str(error)))
                    continue
                relinked.append(relink.Relink(ref.path, str(written_path), device=found.name, track=found.track))
            if not relinked:
                continue
            try:
                self._write(found, patch)
            except StateTransformError as error:
                logger.warning(
                    "%s%s on %s: relinked patch could not be written back: %s", Y, found.name, found.track, error
                )
                refused.append(
                    relink.Refusal(
                        f"{found.name} on {found.track}", f"relinked patch could not be written back: {error}"
                    )
                )
                continue
            for fix in relinked:
                logger.info("%sRelinked %s on %s: %s -> %s", G, fix.device, fix.track, fix.old, fix.new)
            relinks.extend(relinked)
        return relinks

    def _write(self, found: _Device, patch: bytes) -> None:
        written = found.plugin_map.lower(patch, found.device.kind)
        found.device.set_state(written.state)
        if found.device.kind is PluginKind.VST3 and written.controller is not None:
            found.device.set_controller(written.controller)


def _evidence(ref: FileRef, stored: pathlib.Path) -> relink.Evidence:
    """What a plugin's patch says about the file it points at: whatever its map reads besides the path."""
    return relink.Evidence(stored.name, stored, length=ref.length)
