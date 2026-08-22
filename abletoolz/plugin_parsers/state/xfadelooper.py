"""xfadelooper sample parser.

xfadelooper is an old, tiny VST2 looper that keeps one sample path in its state,
and that state is a flat C struct rather than a serialized document. Every state
seen is exactly 652 bytes and lays out like this::

    offset  size  contents
    0       8     magic, ``lsxeslfx``
    8       4     layout version, ``00 02 03 00``
    12      32    preset name, NUL-terminated, NUL-padded
    44      256   sample path, NUL-terminated, NUL-padded
    300     352   88 little-endian float parameters

The path field is a fixed-width char array written into with a plain string
copy, so real states carry the tail of whatever longer path used to live there
after the terminator. That residue is dead to the plugin, which reads up to the
first NUL, and rewriting the whole field clears it.

Fixed width is what makes writing safe here: the buffer never changes length, so
no enclosing structure can go stale, and every parameter keeps its offset. The
one thing a writer must refuse is a replacement too long for the field, which
would otherwise be silently truncated into a path pointing nowhere.

The plugin ships under two names -- ``xfadelooper`` and ``xfadelooper.64`` -- and
both share unique id 1163098214, so one parser covers them.
"""

from __future__ import annotations

import logging
import pathlib
from typing import TYPE_CHECKING

from abletoolz.plugin_parsers.base import (
    BufferFormat,
    PluginAnalysis,
    PluginData,
    SampleContainerParser,
)
from abletoolz.plugin_parsers.registry import register_parser
from abletoolz.sample_matcher import select_best_candidate_by_name

if TYPE_CHECKING:
    from abletoolz.sample_databaser.create_db import DatabaseT

logger = logging.getLogger(__name__)

MAGIC = b"lsxeslfx"
"""First eight bytes of every xfadelooper state."""

LAYOUT_VERSION = b"\x00\x02\x03\x00"
"""Version dword the measured layout belongs to. Anything else is a stranger."""

PATH_OFFSET = 44
"""Where the sample path field starts."""

PATH_FIELD_SIZE = 256
"""How wide that field is. The parameter block starts immediately after it."""

PATH_END = PATH_OFFSET + PATH_FIELD_SIZE

# The path is a Windows char array. latin-1 maps every byte to a character and
# back again, so a read never loses information and a write round-trips exactly.
PATH_ENCODING = "latin-1"


@register_parser
class XfadeLooperParser(SampleContainerParser):
    """Parser for the xfadelooper VST2 looper.

    Buffer format: fixed-width binary struct. See the module docstring for the
    field map. Reads and writes the one sample path it holds.
    """

    name = "xfadelooper"
    description = "xfadelooper - crossfading sample looper"
    buffer_format = BufferFormat.BINARY_STRUCT
    unique_ids = [1163098214]
    # Substring matching, so this covers "xfadelooper.64" as well.
    name_patterns = ["xfadelooper"]

    def _state(self, plugin: PluginData) -> bytes | None:
        """Return the raw state if it is a layout this parser has measured."""
        raw = plugin.get_buffer_raw_bytes()
        if len(raw) < PATH_END:
            return None
        if raw[: len(MAGIC)] != MAGIC:
            return None
        if raw[8:12] != LAYOUT_VERSION:
            return None
        return raw

    def _read_path(self, raw: bytes) -> str:
        """Decode the path field up to its NUL terminator."""
        field = raw[PATH_OFFSET:PATH_END]
        return field.split(b"\x00", 1)[0].decode(PATH_ENCODING)

    def _write_path(self, raw: bytes, new_path: str) -> bytes | None:
        """Return the state with the path field replaced, or None if it will not fit."""
        try:
            encoded = new_path.encode(PATH_ENCODING)
        except UnicodeEncodeError:
            logger.debug("Path is not representable in the plugin's encoding: %s", new_path)
            return None
        # One byte has to be left for the terminator.
        if len(encoded) >= PATH_FIELD_SIZE:
            logger.debug("Path is too long for xfadelooper's %d byte field: %s", PATH_FIELD_SIZE, new_path)
            return None
        field = encoded.ljust(PATH_FIELD_SIZE, b"\x00")
        return raw[:PATH_OFFSET] + field + raw[PATH_END:]

    def get_sample_paths(self, plugin: PluginData) -> list[pathlib.Path]:
        """Extract the sample path from an xfadelooper state."""
        raw = self._state(plugin)
        if raw is None:
            return []
        text = self._read_path(raw)
        if not text:
            return []
        # The plugin writes Windows separators; normalize so this reads anywhere.
        return [pathlib.Path(text.replace("\\", "/"))]

    def set_sample_path(self, plugin: PluginData, old_path: pathlib.Path, new_path: pathlib.Path) -> bool:
        """Replace the sample path, keeping every other byte of the state where it was."""
        raw = self._state(plugin)
        if raw is None:
            return False
        written = self._write_path(raw, str(new_path))
        if written is None:
            return False
        plugin.set_buffer_from_bytes(written)
        return True

    def analyze(self, plugin: PluginData) -> PluginAnalysis:
        """Analyze an xfadelooper state."""
        analysis = PluginAnalysis(
            plugin_name=plugin.plugin_name,
            plugin_path=plugin.path,
            exists=plugin.path.exists() if plugin.path else False,
            unique_id=plugin.unique_id,
            format="VST2",
        )

        sample_paths = self.get_sample_paths(plugin)
        if not sample_paths:
            analysis.metadata["sample_path"] = None
            return analysis

        sample_path = sample_paths[0]
        analysis.metadata["sample_path"] = str(sample_path)
        if sample_path.exists():
            analysis.metadata["sample_exists"] = True
        else:
            analysis.issues.append(f"Missing sample: {sample_path}")
            analysis.can_fix = True
        return analysis

    def fix(self, plugin: PluginData, db: DatabaseT | None = None) -> bool:
        """Relink a missing sample using the sample database."""
        if db is None:
            logger.debug("No database provided for xfadelooper fix")
            return False

        sample_paths = self.get_sample_paths(plugin)
        if not sample_paths:
            return False

        current_path = sample_paths[0]
        if current_path.exists():
            return False

        replacement = select_best_candidate_by_name(
            db,
            current_path.name,
            current_path,
            target_length=None,
            target_size=None,
            target_mtime=None,
        )
        if not replacement:
            logger.debug("No replacement found for %s", current_path.name)
            return False

        if not self.set_sample_path(plugin, current_path, replacement):
            return False

        logger.info("Fixed xfadelooper: %s → %s", current_path.name, replacement)
        return True
