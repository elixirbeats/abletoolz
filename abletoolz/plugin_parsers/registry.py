"""Plugin parser registry for automatic discovery and dispatch."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from abletoolz.plugin_parsers.base import PluginAnalysis, PluginData, PluginParser

if TYPE_CHECKING:
    from abletoolz.plugin_parsers.config import AbletoolzConfig
    from abletoolz.sample_databaser.create_db import DatabaseT

logger = logging.getLogger(__name__)

# Global registry of parser classes
_PARSER_REGISTRY: dict[str, type[PluginParser]] = {}


def register_parser(parser_class: type[PluginParser]) -> type[PluginParser]:
    """Decorator to register a plugin parser class."""
    _PARSER_REGISTRY[parser_class.name] = parser_class
    logger.debug("Registered plugin parser: %s", parser_class.name)
    return parser_class


def get_parser_for_plugin(plugin: PluginData, config: AbletoolzConfig | None = None) -> PluginParser | None:
    """Find and instantiate a parser that can handle this plugin.

    Args:
        plugin: Plugin data to find parser for
        config: Optional config to check if parser is enabled

    Returns:
        Instantiated parser or None if no match
    """
    for _name, parser_class in _PARSER_REGISTRY.items():
        if parser_class.can_handle(plugin):
            return parser_class()

    return None


def get_all_parsers() -> dict[str, type[PluginParser]]:
    """Get all registered parser classes."""
    return _PARSER_REGISTRY.copy()


def analyze_plugin(
    plugin: PluginData,
    config: AbletoolzConfig | None = None
) -> PluginAnalysis | None:
    """Analyze a plugin using appropriate parser.

    Returns:
        PluginAnalysis if a parser handled it, None otherwise
    """
    parser = get_parser_for_plugin(plugin, config)
    if parser is None:
        return None
    return parser.analyze(plugin)


def fix_plugin(
    plugin: PluginData,
    db: DatabaseT | None = None,
    config: AbletoolzConfig | None = None,
) -> bool:
    """Attempt to fix a plugin using appropriate parser.

    Returns:
        True if changes were made
    """
    parser = get_parser_for_plugin(plugin, config)
    if parser is None:
        return False
    return parser.fix(plugin, db)


def upgrade_plugin(
    plugin: PluginData,
    installed_plugins: list[dict[str, str]],
    config: AbletoolzConfig | None = None,
) -> bool:
    """Attempt to upgrade a plugin path using appropriate parser.

    Returns:
        True if path was upgraded
    """
    parser = get_parser_for_plugin(plugin, config)
    if parser is None:
        return False
    return parser.upgrade(plugin, installed_plugins)


# Auto-import parsers to trigger registration
def _load_builtin_parsers() -> None:
    """Import all builtin parser modules to register them."""
    try:
        from abletoolz.plugin_parsers import serato_sample_parser  # noqa: F401
    except ImportError as e:
        logger.debug("Could not import serato_sample_parser: %s", e)

    # Add more parser imports here as they're created
    # from abletoolz.plugin_parsers import kontakt_parser
    # from abletoolz.plugin_parsers import battery_parser


# Load parsers on module import
_load_builtin_parsers()
