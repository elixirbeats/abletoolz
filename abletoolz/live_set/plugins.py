"""Plugin reference scanning, analysis, and repair."""

from __future__ import annotations

import ctypes
import dataclasses
import functools
import logging
import pathlib
import sqlite3
import sys
import threading
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast
from xml.etree import ElementTree as ET

from abletoolz.ansi import RST, C, G, Y
from abletoolz.formats.path_data import SetOperatingSystem
from abletoolz.live_set.tracks import display_name
from abletoolz.machine import default_au_component_dirs, default_live_database_dir, default_vst_dirs, read_plist
from abletoolz.plugins import (
    AbletoolzConfig,
    PluginAnalysis,
    PluginData,
    PluginKind,
    analyze_plugin,
    fix_plugin,
)
from abletoolz.plugins.config import load_config
from abletoolz.plugins.container.device import AuIdentifier, PluginDevices, ReferenceForm
from abletoolz.plugins.container.format_translation import ConfiguredConversionRule
from abletoolz.plugins.container.upgrade_rules import get_upgrade
from abletoolz.plugins.identity.plugin_db import load_plugin_db
from abletoolz.plugins.identity.uid_sources import UidLookup
from abletoolz.plugins.migration import MigrationResult, Vst2Migration, apply_vst2_migration
from abletoolz.plugins.repair import RepairReport, default_oracle, repair_set
from abletoolz.samples.database import DatabaseT
from abletoolz.schema import Attr, Tag
from abletoolz.schema.versioning import Version

if TYPE_CHECKING:
    from abletoolz.live_set.document import AbletonSet

logger = logging.getLogger(__name__)


# -- VST3 resolution --------------------------------------------------------
# Sets store VST3 devices by display name (older sets carry no path at all).
# On Windows a .vst3 is a single file matched by stem; on macOS it is a bundle
# dir whose name often differs, so matching goes through Contents/Info.plist
# (plistlib reads XML and binary plists on any OS - testable away from a Mac).
# Live's own plugin index (sqlite under the Live Database dir) is the second
# source - the only way to resolve shell plugins (e.g. Waves).

# Verified against Live 12's Live-plugins-*.db: plugins(name, module_id,
# dev_identifier, ...) joins plugin_modules(module_id, path, ...), with
# dev_identifier prefixed "device:vst3:" for VST3 entries.
_PLUGIN_QUERY = (
    "SELECT pm.path FROM plugins p"
    " LEFT JOIN plugin_modules pm ON p.module_id = pm.module_id"
    " WHERE p.name = ? AND p.dev_identifier LIKE 'device:vst3:%' LIMIT 1"
)
_VST3_INDEX_CACHE: dict[tuple[pathlib.Path, ...], dict[str, pathlib.Path]] = {}
_VST3_INDEX_LOCK = threading.Lock()

_AU_INDEX_CACHE: dict[tuple[pathlib.Path, ...], dict[AuIdentifier, pathlib.Path]] = {}
_AU_INDEX_LOCK = threading.Lock()
_AUDIO_TOOLBOX_PATH = "/System/Library/Frameworks/AudioToolbox.framework/AudioToolbox"


class _AudioComponentDescription(ctypes.Structure):
    _fields_ = [
        ("component_type", ctypes.c_uint32),
        ("component_subtype", ctypes.c_uint32),
        ("component_manufacturer", ctypes.c_uint32),
        ("component_flags", ctypes.c_uint32),
        ("component_flags_mask", ctypes.c_uint32),
    ]


def _fourcc_int(value: str) -> int:
    return int.from_bytes(value.encode("latin-1"), "big")


def plist_au_identifiers(plist_path: pathlib.Path) -> set[AuIdentifier]:
    """Exact component identities declared by one Audio Unit bundle."""
    plist = read_plist(plist_path)
    identifiers: set[AuIdentifier] = set()
    components = plist.get("AudioComponents")
    if not isinstance(components, list):
        return identifiers
    for component in cast(list[object], components):
        if not isinstance(component, dict):
            continue
        component_data = cast(dict[str, object], component)
        values = [component_data.get(key) for key in ("type", "subtype", "manufacturer")]
        if all(isinstance(value, str) and len(value) == 4 for value in values):
            component_type, component_subtype, manufacturer = values
            assert isinstance(component_type, str)
            assert isinstance(component_subtype, str)
            assert isinstance(manufacturer, str)
            identifiers.add((_fourcc_int(component_type), _fourcc_int(component_subtype), _fourcc_int(manufacturer)))
    return identifiers


