"""Base classes and protocols for plugin parsers.

Each plugin parser handles a specific plugin or family of plugins,
providing functionality like:
- Detecting if a PluginDesc element matches
- Fixing broken sample paths within plugin state
- Upgrading plugin paths (32→64 bit, VST2→VST3, etc.)
- Extracting metadata for analysis
"""

from __future__ import annotations

import base64
import binascii
import enum
import json
import logging
import pathlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from xml.etree import ElementTree as ET

from pydantic import BaseModel, ConfigDict

from abletoolz import decode_encode
from abletoolz.misc import search_element

if TYPE_CHECKING:
    from abletoolz.sample_databaser.create_db import DatabaseT

logger = logging.getLogger(__name__)


class PluginKind(enum.StrEnum):
    """A plugin format, as a set stores it.

    Lives here rather than beside the set-scanning code because format is what
    every parser and every format translation is keyed on.
    """

    VST = "vst"
    VST3 = "vst3"
    AU = "au"


class BufferFormat(enum.Enum):
    """Describes the format of plugin state data in the Buffer element.

    This helps developers understand what they're working with when
    creating new plugin parsers.
    """

    # Easy formats
    JSON = "json"  # Plain JSON encoded as hex (e.g., Serato Sample)
    XML = "xml"  # XML encoded as hex
    BASE64_JSON = "base64_json"  # Base64-wrapped JSON

    # Standard binary formats
    CHUNK = "chunk"  # VST chunk format (FXP/FXB style)
    MIDI = "midi"  # MIDI-like data

    # Hard formats - custom binary
    BINARY_STRUCT = "binary_struct"  # C struct / packed binary
    PROPRIETARY = "proprietary"  # Completely custom format

    # Unknown
    UNKNOWN = "unknown"


@dataclass
class PluginAnalysis:
    """Result of analyzing a plugin instance."""

    plugin_name: str
    plugin_path: pathlib.Path | None
    exists: bool
    unique_id: int | None = None
    format: str | None = None  # "VST2", "VST3", "AU"
    arch: str | None = None  # "x64", "x86"

    # Parser-specific findings
    issues: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    # Suggested fixes
    suggested_path: pathlib.Path | None = None
    can_fix: bool = False


