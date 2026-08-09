"""Plugin reference scanning, analysis, and repair."""

from __future__ import annotations

import ctypes
import dataclasses
import functools
import logging
import pathlib
import plistlib
import sqlite3
import sys
import threading
from typing import TYPE_CHECKING, cast
from xml.etree import ElementTree as ET

from abletoolz import utils
from abletoolz.misc import (
    RST,
    C,
    G,
    R,
    SetOperatingSystem,
    Y,
    default_au_component_dirs,
    default_live_database_dir,
    default_vst_dirs,
    get_element,
)
from abletoolz.plugin_parsers import AbletoolzConfig, PluginAnalysis, PluginData, analyze_plugin, fix_plugin
from abletoolz.plugin_parsers.config import load_config
from abletoolz.plugin_parsers.upgrade_rules import get_upgrade
from abletoolz.sample_databaser import create_db
from abletoolz.versioning import Version

if TYPE_CHECKING:
    from abletoolz.live_set.document import AbletonSet

logger = logging.getLogger(__name__)

_TRACK_TYPES = {"AudioTrack", "MidiTrack", "ReturnTrack", "MasterTrack", "MainTrack", "GroupTrack"}


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

type AuIdentifier = tuple[int, int, int]

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
    try:
        with plist_path.open("rb") as file:
            plist = plistlib.load(file)
    except (plistlib.InvalidFileException, ValueError) as error:
        logger.debug("Skipping malformed Info.plist %s: %s", plist_path, error)
        return set()
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
            identifiers.add(
                (_fourcc_int(component_type), _fourcc_int(component_subtype), _fourcc_int(manufacturer))
            )
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
    try:
        with plist_path.open("rb") as file:
            plist = plistlib.load(file)
    except (plistlib.InvalidFileException, ValueError) as error:
        logger.debug("Skipping malformed Info.plist %s: %s", plist_path, error)
        return set()
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

    kind: str  # "vst", "vst3" or "au"
    name: str | None
    path: pathlib.Path | None
    exists: bool
    alternative: pathlib.Path | None
    track_location: str
    manufacturer: str | None = None


