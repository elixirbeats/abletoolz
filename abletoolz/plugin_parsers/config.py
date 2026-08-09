"""Abletoolz config - YAML file at %APPDATA%/abletoolz/config.yaml"""

from __future__ import annotations

import logging
import os
import pathlib
import sys
from dataclasses import dataclass, field

import yaml

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
    plugin_upgrade_rules: dict[str, list[str]] = field(default_factory=dict)


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

        if "plugin_upgrades" in raw and "exact_rules" in raw["plugin_upgrades"]:
            config.plugin_upgrade_rules = raw["plugin_upgrades"]["exact_rules"]

    except (yaml.YAMLError, OSError, TypeError) as e:
        logger.warning("Failed to load config: %s", e)

    return config
