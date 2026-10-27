"""iZotope's plugins, and the VST2 wrapper they share.

An iZotope VST2 chunk is the VST3 processor state with a length in front of it
and a preset name behind it. Measured 2026-08-13 on Trash 2 and Ozone 9 devices
a set stores both ways. The VST2 chunk is the plugin's own patch, because it is
the one that holds everything, name included; the VST3 holds it without the
name. Reading the nesting is
:func:`~abletoolz.plugins.state.framings.izotope_unwrap`; what is iZotope's
own, and lives here, is the framing a plugin map names and the refusal for a
chunk that is not that shape.

Neither plugin's VST3 controller half is measured, and Trash 2 is known to
revert to its defaults without one, so neither VST3 can be written from here
yet. Trash 2's map, with its codec, is in
:mod:`abletoolz.plugins.state.izotope_trash2`.
"""

from __future__ import annotations

import dataclasses
import datetime
import pathlib
import re

from abletoolz.plugins.identity.ids import Vst2Id, Vst3Id
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.framings import VERBATIM, izotope_unwrap
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import AUDIO_SUFFIXES, FileRef, Layout, PluginFormat, PluginMap

# The day the host rig read the wrapper off Trash 2 and Ozone 9 devices.
NESTING_MEASURED = MeasuredState(StateRung.REFRAME, (StateEvidence.HOSTED,), datetime.date(2026, 8, 13))


# The one shape of patch measured: a VST2 chunk the nesting accounts for.
WRAPPED = Layout("VST2 wrapper", lambda patch: izotope_unwrap(patch) is not None)


@dataclasses.dataclass(frozen=True, slots=True)
class IzotopeVst3:
    """An iZotope VST3 processor state: the VST2 chunk with its length and preset name taken off.

    Only the way out of the chunk is measured. Rebuilding the chunk needs the
    preset name, which the VST3 state does not hold.
    """

    def lift(self, state: bytes) -> bytes:
        raise StateTransformError("Rebuilding an iZotope VST2 chunk needs its preset name, which is not measured")

    def lower(self, patch: bytes) -> bytes:
        state = izotope_unwrap(patch)
        if state is None:
            raise StateTransformError(
                f"A {len(patch)} byte chunk is not an iZotope VST2 wrapper: its leading length and its "
                "trailing preset name do not account for it."
            )
        return state


@register
def ozone_9_exciter() -> PluginMap:
    return PluginMap(
        id="izotope.ozone-9-exciter",
        name="Ozone 9 Exciter",
        version="9",
        scope="VST2 wrapper around the VST3 processor state; controller half not measured",
        vendor="iZotope",
        vst2=PluginFormat(Vst2Id(1515141464), "Ozone 9 Exciter", VERBATIM, NESTING_MEASURED),
        vst3=PluginFormat(
            Vst3Id((1448301658, 1329158223, 2054123109, 540614725)), "Ozone 9 Exciter", IzotopeVst3(), NESTING_MEASURED
        ),
        layouts=(WRAPPED,),
    )


# -- Iris: the samples its layers play -------------------------------------------------
#
# Iris names each layer's sample by its absolute path, as a u32 byte count and that many bytes of UTF-8,
# inside the VST2 wrapper and among lengths of Iris's own; its library's .irss files sit in the user's
# Documents, so a new machine or user name loses them. Measured 2026-10-24 on the one Iris state in the
# user's sets: four sample paths, all in this form. Iris is not installed here, so nothing is rewritten.

IRIS_SUFFIXES = frozenset({".irss"}) | AUDIO_SUFFIXES
"""Iris's own sample files, and audio a user loaded into a layer."""

_ABSOLUTE_START = re.compile(rb"[A-Za-z]:[\\/]|/")
_MAX_PATH_BYTES = 4096


@dataclasses.dataclass(frozen=True, slots=True)
class IrisSamplePaths:
    """The samples an Iris patch's layers play; read only until a rewrite is measured."""

    rewrites: bool = False
    when_empty: str | None = None
    suffixes: frozenset[str] = IRIS_SUFFIXES

    def read(self, patch: bytes) -> tuple[FileRef, ...]:
        found: dict[str, FileRef] = {}
        for match in _ABSOLUTE_START.finditer(patch):
            start = match.start()
            size = int.from_bytes(patch[start - 4 : start], "little") if start >= 4 else 0
            if not 4 <= size <= _MAX_PATH_BYTES or start + size > len(patch):
                continue
            try:
                path = patch[start : start + size].decode("utf-8")
            except UnicodeDecodeError:
                continue
            if pathlib.PurePath(path.replace("\\", "/")).suffix.lower() in self.suffixes:
                found.setdefault(path, FileRef(path))
        return tuple(found.values())

    def rewrite(self, patch: bytes, ref: FileRef, path: pathlib.Path) -> bytes:
        raise StateTransformError("Iris samples are not relinked from here yet. Repoint them in Iris.")


@register
def iris() -> PluginMap:
    return PluginMap(
        id="izotope.iris",
        name="iZotope Iris",
        version="1",
        scope="VST2 wrapper; each layer's sample named by absolute path",
        vendor="iZotope",
        vst2=PluginFormat(
            Vst2Id(1767524691), "iZotope Iris", VERBATIM, MeasuredState(StateRung.VERBATIM, (StateEvidence.STRUCTURAL,))
        ),
        file_paths=IrisSamplePaths(),
    )
