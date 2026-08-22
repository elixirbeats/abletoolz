"""What a set says about its plugins, and what can be done about it.

Three questions about a plugin device, answered by three groups of modules:

* **identity** -- which plugin is this, and what is its id in another format?
  ``base``, ``plugin_db``, ``uid_sources``, ``mapping``.
* **container** -- how is a device of one format rewritten as another?
  ``format_translation`` and ``upgrade_rules``, with ``repair`` as the policy
  that decides when to.
* **state** -- what happens to the saved patch when the container changes? the
  ``state`` package.

Beside them sit the per-plugin parsers in ``parsers``, which read a device's own
buffer to find the samples it points at; ``registry`` dispatches to those.

Only the names below are re-exported, and they are the ones the CLI and the
live_set domains reach for. Everything else is imported from the module that
owns it, so a reader arriving at a name can tell which question it answers.
"""

from abletoolz.plugin_parsers.base import PluginAnalysis, PluginData, PluginKind
from abletoolz.plugin_parsers.config import AbletoolzConfig, load_config
from abletoolz.plugin_parsers.mapping import (
    default_suggestions_path,
    render_targets_yaml,
    survey_machine,
)
from abletoolz.plugin_parsers.registry import analyze_plugin, fix_plugin, get_all_parsers

__all__ = [
    "AbletoolzConfig",
    "PluginAnalysis",
    "PluginData",
    "PluginKind",
    "analyze_plugin",
    "default_suggestions_path",
    "fix_plugin",
    "get_all_parsers",
    "load_config",
    "render_targets_yaml",
    "survey_machine",
]
