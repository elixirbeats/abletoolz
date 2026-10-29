"""Arturia's plugins.

Prophet V3's VST2 and VST3 save the same patch, and the VST3 writes no
controller half; heard in Live 12.4.5 on 2026-08-10.
"""

from __future__ import annotations

import datetime
import pathlib
import re
from dataclasses import dataclass

from abletoolz.plugins.container.controller import NO_CONTROLLER_STATE
from abletoolz.plugins.identity.ids import Vst2Id, Vst3Id
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.framings import VERBATIM
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import AUDIO_SUFFIXES, FileRef, PluginFormat, PluginMap


@register
def prophet_v3() -> PluginMap:
    measured = MeasuredState(StateRung.VERBATIM, (StateEvidence.EAR,), datetime.date(2026, 8, 10))
    return PluginMap(
        id="arturia.prophet-v3",
        name="Prophet V3",
        version="3",
        scope="VST2 and VST3 save the same patch",
        vendor="Arturia",
        vst2=PluginFormat(Vst2Id(1347571507), "Prophet V3", VERBATIM, measured, plan_name="prophet_v3_vst2"),
        vst3=PluginFormat(
            Vst3Id((1098019957, 1096173907, 1347571507, 1349676899)),
            "Prophet V3",
            VERBATIM,
            measured,
            NO_CONTROLLER_STATE,
            plan_name="prophet_v3_vst3",
        ),
    )


# -- CMI V: the samples its voices play ------------------------------------------------
#
# Each voice keeps its sample in an object named ``Voice<n>_AudioSampleObject`` in the text archive: the
# name, a byte count, then that many bytes of a portable binary archive -- its signature, five bytes, and the
# path in a fixed 256 byte field, padded with NULs. Factory sounds are named relative to the plugin's own
# library ("Factory/MALLETS/BASSMAR1.wav"), or by its absolute path when it is the plugin's own resource
# (".../CMI V/resources/.../data0.bin", not a sample); a sample the user loaded is an absolute path. The
# patch holds two copies of every voice, one per section, and both name the same file. Measured 2026-10-24
# over the user's sets: all 236 voice objects in 11 states have this layout.
#
# A rewrite fills the same field, so nothing else in the patch moves. Measured 2026-10-24 in DawDreamer with
# the installed CMI V VST2: a user's bass loop patch whose sample is missing here renders silence; relinked
# to a generated 1 kHz tone, all 72 keys of a sweep sound that tone, a semitone apart, and the state CMI V
# saves back still names it.

_SAMPLE_OBJECT = re.compile(rb" \d+ Voice\d+_AudioSampleObject (\d+) ")
_SAMPLE_HEAD = b"\x01\x16serialization::archive\x01\x0a\x00\x00\x00"
_SAMPLE_PATH_BYTES = 256
_ABSOLUTE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")


def _sample_fields(patch: bytes) -> list[tuple[int, str]]:
    """Where each voice's 256 byte path field starts, and the path it holds."""
    fields = []
    for match in _SAMPLE_OBJECT.finditer(patch):
        start = match.end()
        blob = patch[start : start + int(match.group(1))]
        if not blob.startswith(_SAMPLE_HEAD) or len(blob) < len(_SAMPLE_HEAD) + _SAMPLE_PATH_BYTES:
            continue
        field_at = start + len(_SAMPLE_HEAD)
        raw = patch[field_at : field_at + _SAMPLE_PATH_BYTES].split(b"\x00", 1)[0]
        try:
            fields.append((field_at, raw.decode("utf-8")))
        except UnicodeDecodeError:
            continue
    return fields


@dataclass(frozen=True, slots=True)
class CmiSamplePaths:
    """The samples a CMI V patch's voices play, where the user loaded them from disk."""

    rewrites: bool = True
    when_empty: str | None = None
    suffixes: frozenset[str] = AUDIO_SUFFIXES

    def read(self, patch: bytes) -> tuple[FileRef, ...]:
        names = dict.fromkeys(
            path
            for _, path in _sample_fields(patch)
            if _ABSOLUTE_PATH.match(path) and pathlib.PurePath(path.replace("\\", "/")).suffix.lower() in self.suffixes
        )
        return tuple(FileRef(name) for name in names)

    def rewrite(self, patch: bytes, ref: FileRef, path: pathlib.Path) -> bytes:
        encoded = str(path).encode("utf-8")
        if len(encoded) >= _SAMPLE_PATH_BYTES:
            raise StateTransformError(f"{path} is too long for CMI V's {_SAMPLE_PATH_BYTES} byte path field")
        out = bytearray(patch)
        count = 0
        for field_at, stored in _sample_fields(patch):
            if stored == ref.path:
                out[field_at : field_at + _SAMPLE_PATH_BYTES] = encoded.ljust(_SAMPLE_PATH_BYTES, b"\x00")
                count += 1
        if not count:
            raise StateTransformError(f"This CMI V patch does not point at {ref.path}")
        return bytes(out)


@register
def cmi_v() -> PluginMap:
    measured = MeasuredState(StateRung.VERBATIM, (StateEvidence.STRUCTURAL,))
    return PluginMap(
        id="arturia.cmi-v",
        name="CMI V",
        version="5",
        scope="text archive of objects; each voice's sample in a fixed 256 byte path field",
        vendor="Arturia",
        vst2=PluginFormat(Vst2Id(1129138550), "CMI V", VERBATIM, measured, plan_name="cmi_v_vst2"),
        file_paths=CmiSamplePaths(),
    )
