"""The .als document: identity, parse state, version, and domain accessors."""

import functools
import logging
import pathlib
import subprocess
import sys
from xml.etree import ElementTree as ET

from abletoolz import versioning
from abletoolz.live_set import persistence
from abletoolz.live_set.clips import Clips
from abletoolz.live_set.describe import Describe
from abletoolz.live_set.devices import Devices
from abletoolz.live_set.plugins import Plugins
from abletoolz.live_set.samples import Samples
from abletoolz.live_set.tracks import Tracks
from abletoolz.live_set.transport import Transport
from abletoolz.misc import B, C, M, R, SetError, SetOperatingSystem, Y

logger = logging.getLogger(__name__)


def elements_equal(e1: ET.Element, e2: ET.Element) -> bool:
    """Check if two xml.Etree roots are equivalent."""
    if e1.tag != e2.tag:
        return False
    if e1.text != e2.text:
        return False
    if e1.tail != e2.tail:
        return False
    if e1.attrib != e2.attrib:
        return False
    if len(e1) != len(e2):
        return False
    return all(elements_equal(c1, c2) for c1, c2 in zip(e1, e2, strict=False))


class AbletonSet:
    """One .als document. Feature operations live on the domain accessors."""

    def __init__(self, pathlib_obj: pathlib.Path) -> None:
        """Construct class."""
        self.name = pathlib_obj.name
        self.path = pathlib_obj
        self._root: ET.Element | None = None

        self.project_root_folder: pathlib.Path | None = None  # Folder where Ableton Project Info resides.
        self.last_modification_time: float | None = None
        self.creation_time: float | None = None

        self.version: str | None = None  # Official Ableton live version.
        self._version_tuple: versioning.Version | None = None

        # Loaded lazily by Transport; persistence uses them for filename mods.
        self.furthest_bar: int | None = None
        self.bpm: float | None = None

        self.set_os: SetOperatingSystem = SetOperatingSystem.UNSET

    @functools.cached_property
    def transport(self) -> Transport:
        return Transport(self)

    @functools.cached_property
    def tracks(self) -> Tracks:
        return Tracks(self)

    @functools.cached_property
    def clips(self) -> Clips:
        return Clips(self)

    @functools.cached_property
    def devices(self) -> Devices:
        return Devices(self)

    @functools.cached_property
    def samples(self) -> Samples:
        return Samples(self)

    @functools.cached_property
    def plugins(self) -> Plugins:
        return Plugins(self)

    @functools.cached_property
    def describe(self) -> Describe:
        """Callable accessor: ``set.describe(level)`` -- see ``describe.Describe``."""
        return Describe(self)

    def __eq__(self, o: object) -> bool:
        """Compare two sets."""
        if self.root is None:
            return False
        if not isinstance(o, AbletonSet) or isinstance(o, AbletonSet) and o.root is None:
            return False
        return elements_equal(self.root, o.root)

    @property
    def root(self) -> ET.Element:
        """Lazy-load and return XML root; raise if unavailable."""
        if self._root is None:
            self.parse()
        if self._root is not None:
            return self._root
        else:
            raise SetError("Set is not loaded! Failed to parse.")

    @property
    def version_tuple(self) -> versioning.Version:
        """Lazy-load and return version tuple; raise if unavailable."""
        if self._version_tuple is None:
            self.load_version()
        if self._version_tuple is not None:
            return self._version_tuple
        else:
            raise SetError("Set version is not loaded! Failed to parse.")

    def open_folder(self) -> None:
        """Open folder in file explorer/finder.

        Currently unused.
        """
        if sys.platform == "win32":
            subprocess.Popen(f'explorer /select, "{self.path}"')
        elif sys.platform == "darwin":
            subprocess.Popen(f"open {self.path}")

    def load_version(self) -> None:
        """Load version."""
        self.version = self.root.get("Creator")
        if not isinstance(self.version, str):
            raise SetError("Couldn't parse Creator from set.")
        try:
            self._version_tuple = versioning.parse_creator(self.version)
        except ValueError as exc:
            raise SetError(str(exc)) from exc
        logger.info("%sSet version: %s%s", B, M, self.version)
        if "b" in self.version.split()[-1]:
            logger.warning(
                "%sSet is from a beta version, some commands might not work properly!",
                Y,
            )

    def parse(self) -> bool:
        """Uncompresses ableton set and loads into element tree."""
        return persistence.read_set(self)

    def find_project_root_folder(self) -> pathlib.Path | None:
        """Find project root folder for set."""
        if self.project_root_folder:
            return self.project_root_folder

        for current_dir in self.path.parents:
            if pathlib.Path(current_dir / "Ableton Project Info").exists():
                self.project_root_folder = current_dir
                logger.debug("%sProject root folder: %s", C, current_dir)
                return self.project_root_folder
        logger.error(
            "%sCould not find project folder(Ableton Project Info), unable to validate relative paths!",
            R,
        )
        return None

    def generate_xml(self) -> bytes:
        """Add header and footer to xml data."""
        return persistence.to_xml_bytes(self)

    def save_xml(self) -> None:
        """Save set XML."""
        persistence.save_xml(self)

    def get_file_times(self) -> None:
        """Find set creation/modification times."""
        persistence.get_file_times(self)

    def save_set(self, append_bars_bpm: bool = False, prepend_version: bool = False) -> None:
        """Save set to disk (backup first), with optional filename modifications."""
        persistence.save_set(self, append_bars_bpm=append_bars_bpm, prepend_version=prepend_version)
