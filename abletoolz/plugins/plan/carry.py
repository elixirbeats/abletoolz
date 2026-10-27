"""What one device becomes as a target: why it cannot, or what to write.

A plan asks :func:`carry` about every device and target when it is made, to
count each target's refusals, and again when it is applied, where the answer is
what gets written. Working it out changes nothing: the container, the
document's schema, controls in use and the patch itself are all checked, the
patch carried across in memory.
"""

from __future__ import annotations

import dataclasses

from abletoolz.plugins.container.device import PluginDevice
from abletoolz.plugins.container.rewritable import refusal
from abletoolz.plugins.conversion import Answer, Edit, Rebuild, as_format, as_plugin
from abletoolz.plugins.identity.ids import PluginKind
from abletoolz.plugins.plan.evidence import _converter, _map_by_id
from abletoolz.plugins.plan.machine import Machine
from abletoolz.plugins.plan.model import PluginChoice, Route, Target
from abletoolz.plugins.recovery import recover


def carry(device: PluginDevice, choice: PluginChoice, target: Target, machine: Machine) -> Answer:
    """What ``device``, one of ``choice``'s, becomes as ``target`` on ``machine``: a refusal, or what to write.

    ``machine`` holds the recovery routes a recovery target names, and reads
    this machine's VST3 parameters for a device carrying Live's own list.
    """
    match target.route:
        case Route.RECOVERY:
            found = next((item for item in machine.recovery if item.route.id == target.route_id), None)
            return "no verified recovery route for this device" if found is None else recover(device, found)
        case Route.FILE | Route.EXPERIMENT if target.format is PluginKind.VST:
            if device.kind is not PluginKind.VST:
                return "only a VST2 device points at a plugin file"
            assert target.path is not None
            if target.route is Route.FILE:
                return Edit(target.path, target.plugin)  # the same plugin in another file numbers its parameters alike
            reason = refusal(device, PluginKind.VST, can_rebuild_live_list=False)
            return reason if reason is not None else Edit(target.path, target.plugin, unique_id=target.unique_id)
        case Route.EXPERIMENT:
            # A guess names a VST3 by its class id and copies the patch as it is.
            assert target.uid_fields is not None
            reason = refusal(device, target.format, can_rebuild_live_list=False)
            if reason is not None:
                return reason
            identity = dataclasses.replace(
                device.identity, kind=target.format, name=target.plugin, uid_fields=target.uid_fields
            )
            return Rebuild(target.format, identity, device.saved_state)
    # The same plugin in another format, or another plugin through a converter: plugin maps write it.
    assert choice.map_id is not None and target.map_id is not None
    reason = refusal(device, target.format, can_rebuild_live_list=True)
    if reason is not None:
        return reason
    try:
        source, written = _map_by_id(choice.map_id), _map_by_id(target.map_id)
        if target.route is Route.FORMAT:
            return as_format(device, source, target.format, parameters=machine.parameters)
        converter = _converter(source.id, written.id)
        return as_plugin(device, converter, source, written, target.format, parameters=machine.parameters)
    except ValueError as error:  # StateTransformError, or a device the conversion cannot read
        return str(error)
