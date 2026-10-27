"""Akai's plugins.

MPC Essentials keeps no patch of its own: its state names the MPC project it
has open, an ``.xpj``, and the project holds everything else. The state is a
u64 1, the project's absolute path ending in a NUL, then the view the plugin
was showing ("Main mode", "Program mixer") and a NUL. Measured 2026-10-24 on
the two MPC Essentials states in the user's sets. The plugin is not installed
here, so nothing is rewritten.
"""

from __future__ import annotations

import dataclasses
import pathlib

from abletoolz.plugins.identity.ids import Vst2Id
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.framings import VERBATIM
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import FileRef, PluginFormat, PluginMap

PROJECT_SUFFIXES = frozenset({".xpj"})
"""MPC projects."""

_HEAD = (1).to_bytes(8, "little")


@dataclasses.dataclass(frozen=True, slots=True)
class MpcProjectPaths:
    """The MPC project an MPC Essentials state has open; read only until a rewrite is measured."""

    rewrites: bool = False
    when_empty: str | None = None
    suffixes: frozenset[str] = PROJECT_SUFFIXES

    def read(self, patch: bytes) -> tuple[FileRef, ...]:
        end = patch.find(b"\x00", len(_HEAD))
        if not patch.startswith(_HEAD) or end < 0:
            return ()
        try:
            path = patch[len(_HEAD) : end].decode("utf-8")
        except UnicodeDecodeError:
            return ()
        if pathlib.PureWindowsPath(path).suffix.lower() not in self.suffixes:
            return ()
        return (FileRef(path, "project"),)

    def rewrite(self, patch: bytes, ref: FileRef, path: pathlib.Path) -> bytes:
        raise StateTransformError("MPC projects are not relinked from here yet. Open the project in MPC Essentials.")


@register
def mpc_essentials() -> PluginMap:
    return PluginMap(
        id="akai.mpc-essentials",
        name="MPC Essentials",
        version="1",
        scope="names the MPC project it has open; the project holds the rest",
        vendor="Akai",
        vst2=PluginFormat(
            Vst2Id(1299211077),
            "MPC Essentials",
            VERBATIM,
            MeasuredState(StateRung.VERBATIM, (StateEvidence.STRUCTURAL,)),
        ),
        file_paths=MpcProjectPaths(),
    )