def index_au_components(search_dirs: list[pathlib.Path]) -> dict[AuIdentifier, pathlib.Path]:
    """Index installed component bundles by exact Audio Unit identity."""
    index: dict[AuIdentifier, pathlib.Path] = {}
    for base in search_dirs:
        for bundle in base.rglob("*.component"):
            plist_path = bundle / "Contents" / "Info.plist"
            if plist_path.is_file():
                for identifier in plist_au_identifiers(plist_path):
                    index.setdefault(identifier, bundle)
    return index


def cached_au_index(search_dirs: tuple[pathlib.Path, ...]) -> dict[AuIdentifier, pathlib.Path]:
    """Build one component index per directory set and share it across batch workers."""
    with _AU_INDEX_LOCK:
        index = _AU_INDEX_CACHE.get(search_dirs)
        if index is None:
            index = index_au_components(list(search_dirs))
            _AU_INDEX_CACHE[search_dirs] = index
        return index


@functools.cache
def audio_component_registered(identifier: AuIdentifier) -> bool:
    """Ask macOS whether an exact Audio Unit identity is registered."""
    if sys.platform != "darwin":
        return False
    audio_toolbox = ctypes.CDLL(_AUDIO_TOOLBOX_PATH)
    find_next = audio_toolbox.AudioComponentFindNext
    find_next.argtypes = [ctypes.c_void_p, ctypes.POINTER(_AudioComponentDescription)]
    find_next.restype = ctypes.c_void_p
    description = _AudioComponentDescription(*identifier, 0, 0)
    return find_next(None, ctypes.byref(description)) is not None


def plist_declared_names(plist_path: pathlib.Path) -> set[str]:
    """Names a bundle's Info.plist declares. Empty for a malformed plist."""
    plist = read_plist(plist_path)
    names: set[str] = set()
    for key in ("CFBundleDisplayName", "CFBundleName"):
        value = plist.get(key)
        if isinstance(value, str):
            names.add(value)
    components = plist.get("AudioComponents")
    if isinstance(components, list):
        for component in cast(list[object], components):
            if isinstance(component, dict):
                name = cast(dict[str, object], component).get("name")
                if isinstance(name, str):
                    names.add(name)
    return names


def bundle_matches(bundle: pathlib.Path, display_name: str) -> bool:
    """True when a .vst3 bundle dir answers to display_name by name or Info.plist."""
    if bundle.stem == display_name:
        return True
    plist_path = bundle / "Contents" / "Info.plist"
    return plist_path.is_file() and display_name in plist_declared_names(plist_path)


def resolve_vst3_name(display_name: str, search_dirs: list[pathlib.Path]) -> pathlib.Path | None:
    """Find a VST3 by display name: single-file .vst3 (Windows) or bundle dir (macOS)."""
    for base in search_dirs:
        for candidate in base.rglob("*.vst3"):
            if candidate.is_dir():
                if bundle_matches(candidate, display_name):
                    return candidate
            elif candidate.stem == display_name:
                return candidate
    return None


def index_vst3_names(search_dirs: list[pathlib.Path]) -> dict[str, pathlib.Path]:
    """Index every name advertised by installed VST3 files and bundles."""
    index: dict[str, pathlib.Path] = {}
    for base in search_dirs:
        for candidate in base.rglob("*.vst3"):
            index.setdefault(candidate.stem, candidate)
            if candidate.is_dir():
                plist_path = candidate / "Contents" / "Info.plist"
                if plist_path.is_file():
                    for name in plist_declared_names(plist_path):
                        index.setdefault(name, candidate)
    return index


