"""Plugin identity, document containers and reusable saved-state knowledge.

The package separates three questions used by inspection, editing, version
migration and conversion between plugins:

* **identity** -- which plugin is this, and what is its id in another format?
  ``plugin_db`` writes down what this machine has, ``uid_sources`` finds the
  VST3 class id a translation needs, and ``mapping`` guesses which installed
  plugin an unmapped one probably is.
* **container** -- what XML shape does Ableton write for a plugin device, and
  how does it change? ``container.device`` reads any format's info element
  into one description of the plugin device, writes a change back in place in
  the same format, or rebuilds the element as another format;
  ``conversion`` says what a device becomes -- an edit in place or a rebuild
  as another format or another plugin -- and writes it.
* **state** -- how is a patch represented, and how can its values be read,
  changed or written for a target? The ``state`` package keeps codecs and
  transforms independent of any set. Answers sit on a ladder, cheapest
  first -- verbatim, reframe, vendor-compat, re-encode -- and
  ``state.measured`` records which rung each plugin was measured to sit on and
  on what evidence: a listen or a vendor declaration settles a conversion,
  anything less leaves it labelled an experiment.

Plugin maps collect byte maps, parameter maps and other knowledge about one
plugin/version. Their codecs support inspection and editing independently of a migration.
A transform binds that knowledge to a specific source and target. A set
migration also needs container, identity and host-control translation.

``plan`` lists what every plugin device in some sets can become -- another
format, another file, a recovery route, another plugin, a guess -- and applies
what you pick; ``loadable`` says which devices Live cannot load here, and
``recovery`` holds the separately measured native routes a plan can offer. A
plan describes document actions, not proof that a project loads and sounds
correct.

A plugin whose patch names files on disk says so on its map
(:class:`~abletoolz.plugins.state.plugin_map.FilePaths`), and
:mod:`abletoolz.live_set.plugin_files` checks and relinks those files.
``config`` is the YAML file all of it reads.

Existing set-facing tables use the names stored in devices. Binary identity,
state version and explicit source/target scope establish which knowledge can
be reused; a plugin name alone does not establish state compatibility.

The package re-exports nothing: every name is imported from the module that
owns it, so a reader arriving at a name can tell which question it answers.
"""