class Plugins:
    """Plugin references of one set."""

    def __init__(self, live_set: AbletonSet) -> None:
        self._set = live_set
        self.found_vst_dirs: list[pathlib.Path] = []
        self._parent_map: dict[ET.Element, ET.Element] | None = None
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

    def _parse_hex_path(self, text: str) -> str | None:
        """Take raw hex string from XML entry and parses."""
        if not text:
            return None
        # Strip new lines and tabs from raw text to have one long hex string.
        abs_hash_path = text.replace("\t", "").replace("\n", "")
        byte_data = bytearray.fromhex(abs_hash_path)
        if byte_data[0:3] == b"\x00" * 3:  # Header only on mac projects.
            self._set.set_os = SetOperatingSystem.MAC_OS
            return utils.parse_mac_data(byte_data, abs_hash_path)
        else:
            self._set.set_os = SetOperatingSystem.WINDOWS_OS
            return utils.parse_windows_data(byte_data, abs_hash_path)

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

    def parse_au_element(self, au_element: ET.Element) -> tuple[str | None, str | None, AuIdentifier | None]:
        """Pull display metadata and exact component identity from an AuPluginInfo element."""
        name_element = au_element.find("Name")
        manufacturer_element = au_element.find("Manufacturer")
        type_element = au_element.find("ComponentType")
        subtype_element = au_element.find("ComponentSubType")
        component_manufacturer_element = au_element.find("ComponentManufacturer")
        type_value = type_element.get("Value") if type_element is not None else None
        subtype_value = subtype_element.get("Value") if subtype_element is not None else None
        component_manufacturer_value = (
            component_manufacturer_element.get("Value") if component_manufacturer_element is not None else None
        )
        identity = None
        if type_value is not None and subtype_value is not None and component_manufacturer_value is not None:
            identity = (int(type_value), int(subtype_value), int(component_manufacturer_value))
        return (
            name_element.get("Value") if name_element is not None else None,
            manufacturer_element.get("Value") if manufacturer_element is not None else None,
            identity,
        )

    def parse_vst3_element(self, vst3_element: ET.Element) -> tuple[str | None, pathlib.Path | None]:
        """Pull display name and stored path out of a Vst3PluginInfo element.

        Every version stores a Name; only newer sets also store a Path.
        """
        name_ele = vst3_element.find("Name")
        name = name_ele.get("Value") if name_ele is not None else None
        path_ele = vst3_element.find("Path")
        path_value = path_ele.get("Value") if path_ele is not None else None
        return name, pathlib.Path(path_value) if path_value else None

    def parse_vst_element(self, vst_element: ET.Element) -> tuple[pathlib.Path | None, str | None, pathlib.Path | None]:
        """Parse out VST element from vst xtree."""
        for plugin_path in ["Dir", "Path"]:
            path_results = vst_element.findall(f".//{plugin_path}")
            if len(path_results):
                if plugin_path == "Path":
                    if (full_path := path_results[0].get("Value")) is None:
                        logger.error("Couldn't get Path for %s", path_results[0])
                        continue
                    if "/" not in full_path and "\\" not in full_path:
                        if search_result := self.search(full_path):
                            return None, search_result.name, search_result
                        return None, full_path, None
                    path_separator = utils.path_separator_type(full_path)
                    name = full_path.split(path_separator)[-1]
                    return pathlib.Path(full_path), name, None
                elif plugin_path == "Dir":
                    if (dir_bin := path_results[0].find("Data")) is None:
                        logger.error("Couldn't get Path for %s", path_results[0])
                        continue
                    if (text := dir_bin.text) is None:
                        continue
                    path = self._parse_hex_path(text)
                    name_ele = vst_element.find("FileName")
                    name = name_ele.get("Value", "") if name_ele is not None else "<>"
                    if not path:
                        logger.error("%sCouldn't parse absolute path for %s", Y, name)
                        return None, name, None
                    path_separator = utils.path_separator_type(path)
                    if path[-1] == path_separator:
                        full_path = f"{path}{name}"
                    else:
                        full_path = f"{path}{path_separator}{name}"
                    return pathlib.Path(full_path), name, None

        logger.error("%sCouldn't parse plugin!", R)
        return None, None, None

    def _find_track_for_element(self, element: ET.Element) -> str:
        """Find which track contains a given element by searching parent map."""
        if self._parent_map is None:
            self._parent_map = {c: p for p in self._root.iter() for c in p}
        current: ET.Element | None = element
        while current is not None:
            if current.tag in _TRACK_TYPES:
                name_elem = current.find(".//EffectiveName")
                if name_elem is not None:
                    return f"{current.tag}: {name_elem.get('Value', '?')}"
                return current.tag
            current = self._parent_map.get(current)
        return "?"

    def scan(self, vst_dirs: list[pathlib.Path]) -> list[PluginRef]:
        """Resolve every plugin reference in the set against disk."""
        self.found_vst_dirs.extend(vst_dirs)
        refs: list[PluginRef] = []
        for plugin_element in self._root.iter("PluginDesc"):
            track_loc = self._find_track_for_element(plugin_element)
            for vst_element in plugin_element.iter("VstPluginInfo"):
                full_path, name, potential = self.parse_vst_element(vst_element)
                exists = bool(full_path and full_path.exists())
                if exists and full_path is not None and full_path.parent not in self.found_vst_dirs:
                    self.found_vst_dirs.append(full_path.parent)
                elif not exists and isinstance(name, str):
                    # Did not find plugin in saved path, try to search
                    potential = self.search(name)
                refs.append(
                    PluginRef(
                        kind="vst",
                        name=name,
                        path=full_path,
                        exists=exists,
                        alternative=potential,
                        track_location=track_loc,
                    )
                )
            for vst3_element in plugin_element.iter("Vst3PluginInfo"):
                name, stored_path = self.parse_vst3_element(vst3_element)
                exists = bool(stored_path and stored_path.exists())
                if exists and stored_path is not None and stored_path.parent not in self.found_vst_dirs:
                    self.found_vst_dirs.append(stored_path.parent)
                resolved = None if exists or name is None else self.search_vst3(name)
                if stored_path is None:
                    # Most sets store only the display name; a search hit IS the path.
                    ref = PluginRef(
                        kind="vst3",
                        name=name,
                        path=resolved,
                        exists=resolved is not None,
                        alternative=None,
                        track_location=track_loc,
                    )
                else:
                    ref = PluginRef(
                        kind="vst3",
                        name=name,
                        path=stored_path,
                        exists=exists,
                        alternative=resolved,
                        track_location=track_loc,
                    )
                refs.append(ref)
            for au_element in plugin_element.iter("AuPluginInfo"):
                name, manufacturer, identifier = self.parse_au_element(au_element)
                registered = identifier is not None and audio_component_registered(identifier)
                component_path = self.search_au(identifier) if identifier is not None else None
                refs.append(
                    PluginRef(
                        kind="au",
                        name=name,
                        path=component_path if registered else None,
                        exists=registered,
                        alternative=component_path if component_path is not None and not registered else None,
                        track_location=track_loc,
                        manufacturer=manufacturer,
                    )
                )
        return refs

    def analyze(self, config: AbletoolzConfig | None = None) -> list[PluginAnalysis]:
        """Analyze all plugins in set using registered parsers."""
        results: list[PluginAnalysis] = []
        for plugin_element in self._root.iter("PluginDesc"):
            for vst_element in plugin_element.iter("VstPluginInfo"):
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
        for plugin_element in self._root.iter("PluginDesc"):
            for vst_element in plugin_element.iter("VstPluginInfo"):
                plugin = PluginData.from_element(vst_element)
                dump = plugin.dump_buffer(max_hex_bytes=max_hex, max_decoded_chars=max_decoded)
                dumps.append(dump)
                # Print each dump immediately for CLI usage
                print(dump)
                print("-" * 80)
        return dumps

    def fix(self, db: create_db.DatabaseT, config: AbletoolzConfig | None = None) -> bool:
        """Scan supported plugins and apply in-place fixes using sample DB."""
        changed = False
        for plugin_element in self._root.iter("PluginDesc"):
            for vst_element in plugin_element.iter("VstPluginInfo"):
                plugin = PluginData.from_element(vst_element)
                if fix_plugin(plugin, db, config):
                    logger.info("%sFixed plugin: %s", G, plugin.plugin_name)
                    changed = True
        return changed

    def upgrade(self) -> bool:
        """Upgrade plugin references using rules from config."""
        rules = load_config().plugin_upgrade_rules
        if not rules:
            logger.info("%sNo upgrade rules in config", C)
            return False

        changed = False
        for plugin_element in self._root.iter("PluginDesc"):
            path_el = get_element(plugin_element, "VstPluginInfo.Path", silent_error=True)
            if not isinstance(path_el, ET.Element):
                continue

            current_path = path_el.get("Value", "")
            if not current_path:
                continue

            current_filename = pathlib.Path(current_path).name
            result = get_upgrade(current_filename, rules)

            if result:
                target_name, target_path = result
                new_path = str(target_path).replace("\\", "/")
                path_el.set("Value", new_path)

                plugname_el = get_element(plugin_element, "VstPluginInfo.PlugName", silent_error=True)
                if isinstance(plugname_el, ET.Element):
                    plugname_el.set("Value", target_path.stem)

                logger.info("%sUpgraded: %s%s%s â†’ %s%s", G, Y, current_filename, RST, G, target_name)
                changed = True

        return changed
