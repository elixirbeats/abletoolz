"""Check one plugin device against a native recovery route, and say what it becomes.

A plan finds a plugin's route with :func:`route_for` when it reads the set, and
asks :func:`recover` what each of the plugin's devices becomes through it: a
refusal, or an edit pointing it at the native file. It asks once when the plan
is made and again when it is applied, and what it writes is that second answer:
the native binary is hashed inside the one call, since a plan can sit for days
between being made and applied. Neither reads the set again: the plan's own
hash of the set is what says it has not changed since.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from abletoolz.formats.errors import FormatError
from abletoolz.plugins.container.device import PluginDevice, ReferenceForm
from abletoolz.plugins.container.rewritable import control_losses
from abletoolz.plugins.conversion import Answer, Edit
from abletoolz.plugins.identity.ids import PluginKind, fourcc
from abletoolz.plugins.recovery_routes import RecoveryTarget, StateMode, TargetStatus, fingerprint_binary


class RecoveryError(ValueError):
    """Recovery would change data outside the measured reference operation."""


def _uid(value: int | None) -> int:
    """A stored unique id as the routes record it: unsigned, and refused outside 32 bits."""
    if value is None:
        raise RecoveryError("Missing plugin identity")
    if not -(1 << 31) <= value < (1 << 32):
        raise RecoveryError("Plugin identity is outside the 32-bit range")
    return value & 0xFFFFFFFF


def route_for(
    kind: PluginKind, name: str, unique_id: int | None, targets: Sequence[RecoveryTarget]
) -> RecoveryTarget | None:
    """The recovery route for a VST2 plugin stored as ``name`` and ``unique_id``, or None when it has none."""
    if kind is not PluginKind.VST:
        return None
    try:
        uid = _uid(unique_id)
    except RecoveryError:
        return None
    return next((item for item in targets if name in item.route.source_names and item.route.unique_id == uid), None)


def _validate_device(device: PluginDevice, target: RecoveryTarget) -> tuple[str, int]:
    route = target.route
    identity, reference = device.identity, device.reference
    try:
        preset = device.saved_state
    except (FormatError, ValueError) as error:
        raise RecoveryError("Malformed saved plugin buffer") from error
    if device.kind is not PluginKind.VST or identity.name not in route.source_names:
        raise RecoveryError("The device is not a plugin this route was measured on")
    if _uid(identity.unique_id) != route.unique_id:
        raise RecoveryError("Source identity differs from the measured recovery route")
    if identity.parameter_count != route.parameter_count:
        raise RecoveryError("Source public parameter count differs from the measured source layout")
    if identity.program_count != route.program_count:
        raise RecoveryError("Source program count differs from the measured native bank")
    if identity.flags is None:
        raise RecoveryError("Missing device field: Flags")
    if reference.form is ReferenceForm.NONE or (reference.form is ReferenceForm.DIR and reference.file_name is None):
        raise RecoveryError("Expected one VST2 Path or Dir/FileName representation")
    if preset.type is None:
        raise RecoveryError("Expected exactly one saved VST2 preset")
    if preset.type != fourcc(route.preset_type):
        raise RecoveryError("Preset container differs from the measured route")
    if _uid(preset.unique_id) != route.unique_id:
        raise RecoveryError("Saved preset identity differs from the device")
    if preset.parameter_count != route.parameter_count:
        raise RecoveryError("Saved preset public parameter count differs from the device")
    if preset.program_count != route.program_count:
        raise RecoveryError("Saved preset program count differs from the device")
    program = preset.program_number
    if program is None or not 0 <= program < route.program_count:
        raise RecoveryError("Selected program is outside the measured bank")
    raw = preset.state
    if not raw:
        raise RecoveryError("Saved plugin buffer is empty")
    if device.owner is None:
        raise RecoveryError("Expected a PluginDevice around the selected VST2 reference")
    controls = device.controls
    if controls.malformed:
        raise RecoveryError("Configured control has no readable parameter id")
    if any(not 0 <= index < route.parameter_count for index in controls.configured):
        raise RecoveryError("Configured control is outside the measured public parameter layout")
    return hashlib.sha256(raw).hexdigest(), program


def recover(device: PluginDevice, target: RecoveryTarget) -> Answer:
    """What ``device``, read through PluginDevices, becomes through ``target``'s route; nothing changes.

    A refusal, or an :class:`Edit` that points it at the native file, raising
    its program count where the route measured a larger bank.
    """
    route = target.route
    try:
        digest, program = _validate_device(device, target)
    except RecoveryError as error:
        return str(error)
    state = next((state for state in route.states if state.sha256 == digest), None)
    if state is None or program not in state.program_numbers:
        return "Saved bank and selected program have not been verified"
    if target.status is not TargetStatus.AVAILABLE or target.path is None or target.binary is None:
        return target.reason
    binary = target.binary
    try:
        if fingerprint_binary(target.path) != binary.sha256:
            return "Native plugin binary changed after it was verified"
    except OSError as error:  # the binary is gone
        return str(error)
    programs = binary.program_count if route.state_mode is StateMode.REFERENCE_WITH_PROGRAM_CAPACITY else None
    # A route that carries a patch carries no list of controls, so links to the old ones go.
    renumbered = route.state_mode is not StateMode.REFERENCE
    losses = control_losses(device) if renumbered else ()
    return Edit(target.path, binary.name, programs=programs, forget_controls=renumbered, warnings=losses)
