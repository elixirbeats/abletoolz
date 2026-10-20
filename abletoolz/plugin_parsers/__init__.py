"""What a set says about its plugins, and what can be done about it.

A plugin device in a set can be broken in three independent ways, and the
package is split along those three questions:

* **identity** -- which plugin is this, and what is its id in another format?
  ``plugin_db`` writes down what this machine has, ``binaries`` reads
  a dll or a bundle without loading it, ``uid_sources`` finds the VST3 class id
  a translation needs, and ``mapping`` drafts the user's what-becomes-what
  table.
* **container** -- how is a device of one format rewritten as another?
  ``format_translation`` rewrites the format-specific info element in place;
  ``upgrade_rules`` is the older path, swapping the plugin file a device points
  at by explicit filename rule.
* **state** -- what happens to the saved patch when the container changes? The
  hard axis, and the ``state`` package. Answers sit on a ladder, cheapest
  first -- verbatim, reframe, vendor-compat, re-encode -- and
  ``state.measured`` records which rung each plugin was measured to sit on and
  on what evidence: a listen or a vendor declaration settles a conversion,
  anything less leaves it labelled an experiment.

Everything above is knowledge. ``repair`` is the one module that decides
anything: it converts only devices Live cannot load today, only where the
user's mapping says what the replacement is, and reports everything else
untouched.

Beside the axes sit the per-plugin parsers in ``parsers``, which read a
device's own buffer to find and fix the samples it points at; ``registry``
dispatches to them, ``base`` is their contract, and ``config`` is the YAML
file all of it reads.

Tables here key plugins by the name a set stores -- the VST2 spelling where the
two formats differ ("FabFilter Pro-Q 3"), the shared name where they do not --
because every lookup starts from a device found in a set. The format axis is
implied: one translation direction exists today (VST2 to VST3), so naming the
plugin names the conversion.

Only the names below are re-exported, and they are the ones the CLI and the
live_set domains reach for. Everything else is imported from the module that
owns it, so a reader arriving at a name can tell which question it answers.
"""

from abletoolz.plugin_parsers.config import AbletoolzConfig, load_config
from abletoolz.plugin_parsers.identity.mapping import (
    default_suggestions_path,
    render_targets_yaml,
    survey_machine,
)
from abletoolz.plugin_parsers.parsers.base import PluginAnalysis, PluginData, PluginKind
from abletoolz.plugin_parsers.parsers.registry import analyze_plugin, fix_plugin, get_all_parsers

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