def cached_vst3_index(search_dirs: tuple[pathlib.Path, ...]) -> dict[str, pathlib.Path]:
    """Build one VST3 index per directory set and share it across batch workers."""
    with _VST3_INDEX_LOCK:
        index = _VST3_INDEX_CACHE.get(search_dirs)
        if index is None:
            index = index_vst3_names(list(search_dirs))
            _VST3_INDEX_CACHE[search_dirs] = index
        return index


def live_database_lookup(display_name: str, database: pathlib.Path) -> pathlib.Path | None:
    """Ask one Live database file where a VST3 lives, read-only.

    Database files without these tables (e.g. Live-files-*.db) or from Live
    versions with another schema simply answer nothing.
    """
    try:
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    except sqlite3.OperationalError as error:
        logger.debug("Cannot open Live Database %s: %s", database, error)
        return None
    try:
        row = connection.execute(_PLUGIN_QUERY, (display_name,)).fetchone()
    except sqlite3.OperationalError as error:
        logger.debug("Live Database %s has no usable plugin tables: %s", database, error)
        return None
    finally:
        connection.close()
    if row is None or row[0] is None:
        return None
    return pathlib.Path(row[0])


def search_live_databases(display_name: str, database_dir: pathlib.Path) -> pathlib.Path | None:
    """Check every database file newest-first; Live keeps one per installed version."""
    databases = sorted(database_dir.rglob("*.db"), key=lambda db: db.stat().st_mtime, reverse=True)
    for database in databases:
        if (path := live_database_lookup(display_name, database)) is not None:
            return path
    return None


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


@dataclasses.dataclass(frozen=True)
class DeviceUpgrade:
    """One device :meth:`Plugins.upgrade` pointed at a different file."""

    track: str
    source: str
    target: str


@dataclasses.dataclass(frozen=True)
class DeviceStateFix:
    """One device a registered parser repaired inside its own saved state."""

    track: str
    name: str


