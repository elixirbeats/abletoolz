"""Everything known about one plugin, and how its patch crosses between its own formats.

A plugin is what one set of stored identities names: a VST2 UniqueId, a VST3
class id, an AU type/subtype/manufacturer. A new major version ships new ids
(Pro-Q 1 to 4 store ``FPQr``, ``FQ2p``, ``FQ3p``, ``FQ4p``), so it is a different
plugin with its own map; builds that keep the ids are one plugin, and the ways
their patches differ are :class:`Layout` entries on it.

Each plugin format the plugin ships in is a :class:`PluginFormat`: its id, the
name a device of that format stores, the :class:`Framing` that wraps the
plugin's own patch for that format, what goes in a VST3 device's controller
half, and the evidence for that format. Crossing between two of the plugin's
formats is always the same two steps -- lift out of the source format into the
plugin's own patch, lower into the target format -- so nothing here is written
per format pair, and the only thing per plugin is the data in its map.

Converting one plugin into another (Pro-Q 1 into Pro-Q 3) is not this module's
job: that needs a correspondence somebody worked out, keyed by both plugins.

A map also keeps, for the few plugins whose patch names files on disk, how to
find and relink those files (:class:`FilePaths`).
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from abletoolz.plugins.container.controller import ControllerState
from abletoolz.plugins.identity.ids import AuId, AuRegistration, PluginKind, StoredId, Vst2Id, Vst3Id
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.measured import MeasuredState


def _text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise StateTransformError(f"{label} must be explicit")


# -- one plugin format ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FormatState:
    """What a device of one plugin format holds for its patch.

    ``controller`` is the second half a VST3 preset carries, and None for the
    other formats.
    """

    state: bytes
    controller: bytes | None = None


class Framing(Protocol):
    """How one plugin format wraps the plugin's own patch.

    ``lift`` takes what a device of this format saved to the plugin's own patch,
    and ``lower`` writes the plugin's own patch as this format's state. A
    direction nobody has measured raises :class:`StateTransformError` rather
    than guessing.
    """

    def lift(self, state: bytes) -> bytes: ...

    def lower(self, patch: bytes) -> bytes: ...


@dataclass(frozen=True, slots=True)
class PluginFormat:
    """The plugin as one plugin format hosts it.

    ``controller`` is what a VST3 device's controller half holds, built from the
    plugin's own patch. None on a VST3 format means nobody has measured it, and
    writing that format is refused: some plugins revert to their defaults
    without it. ``registration`` is what an AU device repeats from the
    component's registration; None on an AU format means it is not recorded,
    and writing that format is refused.
    """

    id: StoredId
    name: str
    framing: Framing
    measured: MeasuredState
    controller: ControllerState | None = None
    registration: AuRegistration | None = None


@dataclass(frozen=True, slots=True)
class Layout:
    """One way a build of this plugin saved its patch, recognised by its bytes."""

    name: str
    matches: Callable[[bytes], bool]


# -- files a patch points at ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class FileRef:
    """A file on disk the plugin's own patch points at.

    ``path`` is spelled as the patch stores it. ``kind`` says what the file is
    to the plugin (a sample, a kit). ``length`` is the audio length in seconds
    the patch records for it, where it records one, which helps tell two files
    of the same name apart.
    """

    path: str
    kind: str = "sample"
    length: float | None = None


AUDIO_SUFFIXES = frozenset({".wav", ".aif", ".aiff", ".flac", ".mp3", ".ogg", ".mp4", ".rx2"})
"""The audio files a plugin that plays samples from disk can point at."""


class FilePaths(Protocol):
    """How a plugin's own patch points at files on disk, and how to relink one.

    Most plugins point at nothing and their map has no ``FilePaths``. For the
    rest, ``read`` finds every file the patch names. ``rewrite`` answers the
    patch pointing ``ref`` at ``path`` instead, and raises
    :class:`StateTransformError` where nobody has measured a safe rewrite --
    ``rewrites`` says so up front. A plugin is given absolute paths: none has
    been measured to resolve one relative to the set. ``when_empty`` is what a
    patch pointing at no file means, where that is worth reporting.
    ``suffixes`` are the lowercase file suffixes a patch can point at, so the
    sample database indexes those files too and a moved one can be found again.
    """

    @property
    def suffixes(self) -> frozenset[str]: ...

    @property
    def rewrites(self) -> bool: ...

    @property
    def when_empty(self) -> str | None: ...

    def read(self, patch: bytes) -> tuple[FileRef, ...]: ...

    def rewrite(self, patch: bytes, ref: FileRef, path: pathlib.Path) -> bytes: ...


# -- the plugin ----------------------------------------------------------------

_ID_TYPES: dict[PluginKind, type[StoredId]] = {PluginKind.VST: Vst2Id, PluginKind.VST3: Vst3Id, PluginKind.AU: AuId}


@dataclass(frozen=True, slots=True)
class PluginMap:
    """Everything known about one plugin, independent of converters and hosts.

    A missing version means the product release is unknown; formats and scope
    still bound the measurements. Fingerprints describe the evidence; callers
    enforce binary compatibility. ``file_paths`` is None for a plugin whose
    patch names no files on disk, which is nearly all of them.
    """

    id: str
    name: str
    version: str | None
    scope: str
    vendor: str = ""
    vst2: PluginFormat | None = None
    vst3: PluginFormat | None = None
    au: PluginFormat | None = None
    layouts: tuple[Layout, ...] = ()
    binary_sha256: tuple[str, ...] = ()
    file_paths: FilePaths | None = None

    def __post_init__(self) -> None:
        _text(self.id, "Plugin-map ID")
        _text(self.name, "Plugin name")
        _text(self.scope, "Plugin-map scope")
        if self.version is not None:
            _text(self.version, "Plugin version")
        if type(self.layouts) is not tuple:
            raise StateTransformError("Plugin-map layouts must be an immutable tuple")
        if not self.formats:
            raise StateTransformError("Plugin map requires a plugin format")
        for kind, plugin_format in self.formats.items():
            if not isinstance(plugin_format.id, _ID_TYPES[kind]):
                raise StateTransformError(
                    f"The {kind.value} format of {self.name} holds a {type(plugin_format.id).__name__}"
                )
            if plugin_format.registration is not None and kind is not PluginKind.AU:
                raise StateTransformError(f"Only an AU format has a registration, not {self.name}'s {kind.value}")
        if type(self.binary_sha256) is not tuple or len(set(self.binary_sha256)) != len(self.binary_sha256):
            raise StateTransformError("Plugin-map binary hashes must be unique and immutable")
        for digest in self.binary_sha256:
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise StateTransformError("Plugin-map binary identity requires lowercase SHA256")

    @property
    def formats(self) -> dict[PluginKind, PluginFormat]:
        """The plugin formats this plugin is mapped in."""
        candidates = {PluginKind.VST: self.vst2, PluginKind.VST3: self.vst3, PluginKind.AU: self.au}
        return {kind: plugin_format for kind, plugin_format in candidates.items() if plugin_format is not None}

    def plugin_format(self, kind: PluginKind) -> PluginFormat:
        """The plugin as ``kind`` hosts it; refused when that format is not mapped."""
        plugin_format = self.formats.get(kind)
        if plugin_format is None:
            raise StateTransformError(f"{self.name} has no measured {kind.value} format")
        return plugin_format

    def can_write(self, kind: PluginKind) -> bool:
        """Whether this plugin's patch can be written for a device of ``kind`` at all."""
        plugin_format = self.formats.get(kind)
        if plugin_format is None:
            return False
        if kind is PluginKind.AU:
            return plugin_format.registration is not None
        return kind is not PluginKind.VST3 or plugin_format.controller is not None

    def lift(self, saved: FormatState, source: PluginKind) -> bytes:
        """The plugin's own patch inside ``saved``, held by a device of ``source``.

        Refused when the format is not mapped, and when the patch matches no
        recorded layout or one the plugin will not load.
        """
        patch = self.plugin_format(source).framing.lift(saved.state)
        self._check_layout(patch)
        return patch

    def lower(self, patch: bytes, target: PluginKind) -> FormatState:
        """The plugin's own ``patch`` as a device of ``target`` holds it.

        Refused when the format is not mapped, and when a VST3 target's
        controller half is not known.
        """
        to_format = self._writable(target)
        controller = None if to_format.controller is None else to_format.controller.build(patch) or b""
        return FormatState(to_format.framing.lower(patch), controller)

    def cross(self, saved: FormatState, source: PluginKind, target: PluginKind) -> FormatState:
        """``saved``, held by a device of ``source``, as a device of ``target`` holds it."""
        self._writable(target)
        return self.lower(self.lift(saved, source), target)

    def _writable(self, target: PluginKind) -> PluginFormat:
        to_format = self.plugin_format(target)
        if target is PluginKind.VST3 and to_format.controller is None:
            raise StateTransformError(
                f"{self.name}'s VST3 controller half is not measured; writing it would lose the patch"
            )
        if target is PluginKind.AU and to_format.registration is None:
            raise StateTransformError(f"{self.name}'s AU registration is not recorded; a set repeats it")
        return to_format

    def _check_layout(self, patch: bytes) -> None:
        if self.layouts and not any(layout.matches(patch) for layout in self.layouts):
            raise StateTransformError(f"This {self.name} patch matches no recorded layout")
