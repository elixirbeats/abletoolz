"""Plan what every plugin device in a set becomes, then apply the plan.

A plan file lists every plugin found in the sets it covers, one entry per plugin
and format, with three fields: ``plugin``, ``format`` and ``convert_to``. Above
each entry a comment lists the names ``convert_to`` takes: the plugin's own
other formats and the plugins its converters reach, each by the ``plan_name``
written by hand in its map, and a verified native recovery route by its id.
``null`` leaves a plugin as it is; a name converts every device of it.

Applying reads each set again, so the file holds no device numbers or hashes,
converts every device whose plugin names a target, and leaves a device that
cannot take it exactly as it was, saying why.

On this computer, :func:`plan_sets`, :func:`write_plan`, :func:`read_plan` and
:func:`apply_in_place` are ``abletoolz plugins plan`` and ``apply``. In Python
a :class:`Plan` holds more than the file: every device, every target with its
evidence, and per-device overrides. :func:`file_targets` (another installed
file of the same VST2) and :func:`experiments` (an installed plugin named like
an unmapped one) add targets the file never offers.
"""

from abletoolz.plugins.plan.apply import SetOutcome, apply_plan
from abletoolz.plugins.plan.carry import carry
from abletoolz.plugins.plan.evidence import known_state
from abletoolz.plugins.plan.here import apply_in_place, plan_sets, read_plan, this_machine, write_plan
from abletoolz.plugins.plan.machine import InstalledVst2, Machine
from abletoolz.plugins.plan.make import make_plan
from abletoolz.plugins.plan.model import (
    KEEP,
    Override,
    Plan,
    PlanFile,
    PluginChoice,
    Route,
    Target,
    dump_plan,
    load_plan,
)
from abletoolz.plugins.plan.targets import experiments, file_targets

__all__ = [
    "KEEP",
    "InstalledVst2",
    "Machine",
    "Override",
    "Plan",
    "PlanFile",
    "PluginChoice",
    "Route",
    "SetOutcome",
    "Target",
    "apply_in_place",
    "apply_plan",
    "carry",
    "dump_plan",
    "experiments",
    "file_targets",
    "known_state",
    "load_plan",
    "make_plan",
    "plan_sets",
    "read_plan",
    "this_machine",
    "write_plan",
]
