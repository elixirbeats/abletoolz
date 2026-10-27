"""Whether Live could load a plugin device on a computer today, and where its plugin is.

``--check-plugins`` asks this through ``Plugins.scan`` and a plan through
``Machine.here``, so the two give one answer. A plan marks a plugin broken when
it says no, and its starting pick gives a broken plugin a target.

Loadability is a machine question, not a set question, and it is the one thing
not taken from the plugin database. That database records identity -- names,
class ids, modules -- which is stable enough to keep in a file. Loadability
flips the moment the user toggles a plug-in folder in Live's preferences, and a
stale "yes" would leave a broken device alone, so it is read live, once per run,
and it gets two answers:

* Live's own plugin database, when there is one. Measured on Live 12.4.5b,
  ``plugin_domains.enabled`` is the only column that distinguishes anything: it
  follows the VST2 folder preference while every .dll stays on disk, and a
  module that failed the scan, or sits in a folder Live never scans, has no row
  either way. The full measurement, both times, is in
  :mod:`abletoolz.plugins.identity.uid_sources`. Live's database lists no Audio
  Units; those are asked of the component registry.
* Failing that -- no Live installed, another OS, a database whose schema moved
  on -- the plugin folders: a VST2 is loadable if its stored path is still there
  or its file turns up in a plugin folder, a VST3 if its stored path is there or
  its display name resolves to something installed.
"""

from __future__ import annotations

import functools
import logging
import pathlib
from collections.abc import Iterable
from xml.etree import ElementTree as ET

from abletoolz.machine import Locations
from abletoolz.plugins.container.device import PluginDevice
from abletoolz.plugins.identity.ids import AuIdentifier, PluginKind, UidFields
from abletoolz.plugins.identity.installed import (
    audio_component_registered,
    plist_au_identifiers,
    plist_declared_names,
)
from abletoolz.plugins.identity.uid_sources import DatabasePlugin, read_live_databases

logger = logging.getLogger(__name__)


class PluginFolders:
    """Every file and bundle under some plugin folders, read in one walk.

    A bundle -- a macOS ``.vst``, ``.vst3`` or ``.component``, a Windows
    ``.vst3`` folder -- is an entry of its own, and its contents are entries
    too, since a native binary can sit inside one. A VST3 answers to its stem
    and to every name its Info.plist declares; an Audio Unit bundle to every
    identity its Info.plist declares. The first folder to hold a name wins, and
    a folder that is not there holds nothing.
    """

    def __init__(self, folders: Iterable[pathlib.Path]) -> None:
        self._named: dict[str, list[pathlib.Path]] = {}
        self._vst3: dict[str, pathlib.Path] = {}
        self._au: dict[AuIdentifier, pathlib.Path] = {}
        for folder in dict.fromkeys(folders):
            if not folder.is_dir():
                continue
            for entry in folder.rglob("*"):
                self._named.setdefault(entry.name.casefold(), []).append(entry)
                suffix = entry.suffix.casefold()
                plist = entry / "Contents" / "Info.plist"
                if suffix == ".vst3":
                    self._vst3.setdefault(entry.stem, entry)
                    if entry.is_dir() and plist.is_file():
                        for name in plist_declared_names(plist):
                            self._vst3.setdefault(name, entry)
                elif suffix == ".component" and plist.is_file():
                    for identifier in plist_au_identifiers(plist):
                        self._au.setdefault(identifier, entry)

    def named(self, file_name: str) -> tuple[pathlib.Path, ...]:
        """Every file or bundle called ``file_name``, whatever its case."""
        return tuple(self._named.get(file_name.casefold(), ()))

    def vst3(self, display_name: str) -> pathlib.Path | None:
        """The installed VST3 that answers to ``display_name``."""
        return self._vst3.get(display_name)

    def au(self, identifier: AuIdentifier) -> pathlib.Path | None:
        """The component bundle that declares an exact Audio Unit identity."""
        return self._au.get(identifier)


