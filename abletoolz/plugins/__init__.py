"""Plugin identity, document containers and reusable saved-state knowledge.

The package separates three questions used by inspection, editing, version
migration and conversion between plugins:

* **identity** -- which plugin is this, and what is its id in another format?
  ``plugin_db`` writes down what this machine has, ``binaries`` reads
  a dll or a bundle without loading it, ``uid_sources`` finds the VST3 class id
  a translation needs, and ``mapping`` drafts the user's what-becomes-what
  table.
* **container** -- what XML shape does Ableton write for a plugin device, and
  how does it change? ``container.device`` reads any format's info element
  into one description of the plugin device, writes a change back in place in
  the same format, or rebuilds the element as another format;
  ``format_translation`` is the recipe that says what a device becomes and
  the one step that applies it; ``upgrade_rules`` swaps the plugin file a
  device points at by explicit filename rule.
* **state** -- how is a patch represented, and how can its values be read,
  changed or written for a target? The ``state`` package keeps codecs and
  transforms independent of a set repair. Answers sit on a ladder, cheapest
  first -- verbatim, reframe, vendor-compat, re-encode -- and
  ``state.measured`` records which rung each plugin was measured to sit on and
  on what evidence: a listen or a vendor declaration settles a conversion,
  anything less leaves it labelled an experiment.

Binary structure maps, control maps and codecs are useful without a migration.
A transform binds that knowledge to a specific source and target. A set
migration also needs container, identity and host-control translation. Repair
is one use of those capabilities, rather than a label for all of them.

``repair`` selects replacements for devices Live cannot load using the user's
mapping. ``recovery`` applies separately measured reference or prepared-state
routes to copies. Their plans describe document actions, not the full body of
plugin knowledge or proof that a project loads and sounds correct.

Beside the axes sit the per-plugin parsers in ``parsers``, which read a
device's own buffer to find and fix the samples it points at; ``registry``
dispatches to them, ``base`` is their contract, and ``config`` is the YAML
file all of it reads.

Existing set-facing tables use the names stored in devices. Binary identity,
state version and explicit source/target scope establish which knowledge can
be reused; a plugin name alone does not establish state compatibility.

Only the names below are re-exported, and they are the ones the CLI and the
live_set domains reach for. Everything else is imported from the module that
owns it, so a reader arriving at a name can tell which question it answers.
"""

from abletoolz.plugins.config import AbletoolzConfig, load_config
from abletoolz.plugins.container.device import PluginKind
from abletoolz.plugins.identity.mapping import (
    default_suggestions_path,
    render_targets_yaml,
    survey_machine,
)
from abletoolz.plugins.parsers.base import PluginAnalysis, PluginData
from abletoolz.plugins.parsers.registry import analyze_plugin, fix_plugin, get_all_parsers

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
