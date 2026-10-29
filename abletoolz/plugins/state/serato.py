"""Serato's plugins.

Serato Sample saves JSON, the same in its VST2 and VST3, and the VST3 writes no
controller half. Heard in Live 12.4.5 on 2026-08-10. The JSON carries its own
version (``project.Version``), and a newer build adds keys while keeping every
one an older patch gives it.

The patch names the one audio file it plays, at ``project.sourceSong.File``,
with its length in seconds at ``project.sourceSong.Length``: :class:`SeratoSamplePaths`
reads and relinks it. Serato writes forward slashes on every OS. Only absolute
paths are known to load.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import pathlib
from typing import Any

from abletoolz.plugins.container.controller import NO_CONTROLLER_STATE
from abletoolz.plugins.identity.ids import Vst2Id, Vst3Id
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.framings import VERBATIM
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import AUDIO_SUFFIXES, FileRef, PluginFormat, PluginMap

# What a patch with no file means. Serato writes one when the file it pointed at
# was missing at save time, and the path goes with it.
NO_SAMPLE = (
    "project holds no sample -- the reference is not in this save, so there is nothing to relink; "
    "an older save of the set may still carry it"
)


def _json_span(patch: bytes) -> tuple[int, int] | None:
    """Where the JSON object sits in the patch, end exclusive."""
    start, end = patch.find(b"{"), patch.rfind(b"}")
    return None if start == -1 or end <= start else (start, end + 1)


def _document(patch: bytes) -> dict[str, Any] | None:
    span = _json_span(patch)
    if span is None:
        return None
    try:
        document = json.loads(patch[span[0] : span[1]].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def _source_song(document: dict[str, Any] | None) -> dict[str, Any] | None:
    project = None if document is None else document.get("project")
    song = project.get("sourceSong") if isinstance(project, dict) else None
    return song if isinstance(song, dict) else None


@dataclasses.dataclass(frozen=True, slots=True)
class SeratoSamplePaths:
    """The audio file a Serato Sample patch plays."""

    rewrites: bool = True
    when_empty: str | None = NO_SAMPLE
    suffixes: frozenset[str] = AUDIO_SUFFIXES

    def read(self, patch: bytes) -> tuple[FileRef, ...]:
        song = _source_song(_document(patch))
        path, length = (None, None) if song is None else (song.get("File"), song.get("Length"))
        if not isinstance(path, str) or not path:
            return ()
        return (FileRef(path, "sample", float(length) if isinstance(length, (int, float)) else None),)

    def rewrite(self, patch: bytes, ref: FileRef, path: pathlib.Path) -> bytes:
        span, document = _json_span(patch), _document(patch)
        song = _source_song(document)
        if span is None or document is None or song is None or song.get("File") != ref.path:
            raise StateTransformError(f"This Serato Sample patch does not point at {ref.path}")
        song["File"] = path.as_posix()
        text = json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return patch[: span[0]] + text + patch[span[1] :]


@register
def sample() -> PluginMap:
    measured = MeasuredState(StateRung.VERBATIM, (StateEvidence.EAR,), datetime.date(2026, 8, 10))
    return PluginMap(
        id="serato.sample",
        name="Serato Sample",
        version=None,
        scope="VST2 and VST3 save the same patch",
        vendor="Serato",
        vst2=PluginFormat(Vst2Id(1399681132), "Serato Sample", VERBATIM, measured, plan_name="serato_sample_vst2"),
        vst3=PluginFormat(
            Vst3Id((1448301651, 1836084339, 1701994868, 1864397665)),
            "Serato Sample",
            VERBATIM,
            measured,
            NO_CONTROLLER_STATE,
            plan_name="serato_sample_vst3",
        ),
        file_paths=SeratoSamplePaths(),
    )