class LoadableOracle:
    """Whether Live could load each plugin device on one computer today, and where its plugin is.

    Live's database, when it has one, matches a device by the id it stores
    first -- a VST2's UniqueId, a VST3's class id -- and by name only when it
    stores none. A set saved on Windows names its devices after their files
    ("ValhallaShimmer_x64") while Live's database names the plugin
    ("ValhallaShimmer"), and Live loads it by id. The plugin folders and Live's
    database are each read once, when first asked.
    """

    def __init__(self, locations: Locations) -> None:
        self.locations = locations

    @functools.cached_property
    def folders(self) -> PluginFolders:
        """The plugin folders, Audio Unit folders included."""
        return PluginFolders(self.locations.vst_dirs + self.locations.au_dirs)

    @functools.cached_property
    def database(self) -> tuple[DatabasePlugin, ...]:
        """Every plugin the newest usable Live database lists; empty without one."""
        directory = self.locations.live_database
        found = () if directory is None else read_live_databases(directory)
        if found:
            logger.debug("Judging plugins against %s entries in %s", len(found), directory)
        else:
            logger.debug("No Live plugin database to consult; judging plugins by what is on disk")
        return found

    @functools.cached_property
    def _usable(self) -> tuple[frozenset[int], frozenset[UidFields], frozenset[tuple[PluginKind, str]]]:
        usable = [plugin for plugin in self.database if plugin.loadable]
        return (
            frozenset(plugin.unique_id for plugin in usable if plugin.unique_id is not None),
            frozenset(plugin.uid_fields for plugin in usable if plugin.uid_fields is not None),
            frozenset((plugin.kind, plugin.name) for plugin in usable),
        )

    def __call__(self, info: ET.Element) -> bool:
        """Whether Live could load the device of one info element."""
        return self.loads(PluginDevice(info))

    def loads(self, device: PluginDevice) -> bool:
        """Whether Live could load ``device`` here today."""
        identity = device.identity
        if device.kind is PluginKind.AU:
            return self.registered(identity.au)
        if self.database:
            unique_ids, class_ids, names = self._usable
            if device.kind is PluginKind.VST and identity.unique_id is not None:
                return identity.unique_id in unique_ids
            if device.kind is PluginKind.VST3 and identity.uid_fields is not None:
                return identity.uid_fields in class_ids
            return (device.kind, identity.name) in names
        stored = device.reference.path
        return (stored is not None and stored.exists()) or self.find(device) is not None

    def registered(self, identifier: AuIdentifier | None) -> bool:
        """Whether macOS has an Audio Unit registered under an exact identity; never off a Mac."""
        return self.locations.platform == "darwin" and identifier is not None and audio_component_registered(identifier)

    def find(self, device: PluginDevice) -> pathlib.Path | None:
        """Where this computer has the plugin ``device`` names, wherever the set says it was.

        A VST2 by its file name in the plugin folders, and a Windows .dll only
        on Windows; a VST3 by its display name there, then in Live's database,
        the only way to find a shell plugin such as Waves; an Audio Unit by the
        component bundle declaring its identity.
        """
        identity = device.identity
        if device.kind is PluginKind.AU:
            return None if identity.au is None else self.folders.au(identity.au)
        if device.kind is PluginKind.VST3:
            if identity.name is None:
                return None
            found = self.folders.vst3(identity.name)
            if found is not None:
                return found
            listed = (row for row in self.database if row.kind is PluginKind.VST3 and row.name == identity.name)
            return next((row.module_path for row in listed), None)
        file_name = device.reference.file_name
        if file_name is None:
            return None
        if pathlib.PurePath(file_name).suffix.casefold() == ".dll" and self.locations.platform != "win32":
            return None
        return next(iter(self.folders.named(file_name)), None)


@functools.cache
def oracle(locations: Locations) -> LoadableOracle:
    """The one oracle of a run on ``locations``, shared by every set that run reads."""
    return LoadableOracle(locations)
