"""Where Live, plugins and abletoolz's own files live on this computer."""

import logging
import os
import pathlib
import plistlib
import sys
from typing import cast

logger = logging.getLogger(__name__)


def _get_user_config_dir() -> pathlib.Path:
    """Get platform-specific user config directory for abletoolz."""
    if sys.platform == "win32":
        base = pathlib.Path(os.environ.get("APPDATA", pathlib.Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = pathlib.Path.home() / "Library" / "Application Support"
    else:
        xdg_config = os.environ.get("XDG_CONFIG_HOME")
        base = pathlib.Path(xdg_config) if xdg_config else pathlib.Path.home() / ".config"
    return base / "abletoolz"


def default_ableton_user_library() -> pathlib.Path | None:
    """Live's default User Library location for this OS, or None when it can't be found.

    Library code never guesses paths; CLI layers call this and fall back to
    asking the user when it returns None (moved library, Linux, etc.).
    """
    if sys.platform == "win32":
        candidate = pathlib.Path.home() / "Documents" / "Ableton" / "User Library"
    elif sys.platform == "darwin":
        candidate = pathlib.Path.home() / "Music" / "Ableton" / "User Library"
    else:
        return None
    return candidate if candidate.is_dir() else None


def default_live_database_dir() -> pathlib.Path | None:
    """Live's own plugin/browser database location for this OS, when present."""
    if sys.platform == "win32":
        base = pathlib.Path(os.environ.get("LOCALAPPDATA", pathlib.Path.home() / "AppData" / "Local"))
        candidate = base / "Ableton" / "Live Database"
    elif sys.platform == "darwin":
        candidate = pathlib.Path.home() / "Library" / "Application Support" / "Ableton" / "Live Database"
    else:
        return None
    return candidate if candidate.is_dir() else None


def default_au_component_dirs() -> list[pathlib.Path]:
    """Standard Audio Unit component locations on macOS, existing dirs only."""
    if sys.platform != "darwin":
        return []
    candidates = [
        pathlib.Path("/Library/Audio/Plug-Ins/Components"),
        pathlib.Path.home() / "Library" / "Audio" / "Plug-Ins" / "Components",
    ]
    return [candidate for candidate in candidates if candidate.is_dir()]


def default_vst_dirs() -> list[pathlib.Path]:
    """Standard plugin install locations for this OS, existing dirs only."""
    if sys.platform == "win32":
        program_files = pathlib.Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        candidates = [
            program_files / "VstPlugins",
            program_files / "Steinberg" / "VstPlugins",
            program_files / "Common Files" / "VST3",
        ]
    elif sys.platform == "darwin":
        candidates = [
            pathlib.Path("/Library/Audio/Plug-Ins/VST"),
            pathlib.Path("/Library/Audio/Plug-Ins/VST3"),
            pathlib.Path.home() / "Library" / "Audio" / "Plug-Ins" / "VST",
            pathlib.Path.home() / "Library" / "Audio" / "Plug-Ins" / "VST3",
        ]
    else:
        candidates = [
            pathlib.Path.home() / ".vst",
            pathlib.Path.home() / ".vst3",
            pathlib.Path("/usr/lib/vst3"),
            pathlib.Path("/usr/local/lib/vst3"),
        ]
    return [candidate for candidate in candidates if candidate.is_dir()]


def read_plist(plist_path: pathlib.Path) -> dict[str, object]:
    """What one bundle's Info.plist declares, empty when it can't be read.

    A macOS plugin is a directory whose Info.plist is the only place its name,
    version and Audio Unit identity are written down. plistlib reads both the
    XML and the binary form on any OS, so bundles stay readable -- and testable
    -- away from a Mac. One unreadable plist skips its bundle rather than
    killing the scan around it.
    """
    try:
        with plist_path.open("rb") as file:
            plist = plistlib.load(file)
    except (plistlib.InvalidFileException, ValueError, OSError) as error:
        logger.debug("Skipping malformed Info.plist %s: %s", plist_path, error)
        return {}
    return cast("dict[str, object]", plist) if isinstance(plist, dict) else {}


# Databases stored alongside config in user config dir
DEFAULT_DB_PATH = _get_user_config_dir() / "sample_db.json"
DEFAULT_PLUGIN_DB_PATH = _get_user_config_dir() / "plugin_db.json"
