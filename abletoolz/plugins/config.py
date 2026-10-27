"""The user's settings: config.yaml in abletoolz's config folder (see ``Locations.config_file``)."""

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass, field

import yaml

logger = logging.getLogger(__name__)


# Sections an older abletoolz read and this one ignores, said once so a config
# still carrying them is not silently half-used.
RETIRED_SECTIONS = ("plugin_upgrades", "plugin_parsers", "plugin_translation.targets")


@dataclass
class AbletoolzConfig:
    """Config container."""

    sample_paths: list[pathlib.Path] = field(default_factory=list)
    # Extra plugin folders on top of this OS's standard ones: the plugin
    # database scans them and the plugin checks look in them. The sample
    # database takes its folders the same way.
    plugin_paths: list[pathlib.Path] = field(default_factory=list)
    # A probed file of VST3 class ids the plugin database reads beside its own
    # sources; see plugins.identity.uid_sources.read_uid_db.
    plugin_uid_db: pathlib.Path | None = None


def load_config(path: pathlib.Path) -> AbletoolzConfig:
    """Load config from the YAML file at ``path``. Returns empty config if missing/invalid."""
    config = AbletoolzConfig()

    if not path.exists():
        return config

    try:
        logger.info("Loading config from: %s", path)
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        if "sample_database" in raw and "paths" in raw["sample_database"]:
            config.sample_paths = [pathlib.Path(p) for p in raw["sample_database"]["paths"]]

        plugin_database = raw.get("plugin_database") or {}
        if "paths" in plugin_database:
            config.plugin_paths = [pathlib.Path(p) for p in plugin_database["paths"]]
        # uid_db used to live under plugin_translation; that spelling still works.
        uid_db = plugin_database.get("uid_db") or (raw.get("plugin_translation") or {}).get("uid_db")
        if uid_db:
            config.plugin_uid_db = pathlib.Path(uid_db)

        retired = [name for name in RETIRED_SECTIONS if _has(raw, name)]
        if retired:
            logger.warning(
                "config.yaml: %s %s retired and ignored. A plan carries earlier choices forward with --from.",
                ", ".join(retired),
                "is" if len(retired) == 1 else "are",
            )

    except (yaml.YAMLError, OSError, TypeError, AttributeError) as e:
        logger.warning("Failed to load config: %s", e)

    return config


def _has(raw: dict[str, object], dotted: str) -> bool:
    node: object = raw
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True
