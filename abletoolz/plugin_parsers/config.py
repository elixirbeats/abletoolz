"""Abletoolz config - YAML file at %APPDATA%/abletoolz/config.yaml"""

from __future__ import annotations

import logging
import os
import pathlib
import sys
from dataclasses import dataclass, field

import yaml

from abletoolz.plugin_parsers.format_translation import ConfiguredTarget, parse_config_targets

logger = logging.getLogger(__name__)


def get_config_path() -> pathlib.Path:
    """Config file location."""
    if sys.platform == "win32":
        base = pathlib.Path(os.environ.get("APPDATA", ""))
    elif sys.platform == "darwin":
        base = pathlib.Path.home() / "Library" / "Application Support"
    else:
        base = pathlib.Path.home() / ".config"
    return base / "abletoolz" / "config.yaml"


@dataclass
class AbletoolzConfig:
    """Config container."""
    sample_paths: list[pathlib.Path] = field(default_factory=list)
    # Extra folders the plugin database scans, on top of this OS's standard
    # plugin locations. The sample database takes its folders the same way.
    plugin_paths: list[pathlib.Path] = field(default_factory=list)
    plugin_upgrade_rules: dict[str, list[str]] = field(default_factory=dict)
    # Extra format-translation targets, merged over the seed table. Keyed by the
    # name the source format stores; see format_translation.TargetConfig.
    plugin_translation_targets: dict[str, ConfiguredTarget] = field(default_factory=dict)
    # A probed file of VST3 class ids, consulted when a target names a plugin
    # without giving its uid; see plugin_parsers.uid_sources.read_uid_db.
    plugin_translation_uid_db: pathlib.Path | None = None


def load_config() -> AbletoolzConfig:
    """Load config from YAML file. Returns empty config if missing/invalid."""
    config = AbletoolzConfig()
    path = get_config_path()

    if not path.exists():
        return config

    try:
        logger.info("Loading config from: %s", path)
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        if "sample_database" in raw and "paths" in raw["sample_database"]:
            config.sample_paths = [pathlib.Path(p) for p in raw["sample_database"]["paths"]]

        if "plugin_database" in raw and "paths" in raw["plugin_database"]:
            config.plugin_paths = [pathlib.Path(p) for p in raw["plugin_database"]["paths"]]

        if "plugin_upgrades" in raw and "exact_rules" in raw["plugin_upgrades"]:
            config.plugin_upgrade_rules = raw["plugin_upgrades"]["exact_rules"]

        if "plugin_translation" in raw:
            translation = raw["plugin_translation"]
            if "targets" in translation:
                config.plugin_translation_targets = parse_config_targets(translation["targets"])
            if "uid_db" in translation:
                config.plugin_translation_uid_db = pathlib.Path(translation["uid_db"])

    except (yaml.YAMLError, OSError, TypeError) as e:
        logger.warning("Failed to load config: %s", e)

    return config
