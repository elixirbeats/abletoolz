"""Plugin parsers for abletoolz.

This package provides:
- Base classes for creating plugin-specific parsers
- Configuration management for parser settings
- Registry for automatic parser discovery and dispatch
- Built-in parsers for common plugins (Serato Sample, etc.)

Usage:
    from abletoolz.plugin_parsers import (
        PluginData,
        PluginParser,
        analyze_plugin,
        fix_plugin,
        load_config,
    )

    # Parse plugin from XML element
    plugin = PluginData.from_element(vst_element)

    # Analyze with auto-detected parser
    analysis = analyze_plugin(plugin)

    # Fix using sample database
    if analysis and analysis.can_fix:
        fix_plugin(plugin, db=sample_db)
"""

from abletoolz.plugin_parsers.base import (
    BufferFormat,
    PluginAnalysis,
    PluginData,
    PluginParser,
    SampleContainerParser,
)
from abletoolz.plugin_parsers.config import (
    AbletoolzConfig,
    get_config_path,
    load_config,
)
from abletoolz.plugin_parsers.registry import (
    analyze_plugin,
    fix_plugin,
    get_all_parsers,
    get_parser_for_plugin,
    register_parser,
    upgrade_plugin,
)

__all__ = [
    # Base classes
    "BufferFormat",
    "PluginData",
    "PluginParser",
    "PluginAnalysis",
    "SampleContainerParser",
    # Config
    "AbletoolzConfig",
    "load_config",
    "get_config_path",
    # Registry
    "register_parser",
    "get_parser_for_plugin",
    "get_all_parsers",
    "analyze_plugin",
    "fix_plugin",
    "upgrade_plugin",
]
