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
    PluginKind,
    PluginParser,
    SampleContainerParser,
)
from abletoolz.plugin_parsers.config import (
    AbletoolzConfig,
    get_config_path,
    load_config,
)
from abletoolz.plugin_parsers.format_translation import (
    KNOWN_TRANSLATIONS,
    ConfiguredTarget,
    IncompleteDevice,
    NamedTarget,
    PluginIdentity,
    TranslationReport,
    TranslationTarget,
    device_infos,
    harvest_moduleinfo_uids,
    harvest_set_uids,
    has_translator,
    is_translatable,
    resolve_target,
    translate_device,
    translate_set,
)
from abletoolz.plugin_parsers.mapping import (
    MappingSuggestion,
    MatchTier,
    SuggestionReport,
    VendorAgreement,
    default_suggestions_path,
    render_targets_yaml,
    suggest_mappings,
    survey_machine,
)
from abletoolz.plugin_parsers.plugin_db import (
    PluginDatabase,
    PluginEntry,
    PluginSource,
    build_plugin_db,
    load_plugin_db,
    read_plugin_db,
    write_plugin_db,
)
from abletoolz.plugin_parsers.registry import (
    analyze_plugin,
    fix_plugin,
    get_all_parsers,
    get_parser_for_plugin,
    register_parser,
    upgrade_plugin,
)
from abletoolz.plugin_parsers.repair import (
    DeviceRepair,
    RepairReport,
    RepairStatus,
    default_oracle,
    repair_set,
)
from abletoolz.plugin_parsers.state import (
    MEASURED_STATE,
    NO_CONTROLLER_STATE,
    UNMEASURED,
    ConstantControllerState,
    ControllerState,
    CustomState,
    MeasuredState,
    NoControllerState,
    StateEvidence,
    StatePolicy,
    StateRung,
    StateTransform,
    StateTransformError,
    measured_state,
    register_custom_state,
)
from abletoolz.plugin_parsers.state.derived import (
    DerivedParameter,
    DerivedTable,
    FabfState,
    read_derived_table,
)
from abletoolz.plugin_parsers.state.fabfilter import (
    PRO_C_2,
    PRO_Q1_TO_PRO_Q3,
    EditorState,
    FfbsControllerState,
    FfbsState,
    pro_q1_to_pro_q3,
    pro_q1_to_pro_q3_parameters,
)
from abletoolz.plugin_parsers.state.fxbk import LegacyBank
from abletoolz.plugin_parsers.uid_sources import (
    UidLookup,
    harvest_live_database_uids,
    read_uid_db,
)

__all__ = [
    # Base classes
    "BufferFormat",
    "PluginData",
    "PluginKind",
    "PluginParser",
    "PluginAnalysis",
    "SampleContainerParser",
    # Config
    "AbletoolzConfig",
    "load_config",
    "get_config_path",
    # Format translation
    "KNOWN_TRANSLATIONS",
    "ConfiguredTarget",
    "IncompleteDevice",
    "NamedTarget",
    "PluginIdentity",
    "TranslationReport",
    "TranslationTarget",
    "device_infos",
    "harvest_moduleinfo_uids",
    "harvest_set_uids",
    "has_translator",
    "is_translatable",
    "resolve_target",
    "translate_device",
    "translate_set",
    # The local plugin database
    "PluginDatabase",
    "PluginEntry",
    "PluginSource",
    "build_plugin_db",
    "load_plugin_db",
    "read_plugin_db",
    "write_plugin_db",
    # Suggesting mappings
    "MappingSuggestion",
    "MatchTier",
    "SuggestionReport",
    "VendorAgreement",
    "default_suggestions_path",
    "render_targets_yaml",
    "suggest_mappings",
    "survey_machine",
    # Repair
    "DeviceRepair",
    "RepairReport",
    "RepairStatus",
    "default_oracle",
    "repair_set",
    # What happens to the patch
    "MEASURED_STATE",
    "NO_CONTROLLER_STATE",
    "UNMEASURED",
    "ConstantControllerState",
    "ControllerState",
    "CustomState",
    "MeasuredState",
    "NoControllerState",
    "StateEvidence",
    "StatePolicy",
    "StateRung",
    "StateTransform",
    "StateTransformError",
    "measured_state",
    "register_custom_state",
    # The FabFilter re-encodes
    "PRO_C_2",
    "PRO_Q1_TO_PRO_Q3",
    "DerivedParameter",
    "DerivedTable",
    "EditorState",
    "FabfState",
    "FfbsControllerState",
    "FfbsState",
    "LegacyBank",
    "pro_q1_to_pro_q3",
    "pro_q1_to_pro_q3_parameters",
    "read_derived_table",
    # Class id sources
    "UidLookup",
    "harvest_live_database_uids",
    "read_uid_db",
    # Registry
    "register_parser",
    "get_parser_for_plugin",
    "get_all_parsers",
    "analyze_plugin",
    "fix_plugin",
    "upgrade_plugin",
]
