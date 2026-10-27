"""Devious Machines' plugins.

Texture layers a sound onto its input, following the input's level. Its own
sounds ship packed in ``.noises`` bundles and a patch names them by id; a sample
the user dragged in is named by its absolute path, and that is the file a patch
can lose.

The patch is JUCE's XML state behind ``VC2!``: the magic, a u32 byte count, the
XML and a NUL. The sample is a ``<file name="..." start=... end=.../>`` inside
the ``ProcessorStateString`` whose ``__id`` is ``sample``. The VST2 stores that
as it is; the VST3 stores it the same way or, in older builds, inside a
``VstW`` bank with JUCE's private data after the XML. Measured 2026-10-24 over
the user's sets: every VST2 state and one VST3 is the bare form, 33 VST3 states
the bank, and each bank re-encodes byte for byte. No VST3 device stores a
controller half.

A rewrite replaces the name and recomputes the byte count, and the bank's
around it. Measured 2026-10-24 in DawDreamer with the installed Texture VST2:
a user's snare patch whose sample is missing here, relinked to a generated
1 kHz tone, puts 30 times the energy at 1 kHz into the output (1959 against 66)
with 3 kHz unchanged, so Texture plays the file the rewritten path names.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re
from collections.abc import Callable
from xml.sax.saxutils import escape, unescape

from abletoolz.formats import FormatError
from abletoolz.formats.fxb import FxbChunkBank
from abletoolz.plugins.container.controller import NO_CONTROLLER_STATE
from abletoolz.plugins.identity.ids import Vst2Id, Vst3Id
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.framings import JUCE_MAGIC, VERBATIM, VSTW_HEADER_BYTES, VSTW_MAGIC
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import AUDIO_SUFFIXES, FileRef, PluginFormat, PluginMap

_ATTRIBUTE = '"'

# A sample file element, its name captured as XML spells it.
_FILE = re.compile(r'<file name="([^"]*)"')
_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")


@dataclasses.dataclass(frozen=True, slots=True)
class TexturePaths:
    """The sample a Texture patch layers in, when the user chose it from disk rather than the plugin's own."""

    rewrites: bool = True
    when_empty: str | None = None
    suffixes: frozenset[str] = AUDIO_SUFFIXES

    def read(self, patch: bytes) -> tuple[FileRef, ...]:
        xml = _xml(patch)
        if xml is None:
            return ()
        names = (unescape(name, {"&quot;": _ATTRIBUTE}) for name in _FILE.findall(xml))
        return tuple(FileRef(name) for name in dict.fromkeys(names) if _ABSOLUTE.match(name))

    def rewrite(self, patch: bytes, ref: FileRef, path: pathlib.Path) -> bytes:
        old = f'<file name="{escape(ref.path, {_ATTRIBUTE: "&quot;"})}"'
        new = f'<file name="{escape(str(path), {_ATTRIBUTE: "&quot;"})}"'
        return _rewrite_xml(patch, lambda xml: _replace(xml, old, new, ref.path))


def _replace(xml: str, old: str, new: str, wanted: str) -> str:
    if old not in xml:
        raise StateTransformError(f"This Texture patch does not point at {wanted}")
    return xml.replace(old, new)


def _xml(patch: bytes) -> str | None:
    """The XML a Texture patch holds, in either of its stored forms, or None for anything else."""
    block = _juce_block(patch)
    if block is None:
        return None
    start, size = block
    try:
        return patch[start + 8 : start + 8 + size].decode("utf-8")
    except UnicodeDecodeError:
        return None


def _juce_block(patch: bytes) -> tuple[int, int] | None:
    """Where the ``VC2!`` block starts, bare or inside the bank, and its XML's byte count."""
    if patch[:4] == JUCE_MAGIC:
        start = 0
    elif patch[:4] == VSTW_MAGIC:
        start = patch.find(JUCE_MAGIC, VSTW_HEADER_BYTES)
        if start < 0:
            return None
    else:
        return None
    size = int.from_bytes(patch[start + 4 : start + 8], "little")
    return (start, size) if start + 8 + size <= len(patch) else None


def _rewrite_xml(patch: bytes, edit: Callable[[str], str]) -> bytes:
    """``patch`` with its XML passed through ``edit``, every count around it recomputed."""
    if patch[:4] == VSTW_MAGIC:
        try:
            bank = FxbChunkBank.parse(patch[VSTW_HEADER_BYTES:])
        except FormatError as error:
            raise StateTransformError(f"This Texture VST3 bank is not one measured: {error}") from error
        end = VSTW_HEADER_BYTES + len(bank.encode())
        chunk = _rewrite_bare(bank.chunk, edit)
        rebuilt = dataclasses.replace(bank, chunk=chunk).encode()
        return patch[:VSTW_HEADER_BYTES] + rebuilt + patch[end:]
    return _rewrite_bare(patch, edit)


def _rewrite_bare(block: bytes, edit: Callable[[str], str]) -> bytes:
    if block[:4] != JUCE_MAGIC:
        raise StateTransformError("This is not a Texture patch: no JUCE state")
    size = int.from_bytes(block[4:8], "little")
    xml = edit(block[8 : 8 + size].decode("utf-8")).encode("utf-8")
    return JUCE_MAGIC + len(xml).to_bytes(4, "little") + xml + block[8 + size :]


_STRUCTURAL = MeasuredState(StateRung.VERBATIM, (StateEvidence.STRUCTURAL,))


@register
def texture() -> PluginMap:
    return PluginMap(
        id="devious-machines.texture",
        name="Texture",
        version=None,
        scope="JUCE XML state; a user sample is named by its absolute path",
        vendor="Devious Machines",
        vst2=PluginFormat(Vst2Id(1484419439), "Texture", VERBATIM, _STRUCTURAL),
        vst3=PluginFormat(
            Vst3Id((1448301656, 2054254452, 1702392949, 1919221760)),
            "Texture",
            VERBATIM,
            _STRUCTURAL,
            NO_CONTROLLER_STATE,
        ),
        file_paths=TexturePaths(),
    )