class PluginData(BaseModel):
    """Unified plugin data parsed from PluginDesc XML element.

    Handles both old format (Dir/Data hex-encoded path + FileName)
    and new format (Path with full path string).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Core identifiers
    plugin_name: str
    file_name: str | None = None
    unique_id: int | None = None

    # Path info (new format has Path element, old has Dir/Data)
    path: pathlib.Path | None = None
    data: str | None = None  # Hex-encoded path for old format

    # Buffer contains plugin state (presets, sample refs, etc.)
    buffer_element: ET.Element | None = None
    path_element: ET.Element | None = None

    # Reference to original XML element for mutations
    _root_element: ET.Element | None = None

    @classmethod
    def from_element(cls, root: ET.Element) -> PluginData:
        """Parse PluginData from VstPluginInfo or AuPluginInfo element."""
        # Try new format first (Path element with Value attribute)
        path_element = root.find(".//Path")
        path_str = path_element.get("Value") if path_element is not None else None
        path = pathlib.Path(path_str) if path_str else None

        # Old format: Dir/Data with hex-encoded path
        dir_element = root.find(".//Dir")
        data = dir_element.findtext("Data") if dir_element is not None else None

        # Get names
        file_name = search_element(root, "FileName", "Value")
        plugin_name = search_element(root, "PlugName", "Value") or file_name or "<unknown>"

        # Unique ID
        unique_id_str = search_element(root, "UniqueId", "Value")
        unique_id = int(unique_id_str) if unique_id_str and unique_id_str.isdigit() else None

        # Buffer element (contains plugin state)
        buffer_element = search_element(root, "Buffer")

        return cls(
            plugin_name=plugin_name,
            file_name=file_name,
            unique_id=unique_id,
            path=path,
            path_element=path_element,
            data=data,
            buffer_element=buffer_element,
            _root_element=root,
        )

    def decode_buffer(self) -> str:
        """Decode Buffer hex to raw string (often JSON for modern plugins)."""
        if self.buffer_element is None or self.buffer_element.text is None:
            return ""
        hex_str, _levels = decode_encode.xml_to_string(self.buffer_element.text)
        raw_bytes = decode_encode.hex_to_string(hex_str, return_bytes=True)
        decoded = raw_bytes.decode("utf-8", errors="ignore")
        # Extract JSON payload if present
        start = decoded.find("{")
        end = decoded.rfind("}")
        return decoded[start : end + 1] if start != -1 and end != -1 else decoded

    def decode_buffer_json(self) -> dict[str, Any] | None:
        """Parse Buffer as JSON dict if possible."""
        try:
            s = self.decode_buffer()
            return json.loads(s) if s else None
        except json.JSONDecodeError:
            return None

    def get_buffer_raw_hex(self) -> str:
        """Get the raw hex string from Buffer (stripped of whitespace)."""
        if self.buffer_element is None or self.buffer_element.text is None:
            return ""
        hex_str, _levels = decode_encode.xml_to_string(self.buffer_element.text)
        return hex_str

    def get_buffer_raw_bytes(self) -> bytes:
        """Get the raw bytes from Buffer (decoded from hex)."""
        hex_str = self.get_buffer_raw_hex()
        if not hex_str:
            return b""
        return bytes.fromhex(hex_str)

    def detect_buffer_format(self) -> BufferFormat:
        """Attempt to auto-detect the buffer format.

        Returns best guess of BufferFormat based on content analysis.
        """
        raw_bytes = self.get_buffer_raw_bytes()
        if not raw_bytes:
            return BufferFormat.UNKNOWN

        # Try JSON first (most common for modern plugins)
        try:
            decoded = raw_bytes.decode("utf-8", errors="strict")
            # Look for JSON object
            start = decoded.find("{")
            end = decoded.rfind("}")
            if start != -1 and end > start:
                json.loads(decoded[start : end + 1])
                return BufferFormat.JSON
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass

        # Try XML
        try:
            decoded = raw_bytes.decode("utf-8", errors="strict")
            if decoded.strip().startswith("<?xml") or decoded.strip().startswith("<"):
                return BufferFormat.XML
        except UnicodeDecodeError:
            pass

        # Check for VST chunk magic bytes (common patterns)
        if raw_bytes[:4] in (b"CcnK", b"FBCh", b"FPCh", b"FxCk"):
            return BufferFormat.CHUNK

        # Check for base64 (mostly alphanumeric with +/=)
        try:
            decoded = raw_bytes.decode("ascii", errors="strict")
            base64.b64decode(decoded)
            # If it decodes, check if result is JSON
            try:
                inner = base64.b64decode(decoded).decode("utf-8")
                if inner.strip().startswith("{"):
                    return BufferFormat.BASE64_JSON
            except (binascii.Error, UnicodeDecodeError):
                pass
        except (UnicodeDecodeError, binascii.Error):
            pass

        # Likely binary/proprietary
        # Check if mostly printable vs binary
        printable = sum(1 for b in raw_bytes if 32 <= b <= 126)
        if printable / len(raw_bytes) > 0.8:
            return BufferFormat.PROPRIETARY  # Text-ish but unknown format

        return BufferFormat.BINARY_STRUCT

    def dump_buffer(self, max_hex_bytes: int = 256, max_decoded_chars: int = 500) -> str:
        """Create a human-readable dump of the buffer for analysis.

        Useful for reverse engineering new plugin formats.
        """
        lines = []
        lines.append(f"Plugin: {self.plugin_name}")
        lines.append(f"Unique ID: {self.unique_id}")
        lines.append(f"Path: {self.path}")

        raw_bytes = self.get_buffer_raw_bytes()
        if not raw_bytes:
            lines.append("Buffer: <empty>")
            return "\n".join(lines)

        detected = self.detect_buffer_format()
        lines.append(f"Buffer size: {len(raw_bytes)} bytes")
        lines.append(f"Detected format: {detected.value}")

        # Hex dump (first N bytes)
        hex_preview = raw_bytes[:max_hex_bytes].hex(" ", 1).upper()
        if len(raw_bytes) > max_hex_bytes:
            hex_preview += f" ... ({len(raw_bytes) - max_hex_bytes} more bytes)"
        lines.append(f"Hex preview:\n  {hex_preview}")

        # Decoded preview. errors="replace" means this can't actually raise.
        decoded = raw_bytes.decode("utf-8", errors="replace")[:max_decoded_chars]
        if len(raw_bytes) > max_decoded_chars:
            decoded += "..."
        # Clean up non-printable chars for display
        cleaned = "".join(c if c.isprintable() or c in "\n\t" else "·" for c in decoded)
        lines.append(f"UTF-8 preview:\n  {cleaned[:200]}")

        # If JSON, pretty print structure
        if detected == BufferFormat.JSON:
            obj = self.decode_buffer_json()
            if obj:
                # Show top-level keys
                if isinstance(obj, dict):
                    lines.append(f"JSON keys: {list(obj.keys())}")

        return "\n".join(lines)

    def set_buffer_from_bytes(self, data: bytes) -> None:
        """Encode raw bytes back into Buffer as hex with original indentation.

        Binary plugin states go through here rather than through
        :meth:`set_buffer_from_string`, which would mangle any byte that is not
        valid UTF-8 on the way out.
        """
        if self.buffer_element is None:
            return
        existing_text = self.buffer_element.text or ""
        _hex_existing, levels = decode_encode.xml_to_string(existing_text) if existing_text.strip() else ("", 14)
        self.buffer_element.text = decode_encode.string_to_xml(data.hex().upper(), levels=levels)

    def set_buffer_from_string(self, data: str) -> None:
        """Encode string back into Buffer as hex with original indentation."""
        self.set_buffer_from_bytes(data.encode("utf-8"))

    def set_buffer_from_json(self, obj: dict[str, Any]) -> None:
        """Dump JSON dict and set Buffer text."""
        data = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
        self.set_buffer_from_string(data)

    def set_path(self, new_path: pathlib.Path) -> bool:
        """Update plugin path in XML (new format only)."""
        if self.path_element is not None:
            self.path_element.set("Value", str(new_path))
            self.path = new_path
            return True
        return False


class PluginParser(ABC):
    """Abstract base class for plugin-specific parsers.

    Subclass this to handle specific plugins (Serato Sample, Kontakt, etc.)
    """

    # Override these in subclasses
    name: str = "BaseParser"
    description: str = "Base plugin parser"

    # What format is the Buffer data in? Helps developers understand complexity
    buffer_format: BufferFormat = BufferFormat.UNKNOWN

    # Matching criteria - plugin matches if ANY of these match
    unique_ids: list[int] = []
    name_patterns: list[str] = []  # Substrings to match in plugin_name

    @classmethod
    def can_handle(cls, plugin: PluginData) -> bool:
        """Check if this parser can handle the given plugin.

        Override for custom matching logic.
        """
        # Match by unique_id
        if plugin.unique_id and plugin.unique_id in cls.unique_ids:
            return True

        # Match by name pattern
        plugin_name_lower = plugin.plugin_name.lower()
        for pattern in cls.name_patterns:
            if pattern.lower() in plugin_name_lower:
                return True

        return False

    @abstractmethod
    def analyze(self, plugin: PluginData) -> PluginAnalysis:
        """Analyze plugin state and return findings.

        Should detect issues like missing samples, outdated paths, etc.
        """
        ...

    def fix(self, plugin: PluginData, db: DatabaseT | None = None) -> bool:
        """Attempt to fix detected issues.

        Args:
            plugin: Plugin data to fix
            db: Sample database for path lookups

        Returns:
            True if any changes were made
        """
        return False

    def upgrade(self, plugin: PluginData, installed_plugins: list[dict[str, str]]) -> bool:
        """Attempt to upgrade plugin path (e.g., 32→64 bit).

        Args:
            plugin: Plugin data to upgrade
            installed_plugins: List of installed plugins from scanner

        Returns:
            True if path was upgraded
        """
        return False


class SampleContainerParser(PluginParser):
    """Base class for plugins that contain sample references.

    Examples: Serato Sample, Kontakt, Battery, etc.
    """

    @abstractmethod
    def get_sample_paths(self, plugin: PluginData) -> list[pathlib.Path]:
        """Extract sample paths referenced by this plugin."""
        ...

    @abstractmethod
    def set_sample_path(self, plugin: PluginData, old_path: pathlib.Path, new_path: pathlib.Path) -> bool:
        """Replace a sample path in the plugin state."""
        ...
