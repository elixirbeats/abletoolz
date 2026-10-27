"""The .als document: identity, parse state, version, and domain accessors."""

import functools
import logging
import pathlib
from typing import Self
from xml.etree import ElementTree as ET

from abletoolz.ansi import B, C, M, R, Y
from abletoolz.errors import SetError
from abletoolz.live_set import persistence
from abletoolz.live_set.clips import Clips
from abletoolz.live_set.describe import Describe
from abletoolz.live_set.devices import Devices
from abletoolz.live_set.files import SetFiles
from abletoolz.live_set.plugin_files import PluginFiles
from abletoolz.live_set.plugins import Plugins
from abletoolz.live_set.samples import Samples
from abletoolz.live_set.save_as import SaveAsReport, SaveTarget, write_for
from abletoolz.live_set.tracks import Tracks
from abletoolz.live_set.transport import Transport
from abletoolz.schema import Attr, versioning

logger = logging.getLogger(__name__)


def elements_equal(e1: ET.Element, e2: ET.Element) -> bool:
    """Check if two xml.Etree roots are equivalent."""
    if (e1.tag, e1.text, e1.tail, e1.attrib, len(e1)) != (e2.tag, e2.text, e2.tail, e2.attrib, len(e2)):
        return False
    return all(elements_equal(c1, c2) for c1, c2 in zip(e1, e2, strict=False))


class AbletonSet:
    """One .als document. Feature operations live on the domain accessors."""

    def __init__(self, pathlib_obj: pathlib.Path, project_root: pathlib.Path | None = None) -> None:
        """A set at ``pathlib_obj``; ``project_root`` names its project folder instead of searching for one."""
        self.name = pathlib_obj.name
        self.path = pathlib_obj
        self._root: ET.Element | None = None

        self._given_project_root = project_root
        self.last_modification_time: float | None = None
        self.creation_time: float | None = None

        self._version: str | None = None
        self._version_tuple: versioning.Version | None = None

        # Loaded lazily by Transport; persistence uses them for filename mods.
        self.furthest_bar: int | None = None
        self.bpm: float | None = None

    @classmethod
    def from_root(cls, root: ET.Element, path: pathlib.Path, *, project_root: pathlib.Path | None = None) -> Self:
        """A set whose document is ``root``, already in memory, as if it had been read from ``path``."""
        live_set = cls(path, project_root)
        live_set._root = root
        return live_set

    @functools.cached_property
    def project_root(self) -> pathlib.Path | None:
        """The project folder: the nearest folder above the set that holds ``Ableton Project Info``.

        Searched for once, the first time anything asks, unless the set was
        given one. None when the set is in no project, and then nothing a
        project-relative path names can be found.
        """
        if self._given_project_root is not None:
            return self._given_project_root
        for folder in self.path.parents:
            if (folder / "Ableton Project Info").exists():
                logger.debug("%sProject root folder: %s", C, folder)
                return folder
        logger.error(
            "%sCould not find project folder(Ableton Project Info), unable to validate relative paths!",
            R,
        )
        return None

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
    def plugin_files(self) -> PluginFiles:
        return PluginFiles(self)

    @functools.cached_property
    def files(self) -> SetFiles:
        """Sample references and plugin files together, the way the sample flags treat them."""
        return SetFiles(self)

    @functools.cached_property
    def describe(self) -> Describe:
        """Callable accessor: ``set.describe(level)`` -- see ``describe.Describe``."""
        return Describe(self)

    def __eq__(self, o: object) -> bool:
        """Compare two sets."""
        if self.root is None:
            return False
        if not isinstance(o, AbletonSet) or o.root is None:
            return False
        return elements_equal(self.root, o.root)

    @property
    def root(self) -> ET.Element:
        """Lazy-load and return XML root; raise if unavailable."""
        if self._root is None:
            self.parse()
        if self._root is None:
            raise SetError("Set is not loaded! Failed to parse.")
        return self._root

    @property
    def version(self) -> str:
        """The Live release that saved the set, as Live writes it: "Ableton Live 11.3.42"."""
        if self._version is None:
            self.load_version()
        if self._version is None:
            raise SetError("Set version is not loaded! Failed to parse.")
        return self._version

    @property
    def version_tuple(self) -> versioning.Version:
        """Lazy-load and return version tuple; raise if unavailable."""
        if self._version_tuple is None:
            self.load_version()
        if self._version_tuple is None:
            raise SetError("Set version is not loaded! Failed to parse.")
        return self._version_tuple

    def load_version(self) -> None:
        """Load version."""
        creator = self.root.get(Attr.CREATOR)
        if not isinstance(creator, str):
            raise SetError("Couldn't parse Creator from set.")
        try:
            self._version_tuple = versioning.parse_creator(creator)
        except ValueError as exc:
            raise SetError(str(exc)) from exc
        self._version = creator
        logger.info("%sSet version: %s%s", B, M, creator)
        if "b" in creator.split()[-1]:
            logger.warning(
                "%sSet is from a beta version, some commands might not work properly!",
                Y,
            )

    def parse(self) -> bool:
        """Uncompresses ableton set and loads into element tree."""
        return persistence.read_set(self)

    def generate_xml(self) -> bytes:
        """Add header and footer to xml data."""
        return persistence.to_xml_bytes(self)

    def save_xml(self) -> None:
        """Save set XML."""
        persistence.save_xml(self)

    def get_file_times(self) -> None:
        """Find set creation/modification times."""
        persistence.get_file_times(self)

    def save_as(self, target: SaveTarget, output_dir: pathlib.Path | None = None) -> tuple[pathlib.Path, SaveAsReport]:
        """Write a copy for an older Live release; see :func:`abletoolz.live_set.save_as.write_for`."""
        return write_for(self, target, output_dir)

    def save_set(self, output_dir: pathlib.Path | None = None) -> None:
        """Save set to disk: in place (backup first) by default; into ``output_dir``
        (original untouched, no backup) when given."""
        persistence.save_set(self, output_dir=output_dir)