class Plugins:
    """Plugin references of one set."""

    def __init__(self, live_set: AbletonSet) -> None:
        self._set = live_set
        self.found_vst_dirs: list[pathlib.Path] = []
        self._document: PluginDevices | None = None
        self._vst3_index: dict[str, pathlib.Path] | None = None
        self._vst3_index_dirs: tuple[pathlib.Path, ...] = ()
        self._vst3_search_cache: dict[str, pathlib.Path | None] = {}
        self._au_index: dict[AuIdentifier, pathlib.Path] | None = None
        self._au_index_dirs: tuple[pathlib.Path, ...] = ()

    @property
    def version(self) -> Version:
        return self._set.version_tuple

    @property
    def _root(self) -> ET.Element:
        return self._set.root

    def search(self, plugin_name: str) -> pathlib.Path | None:
        """Search this OS's standard plugin dirs and previously seen plugin dirs."""
        if pathlib.Path(plugin_name).suffix.casefold() == ".dll" and sys.platform != "win32":
            return None
        for base in default_vst_dirs():
            for candidate in list(base.rglob("*.dll")) + list(base.rglob("*.vst3")):
                if plugin_name == candidate.name:
                    return candidate
        for directory in self.found_vst_dirs:
            for dll in directory.rglob("*.dll"):
                if plugin_name == dll.name or plugin_name == dll.name.replace(".32", "").replace(".64", ""):
                    return dll
        return None

    def search_vst3(self, display_name: str) -> pathlib.Path | None:
        """Resolve a VST3 display name: plugin dirs first, then Live's own database."""
        search_dirs = tuple(dict.fromkeys(default_vst_dirs() + self.found_vst_dirs))
        if self._vst3_index is None or search_dirs != self._vst3_index_dirs:
            self._vst3_index = cached_vst3_index(search_dirs)
            self._vst3_index_dirs = search_dirs
            self._vst3_search_cache.clear()
        if display_name in self._vst3_search_cache:
            return self._vst3_search_cache[display_name]
        found = self._vst3_index.get(display_name)
        if found is not None:
            self._vst3_search_cache[display_name] = found
            return found
        database_dir = default_live_database_dir()
        if database_dir is None:
            self._vst3_search_cache[display_name] = None
            return None
        found = search_live_databases(display_name, database_dir)
        self._vst3_search_cache[display_name] = found
        return found

    def search_au(self, identifier: AuIdentifier) -> pathlib.Path | None:
        """Find the component bundle that declares an exact Audio Unit identity."""
        search_dirs = tuple(default_au_component_dirs())
        if self._au_index is None or search_dirs != self._au_index_dirs:
            self._au_index = cached_au_index(search_dirs)
            self._au_index_dirs = search_dirs
        return self._au_index.get(identifier)

    def au_loadable(self, identifier: AuIdentifier | None) -> bool:
        """Whether this Mac has an Audio Unit registered under an exact identity."""
        return identifier is not None and audio_component_registered(identifier)

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
        if track is None:
            return "?"
        return display_name(track, self.version) or track.tag

    def _find_track_for_element(self, element: ET.Element) -> str:
        """Find which track contains a given element by searching parent map."""
        track = self.containing_track(element)
        if track is None:
            return "?"
        name_elem = track.find(".//" + Tag.EFFECTIVE_NAME)
        if name_elem is not None:
            return f"{track.tag}: {name_elem.get(Attr.VALUE, '?')}"
        return track.tag

    def scan(self, vst_dirs: list[pathlib.Path]) -> list[PluginRef]:
        """Resolve every plugin reference in the set against disk."""
        self.found_vst_dirs.extend(vst_dirs)
        refs: list[PluginRef] = []
        for device in self._devices():
            track_loc = self._find_track_for_element(device.info)
            identity, reference = device.identity, device.reference
            if device.kind is PluginKind.VST:
                if reference.form is ReferenceForm.DIR and reference.os is not SetOperatingSystem.UNSET:
                    self._set.set_os = reference.os
                full_path, name = reference.path, reference.file_name
                exists = bool(full_path and full_path.exists())
                if exists and full_path is not None and full_path.parent not in self.found_vst_dirs:
                    self.found_vst_dirs.append(full_path.parent)
                # Not at its saved path, so look for the file by name.
                potential = self.search(name) if not exists and name is not None else None
                refs.append(
                    PluginRef(
                        kind=PluginKind.VST,
                        name=name,
                        path=full_path,
                        exists=exists,
                        alternative=potential,
                        track_location=track_loc,
                    )
                )
            elif device.kind is PluginKind.VST3:
                name, stored_path = identity.name, reference.path
                exists = bool(stored_path and stored_path.exists())
                if exists and stored_path is not None and stored_path.parent not in self.found_vst_dirs:
                    self.found_vst_dirs.append(stored_path.parent)
                resolved = None if exists or name is None else self.search_vst3(name)
                if stored_path is None:
                    # Most sets store only the display name; a search hit IS the path.
                    ref = PluginRef(
                        kind=PluginKind.VST3,
                        name=name,
                        path=resolved,
                        exists=resolved is not None,
                        alternative=None,
                        track_location=track_loc,
                    )
                else:
                    ref = PluginRef(
                        kind=PluginKind.VST3,
                        name=name,
                        path=stored_path,
                        exists=exists,
                        alternative=resolved,
                        track_location=track_loc,
                    )
                refs.append(ref)
            else:
                identifier = identity.au
                registered = self.au_loadable(identifier)
                component_path = self.search_au(identifier) if identifier is not None else None
                refs.append(
                    PluginRef(
                        kind=PluginKind.AU,
                        name=identity.name,
                        path=component_path if registered else None,
                        exists=registered,
                        alternative=component_path if component_path is not None and not registered else None,
                        track_location=track_loc,
                        manufacturer=identity.manufacturer,
                    )
                )
        return refs

    def analyze(self, config: AbletoolzConfig | None = None) -> list[PluginAnalysis]:
        """Analyze all plugins in set using registered parsers."""
        results: list[PluginAnalysis] = []
        for plugin_element in self._root.iter(Tag.PLUGIN_DESC):
            for vst_element in plugin_element.iter(Tag.VST_PLUGIN_INFO):
                plugin = PluginData.from_element(vst_element)
                analysis = analyze_plugin(plugin, config)
                if analysis:
                    results.append(analysis)
                    if analysis.issues:
                        for issue in analysis.issues:
                            logger.warning("%s%s: %s", Y, analysis.plugin_name, issue)
                    else:
                        logger.debug("%s%s: OK", G, analysis.plugin_name)
        return results

    def dump(self, max_hex: int = 256, max_decoded: int = 500) -> list[str]:
        """Dump all plugin buffer data for reverse engineering new parsers."""
        dumps: list[str] = []
        for plugin_element in self._root.iter(Tag.PLUGIN_DESC):
            for vst_element in plugin_element.iter(Tag.VST_PLUGIN_INFO):
                plugin = PluginData.from_element(vst_element)
                dump = plugin.dump_buffer(max_hex_bytes=max_hex, max_decoded_chars=max_decoded)
                dumps.append(dump)
                # Print each dump immediately for CLI usage
                print(dump)
                print("-" * 80)
        return dumps

    def fix(self, db: DatabaseT, config: AbletoolzConfig | None = None) -> list[DeviceStateFix]:
        """Scan supported plugins and apply in-place fixes using sample DB.

        Answers the devices it repaired, so a caller can record what happened
        rather than only that something did.
        """
        fixed: list[DeviceStateFix] = []
        for plugin_element in self._root.iter(Tag.PLUGIN_DESC):
            for vst_element in plugin_element.iter(Tag.VST_PLUGIN_INFO):
                plugin = PluginData.from_element(vst_element)
                if fix_plugin(plugin, db, config):
                    logger.info("%sFixed plugin: %s", G, plugin.plugin_name)
                    fixed.append(DeviceStateFix(self.track_name(vst_element), plugin.plugin_name))
        return fixed

    def migrate_vst2(self, device: int, migration: Vst2Migration) -> MigrationResult:
        """Apply a prepared native chunk and installed identity; caller saves.

        Configured plugin parameters require a separate translation. This
        operation refuses them rather than retaining obsolete parameter IDs.
        """
        result = apply_vst2_migration(self._root, device, migration)
        self._document = None
        return result

    def upgrade(self) -> list[DeviceUpgrade]:
        """Upgrade plugin references using rules from config.

        Answers the devices it moved, source and target, the same way
        :meth:`fix` answers the ones it repaired.
        """
        rules = load_config().plugin_upgrade_rules
        if not rules:
            logger.info("%sNo upgrade rules in config", C)
            return []

        upgraded: list[DeviceUpgrade] = []
        for device in self._devices():
            if device.kind is not PluginKind.VST:
                continue
            current_filename = device.reference.file_name
            if not current_filename:
                continue
            result = get_upgrade(current_filename, rules)
            if result:
                target_name, target_path = result
                device.set_reference(target_path)
                device.set_name(target_path.stem)
                logger.info("%sUpgraded: %s%s%s -> %s%s", G, Y, current_filename, RST, G, target_name)
                upgraded.append(DeviceUpgrade(self.track_name(device.info), current_filename, target_name))

        return upgraded

    def _configured_targets(
        self,
        config: AbletoolzConfig,
        targets: Mapping[str, ConfiguredConversionRule] | None,
    ) -> dict[str, ConfiguredConversionRule]:
        """Config's mapping entries with the caller's on top."""
        table = dict(config.plugin_translation_targets)
        table.update(targets or {})
        return table

    def _uid_lookup(self, config: AbletoolzConfig) -> UidLookup:
        """Where a target that names a plugin without its class id gets one.

        Out of the local plugin database, which is where every class id this
        machine knows already lives.
        """
        return load_plugin_db(config).uid_lookup()

    def repair(self, *, targets: Mapping[str, ConfiguredConversionRule] | None = None) -> RepairReport:
        """Replace only the devices this machine cannot load, and only mapped ones.

        A device Live can still load is left alone, and a device with no mapping
        entry is reported rather than guessed at. Caller saves.
        """
        config = load_config()
        table = self._configured_targets(config, targets)
        return repair_set(
            self._set,
            targets=table,
            uid_lookup=self._uid_lookup(config),
            loadable=default_oracle(self._set),
        )
