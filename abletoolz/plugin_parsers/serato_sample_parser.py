"""Serato Sample plugin parser.

Serato Sample stores sample paths in its Buffer as JSON. This parser can:
- Detect missing sample references
- Fix broken paths using the sample database
"""

from __future__ import annotations

import logging
import pathlib
from typing import TYPE_CHECKING, Any

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


@register_parser
class SeratoSampleParser(SampleContainerParser):
    """Parser for Serato Sample VST plugin.

    Serato Sample stores its state as JSON in the plugin buffer.
    Sample paths are stored at: project.sourceSong.File

    Buffer format: Plain JSON encoded as hex string.
    Easy to work with - just decode hex → parse JSON → modify → re-encode.
    """

    name = "serato_sample"
    description = "Serato Sample - DJ sampling plugin"
    buffer_format = BufferFormat.JSON
    unique_ids = [1399681132]
    name_patterns = ["Serato Sample"]

    def _get_file_from_json(self, obj: dict[str, Any]) -> str | None:
        """Extract source file path from decoded JSON."""
        try:
            value = obj["project"]["sourceSong"]["File"]
        except (KeyError, TypeError):
            return None
        return value if isinstance(value, str) else None

    def _set_file_in_json(self, obj: dict[str, Any], new_path: str) -> bool:
        """Set source file path in JSON."""
        try:
            obj["project"]["sourceSong"]["File"] = new_path
            return True
        except (KeyError, TypeError):
            return False

    def _get_length_from_json(self, obj: dict[str, Any]) -> float | None:
        """Extract audio length in seconds if present."""
        try:
            length = obj["project"]["sourceSong"]["Length"]
            if isinstance(length, (int, float)):
                return float(length)
        except (KeyError, TypeError):
            pass
        return None

    def get_sample_paths(self, plugin: PluginData) -> list[pathlib.Path]:
        """Extract sample paths from Serato Sample buffer."""
        obj = plugin.decode_buffer_json()
        if not obj:
            return []

        file_path = self._get_file_from_json(obj)
        if file_path:
            # Serato uses forward slashes even on Windows
            return [pathlib.Path(file_path.replace("\\", "/"))]
        return []

    def set_sample_path(self, plugin: PluginData, old_path: pathlib.Path, new_path: pathlib.Path) -> bool:
        """Replace sample path in Serato Sample buffer."""
        obj = plugin.decode_buffer_json()
        if not obj:
            return False

        # Use forward slashes as Serato does
        if self._set_file_in_json(obj, new_path.as_posix()):
            plugin.set_buffer_from_json(obj)
            return True
        return False

    def analyze(self, plugin: PluginData) -> PluginAnalysis:
        """Analyze Serato Sample plugin state."""
        analysis = PluginAnalysis(
            plugin_name=plugin.plugin_name,
            plugin_path=plugin.path,
            exists=plugin.path.exists() if plugin.path else False,
            unique_id=plugin.unique_id,
            format="VST2",  # Serato Sample is VST2
        )

        sample_paths = self.get_sample_paths(plugin)
        if sample_paths:
            sample_path = sample_paths[0]
            analysis.metadata["sample_path"] = str(sample_path)

            if not sample_path.exists():
                analysis.issues.append(f"Missing sample: {sample_path}")
                analysis.can_fix = True
            else:
                analysis.metadata["sample_exists"] = True
        else:
            # No sample loaded - not an issue, just empty
            analysis.metadata["sample_path"] = None

        # Get length if available
        obj = plugin.decode_buffer_json()
        if obj:
            length = self._get_length_from_json(obj)
            if length:
                analysis.metadata["sample_length"] = length

        return analysis

    def fix(self, plugin: PluginData, db: DatabaseT | None = None) -> bool:
        """Fix missing sample path using database lookup."""
        if db is None:
            logger.debug("No database provided for Serato Sample fix")
            return False

        obj = plugin.decode_buffer_json()
        if not obj:
            return False

        current = self._get_file_from_json(obj)
        if not current:
            return False

        # Parse current path. pathlib.Path() only raises on embedded NUL bytes.
        try:
            current_path = pathlib.Path(current.replace("\\", "/"))
        except ValueError:
            return False

        # Check if already valid
        if current_path.exists():
            return False

        file_name = current_path.name
        target_length = self._get_length_from_json(obj)

        # Find replacement in database
        replacement = select_best_candidate_by_name(
            db,
            file_name,
            current_path,
            target_length=target_length,
            target_size=None,
            target_mtime=None,
        )

        if not replacement:
            logger.debug("No replacement found for %s", file_name)
            return False

        # Update JSON with forward slashes (Serato format)
        if not self._set_file_in_json(obj, replacement.as_posix()):
            return False

        plugin.set_buffer_from_json(obj)
        logger.info("Fixed Serato Sample: %s → %s", file_name, replacement)
        return True
