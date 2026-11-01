"""Arturia's plugins.

Prophet V3, ARP 2600 V3, Modular V3, Piano V3 and Wurli V3 keep their patch as
a Boost text archive with a map of named numbers: the same archive twice in a
VST2 chunk, and one archive in each VST3 half, which is how Live saves them.
Hosted, each VST3 plays a patch handed to it that way as its VST2 does.
"""

from __future__ import annotations

import datetime
import pathlib
import re
from dataclasses import dataclass

from abletoolz.plugins.identity.ids import Vst2Id, Vst3Id
from abletoolz.plugins.state.arturia_v import FirstArchive, OneArchive, SecondArchive, holds_two_archives
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.framings import VERBATIM
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import AUDIO_SUFFIXES, FileRef, Layout, PluginFormat, PluginMap

ARTURIA_VST3 = (1098019957, 1096173907)
ARTURIA_VST3_TAIL = 1349676899


def _v3(map_id: str, name: str, unique_id: int, plan: str, version: str) -> PluginMap:
    measured = MeasuredState(StateRung.VERBATIM, (StateEvidence.HOSTED,), datetime.date(2026, 10, 31))
    split = MeasuredState(StateRung.REFRAME, (StateEvidence.HOSTED,), datetime.date(2026, 10, 31))
    return PluginMap(
        id=map_id,
        name=name,
        version="3",
        scope=f"{version}; the same archive twice, with its map of named numbers; the VST3 is written, not read",
        vendor="Arturia",
        vst2=PluginFormat(Vst2Id(unique_id), name, VERBATIM, measured, plan_name=f"{plan}_vst2"),
        vst3=PluginFormat(
            Vst3Id((*ARTURIA_VST3, unique_id, ARTURIA_VST3_TAIL)),
            name,
            FirstArchive(name),
            split,
            SecondArchive(name),
            plan_name=f"{plan}_vst3",
        ),
        layouts=(Layout("two archives", holds_two_archives),),
    )


@register
def prophet_v3() -> PluginMap:
    # The VST3 was once written as the VST2 chunk with no controller half; hosted, it ignores a patch handed that way.
    return _v3("arturia.prophet-v3", "Prophet V3", 1347571507, "prophet_v3", "3.9.1.1843")


# -- Piano V and Wurli V: the second generation, and the third that reads it -----------------
#
# Piano V2 and Wurli V2 already save what their V3 successors do: Boost text archives holding a map of named
# numbers. Piano V2's VST2 chunk is two archives behind their lengths; the first is an older copy with fewer
# names and no preset record, the second the patch. Wurli V2's VST3 writes one archive as both halves. Each V3
# keeps the same archive twice in its VST2 chunk and as its VST3 halves.


@register
def piano_v2() -> PluginMap:
    measured = MeasuredState(StateRung.VERBATIM, (StateEvidence.STRUCTURAL,))
    return PluginMap(
        id="arturia.piano-v2",
        name="Piano V2",
        version="2",
        scope="VST2; two archives behind their lengths, an older copy and the patch",
        vendor="Arturia",
        vst2=PluginFormat(Vst2Id(1349083442), "Piano V2", VERBATIM, measured, plan_name="piano_v2_vst2"),
        layouts=(Layout("two archives", holds_two_archives),),
    )


@register
def piano_v3() -> PluginMap:
    return _v3("arturia.piano-v3", "Piano V3", 1349083443, "piano_v3", "3.3.3.6106")


@register
def wurli_v2() -> PluginMap:
    measured = MeasuredState(StateRung.VERBATIM, (StateEvidence.STRUCTURAL,))
    return PluginMap(
        id="arturia.wurli-v2",
        name="Wurli V2",
        version="2",
        scope="VST3; one archive as both halves",
        vendor="Arturia",
        vst3=PluginFormat(
            Vst3Id((*ARTURIA_VST3, 1465209394, ARTURIA_VST3_TAIL)),
            "Wurli V2",
            OneArchive("Wurli V2"),
            measured,
            plan_name="wurli_v2_vst3",
        ),
        layouts=(Layout("two archives", holds_two_archives),),
    )


@register
def wurli_v3() -> PluginMap:
    return _v3("arturia.wurli-v3", "Wurli V3", 1465209395, "wurli_v3", "3.0.3.6106")


@register
def arp_2600_v3() -> PluginMap:
    return _v3("arturia.arp-2600-v3", "ARP 2600 V3", 1095913523, "arp_2600_v3", "3.13.3.6106")


@register
def modular_v3() -> PluginMap:
    return _v3("arturia.modular-v3", "Modular V3", 1297040435, "modular_v3", "3.13.3.6106")


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
