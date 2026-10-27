"""Where Live, plugins and abletoolz's own files live on one computer.

:class:`Locations` is that answer as one value. ``Locations.this_computer()``
reads it off the running OS and its environment -- the only place in abletoolz
that does -- and everything else takes the value it is handed, so a spawned
worker or a test sees exactly the folders it was given and nothing else.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import pathlib
import plistlib
import sys
from collections.abc import Iterable
from typing import cast

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class Locations:
    """Where one computer keeps Live's plugin database, its plugins and abletoolz's own files.

    ``platform`` is the OS as ``sys.platform`` spells it, which decides what
    plugin files that computer can load and whether macOS's component registry
    is asked. ``vst_dirs`` and ``au_dirs`` are the standard plugin folders that
    exist there, ``live_database`` the folder Live keeps its plugin database in
    and ``user_library`` Live's User Library, each None where there is none.
    Everything abletoolz writes for itself sits in ``config_dir``.

    Not :class:`~abletoolz.plugins.plan.Machine`, which is what is installed in
    these places as a plan sees it.
    """

    platform: str
    config_dir: pathlib.Path
    vst_dirs: tuple[pathlib.Path, ...] = ()
    au_dirs: tuple[pathlib.Path, ...] = ()
    live_database: pathlib.Path | None = None
    user_library: pathlib.Path | None = None

    @property
    def config_file(self) -> pathlib.Path:
        """config.yaml, the user's settings."""
        return self.config_dir / "config.yaml"

    @property
    def sample_db(self) -> pathlib.Path:
        """The sample database ``--db`` builds and the fix-samples commands read."""
        return self.config_dir / "sample_db.sqlite3"

    @property
    def plugin_db(self) -> pathlib.Path:
        """The plugin database ``--plugin-db`` builds and a plan reads."""
        return self.config_dir / "plugin_db.json"

    @property
    def vst3_parameters(self) -> pathlib.Path:
        """Where the parameters read off installed VST3 modules are kept between runs."""
        return self.config_dir / "vst3_parameters.json"

    def with_plugin_folders(self, folders: Iterable[pathlib.Path]) -> Locations:
        """These locations with the plugin folders config.yaml adds, each folder listed once."""
        extra = tuple(folder for folder in dict.fromkeys(folders) if folder not in self.vst_dirs)
        return dataclasses.replace(self, vst_dirs=self.vst_dirs + extra) if extra else self

    @classmethod
    def this_computer(cls) -> Locations:
        """The locations of the computer running abletoolz, keeping only the folders that exist."""
        home = pathlib.Path.home()
        platform = sys.platform
        live_database: pathlib.Path | None = None
        user_library: pathlib.Path | None = None
        au_dirs: list[pathlib.Path] = []
        if platform == "win32":
            config = pathlib.Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
            program_files = pathlib.Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            vst_dirs = [
                program_files / "VstPlugins",
                program_files / "Steinberg" / "VstPlugins",
                program_files / "Common Files" / "VST3",
            ]
            local = pathlib.Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
            live_database = local / "Ableton" / "Live Database"
            user_library = home / "Documents" / "Ableton" / "User Library"
        elif platform == "darwin":
            config = home / "Library" / "Application Support"
            plug_ins = (pathlib.Path("/Library/Audio/Plug-Ins"), home / "Library" / "Audio" / "Plug-Ins")
            vst_dirs = [folder / kind for folder in plug_ins for kind in ("VST", "VST3")]
            au_dirs = [folder / "Components" for folder in plug_ins]
            live_database = config / "Ableton" / "Live Database"
            user_library = home / "Music" / "Ableton" / "User Library"
        else:
            xdg_config = os.environ.get("XDG_CONFIG_HOME")
            config = pathlib.Path(xdg_config) if xdg_config else home / ".config"
            vst_dirs = [
                home / ".vst",
                home / ".vst3",
                pathlib.Path("/usr/lib/vst3"),
                pathlib.Path("/usr/local/lib/vst3"),
            ]
        return cls(
            platform=platform,
            config_dir=config / "abletoolz",
            vst_dirs=tuple(folder for folder in vst_dirs if folder.is_dir()),
            au_dirs=tuple(folder for folder in au_dirs if folder.is_dir()),
            live_database=live_database if live_database is not None and live_database.is_dir() else None,
            user_library=user_library if user_library is not None and user_library.is_dir() else None,
        )


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


# The sample database's default file, for abletoolz.samples, which does not take
# a Locations yet. The command line always passes its own.
DEFAULT_DB_PATH = Locations.this_computer().sample_db
