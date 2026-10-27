"""Plan what every plugin device in a set becomes, then apply the plan.

A plan is a file you read and edit. It lists every plugin found in the sets it
covers, one entry per plugin, with every target that plugin can become here,
what proves each and whether it is installed on this machine:

* the same plugin in another format, its patch carried by the plugin's map;
* the same plugin in another installed file (same VST2 UniqueId, e.g. its
  64-bit build), the patch kept as it is;
* a verified native recovery route, for the saved patches it was measured on;
* another plugin, through a converter someone worked out;
* an installed plugin with a similar name and no other vendor, for a plugin
  abletoolz has not mapped: an experiment that copies the patch as it is,
  never picked for you.

``pick`` is the target a plugin's devices become, or ``keep``; an override
gives one device a different pick. A new plan starts from a policy
(:class:`Pick`): only plugins this machine cannot load get a target, nothing
does, or every plugin gets its best target. Best means the same plugin before
another plugin, then stronger evidence, then the format order the plan was made
with.

Applying reads each set again, skips one that changed since it was planned,
rewrites every device whose pick is not ``keep`` and leaves a device that
cannot take its pick exactly as it was, saying why.

On this computer, :func:`plan_sets`, :func:`write_plan`, :func:`read_plan` and
:func:`apply_in_place` are ``abletoolz plugins plan`` and ``apply``. A plan is
the plan file as an object: the same entries with the same fields.
"""

from abletoolz.plugins.plan.apply import SetOutcome, apply_plan
from abletoolz.plugins.plan.carry import carry
from abletoolz.plugins.plan.evidence import known_state
from abletoolz.plugins.plan.here import apply_in_place, plan_sets, read_plan, this_machine, write_plan
from abletoolz.plugins.plan.machine import InstalledVst2, Machine
from abletoolz.plugins.plan.make import make_plan, seed_from_plan
from abletoolz.plugins.plan.model import KEEP, Override, Pick, Plan, PluginChoice, Route, Target, dump_plan, load_plan

__all__ = [
    "KEEP",
    "InstalledVst2",
    "Machine",
    "Override",
    "Pick",
    "Plan",
    "PluginChoice",
    "Route",
    "SetOutcome",
    "Target",
    "apply_in_place",
    "apply_plan",
    "carry",
    "dump_plan",
    "known_state",
    "load_plan",
    "make_plan",
    "plan_sets",
    "read_plan",
    "seed_from_plan",
    "this_machine",
    "write_plan",
]
