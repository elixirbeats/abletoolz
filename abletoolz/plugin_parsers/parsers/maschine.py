"""Maschine 2 sample and kit reference parser.

Maschine 2 saves its whole project inside the plugin buffer -- a quarter of a
megabyte is normal -- and buries the references to the kits and samples it was
built from somewhere in the middle. When the drive those came from goes away,
nothing in a set tells you: the device still loads, it just cannot find its
sources any more. This parser finds those references and says which ones are
gone.

Reading them
------------
The buffer is Native Instruments' nested chunk container. Every chunk opens with
a little-endian u64 byte size, a u32 type, and a four character magic -- ``hsin``
for a node, ``DSIN`` for its data -- and the size at offset 0 is always the
length of the whole buffer.

Inside, strings are framed as a little-endian u32 *character* count followed by
that many UTF-16LE code units, with no terminator::

    5b 00 00 00  43 00 3a 00 5c 00 ...   -> 91 chars, "C:\\Sample Libraries\\..."

That framing is what makes detection exact rather than a text search: a
candidate only counts when the length prefix lands the string's end precisely on
a known suffix. Kit references (``.mxgrp``) and loose audio (``.wav``) both show
up this way, sitting in records that name the reference kind -- ``Group`` for a
kit, ``Sample`` for audio -- just before the path.

Why it does not write
---------------------
The path lives inside a stack of chunks, and every one of them records its own
byte length. A replacement path of a different length would have to update the
outer u64 and each enclosing u64 above it, in an undocumented format that may
carry offsets or digests elsewhere too. Getting that wrong does not fail loudly;
it silently destroys a project the set can no longer open. So this parser
detects and reports, and relinking a Maschine kit stays a manual job in Maschine
itself. A correct detector is worth shipping on its own.
"""

from __future__ import annotations

import logging
import pathlib
import re
from typing import TYPE_CHECKING

from abletoolz.plugin_parsers.base import (
    BufferFormat,
    PluginAnalysis,
    PluginData,
    SampleContainerParser,
)
from abletoolz.plugin_parsers.registry import register_parser

if TYPE_CHECKING:
    from abletoolz.sample_databaser.create_db import DatabaseT

logger = logging.getLogger(__name__)

CONTAINER_MAGIC = b"hsin"
"""Magic of the outermost chunk, twelve bytes into the buffer."""

CONTAINER_MAGIC_OFFSET = 12

KIT_SUFFIXES = frozenset({".mxgrp", ".mxsnd"})
"""Maschine's own group and sound files."""

AUDIO_SUFFIXES = frozenset({".wav", ".aif", ".aiff", ".flac", ".mp3", ".ogg", ".mp4", ".rx2"})
"""Loose audio a user dropped onto a pad."""

REFERENCE_SUFFIXES = KIT_SUFFIXES | AUDIO_SUFFIXES

MAX_PATH_CHARS = 4096
"""Sanity bound on the length prefix, well past any real Windows path."""

# Where a path can start, in UTF-16LE: a drive letter ("C", NUL, ":", NUL, "\",
# NUL), which is every path the corpus holds, or a leading slash for a set saved
# on macOS. The length prefix is what decides a candidate either way, so the
# second anchor costs nothing and stops a Mac set reading as empty.
_PATH_START = re.compile(rb"[A-Za-z]\x00:\x00\\\x00|/\x00")


def _decode_prefixed_string(raw: bytes, start: int) -> str | None:
    """Decode the UTF-16LE string whose length prefix sits just before ``start``.

    Answers None unless the prefix is present, in range, and describes text that
    decodes cleanly and holds no control characters.
    """
    if start < 4:
        return None
    count = int.from_bytes(raw[start - 4 : start], "little")
    if not 4 <= count <= MAX_PATH_CHARS:
        return None
    end = start + count * 2
    if end > len(raw):
        return None
    try:
        text = raw[start:end].decode("utf-16-le")
    except UnicodeDecodeError:
        return None
    if any(char < " " for char in text):
        return None
    return text


@register_parser
class Maschine2Parser(SampleContainerParser):
    """Parser for Native Instruments Maschine 2.

    Buffer format: nested NI chunks holding length-prefixed UTF-16LE strings.
    Detection only -- see the module docstring for why there is no writer.
    """

    name = "maschine_2"
    description = "Maschine 2 - kit and sample references (detection only)"
    buffer_format = BufferFormat.PROPRIETARY
    unique_ids = [1315523890]
    # Substring matching, so this covers the "Maschine 2 FX" effect build too.
    name_patterns = ["Maschine 2"]

    def _state(self, plugin: PluginData) -> bytes | None:
        """Return the raw state if it is an NI chunk container."""
        raw = plugin.get_buffer_raw_bytes()
        magic_end = CONTAINER_MAGIC_OFFSET + len(CONTAINER_MAGIC)
        if len(raw) < magic_end:
            return None
        if raw[CONTAINER_MAGIC_OFFSET:magic_end] != CONTAINER_MAGIC:
            return None
        return raw

    def get_sample_paths(self, plugin: PluginData) -> list[pathlib.Path]:
        """Extract every kit and sample reference, in the order they appear.

        Repeats are dropped: Maschine stores the same kit path several times over.
        """
        raw = self._state(plugin)
        if raw is None:
            return []

        found: list[pathlib.Path] = []
        seen: set[str] = set()
        for match in _PATH_START.finditer(raw):
            text = _decode_prefixed_string(raw, match.start())
            if text is None or text in seen:
                continue
            # The plugin writes Windows separators; normalize so this reads anywhere.
            path = pathlib.Path(text.replace("\\", "/"))
            if path.suffix.lower() not in REFERENCE_SUFFIXES:
                continue
            seen.add(text)
            found.append(path)
        return found

    def set_sample_path(self, plugin: PluginData, old_path: pathlib.Path, new_path: pathlib.Path) -> bool:
        """Refuse to rewrite, always.

        A different-length path would leave every enclosing chunk's byte length
        stale. Until that container is understood well enough to update all of
        them, saying no is the only honest answer.
        """
        logger.debug("Maschine 2 state is detection only; not rewriting %s", old_path)
        return False

    def analyze(self, plugin: PluginData) -> PluginAnalysis:
        """Report which kits and samples this Maschine 2 instance can no longer find."""
        analysis = PluginAnalysis(
            plugin_name=plugin.plugin_name,
            plugin_path=plugin.path,
            exists=plugin.path.exists() if plugin.path else False,
            unique_id=plugin.unique_id,
            format="VST2",
        )

        references = self.get_sample_paths(plugin)
        analysis.metadata["references"] = [str(path) for path in references]
        analysis.metadata["kit_references"] = sum(1 for p in references if p.suffix.lower() in KIT_SUFFIXES)
        analysis.metadata["audio_references"] = sum(1 for p in references if p.suffix.lower() in AUDIO_SUFFIXES)
        analysis.metadata["relink_supported"] = False

        missing = [path for path in references if not path.exists()]
        analysis.metadata["missing_references"] = [str(path) for path in missing]
        if not missing:
            return analysis

        for path in missing:
            kind = "kit" if path.suffix.lower() in KIT_SUFFIXES else "sample"
            analysis.issues.append(f"Missing {kind}: {path}")
        analysis.issues.append(
            "Maschine 2 references cannot be relinked from here: they sit inside nested chunks whose "
            "lengths would all have to be rewritten. Repoint them in Maschine."
        )
        return analysis

    def fix(self, plugin: PluginData, db: DatabaseT | None = None) -> bool:
        """Never fixes. Detection only, for the reason in the module docstring."""
        return False
