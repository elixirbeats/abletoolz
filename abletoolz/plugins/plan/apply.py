"""Applying a plan to the sets it covers."""

from __future__ import annotations

import pathlib
from collections.abc import Callable
from xml.etree import ElementTree as ET

import pydantic

from abletoolz.formats import live_document
from abletoolz.plugins.container.device import PluginDevices
from abletoolz.plugins.conversion import write
from abletoolz.plugins.plan.carry import carry
from abletoolz.plugins.plan.machine import Machine
from abletoolz.plugins.plan.make import choice_key, device_key
from abletoolz.plugins.plan.model import Plan, set_sha256


class DeviceOutcome(pydantic.BaseModel):
    """What applying a plan did to one device."""

    model_config = pydantic.ConfigDict(extra="forbid")

    device: int
    track: str
    plugin: str
    pick: str
    done: bool
    reason: str | None = None
    evidence: str | None = None


class SetOutcome(pydantic.BaseModel):
    """What applying a plan did to one set; ``skipped`` says why it was left alone."""

    model_config = pydantic.ConfigDict(extra="forbid")

    path: pathlib.Path
    skipped: str | None = None
    devices: list[DeviceOutcome] = pydantic.Field(default_factory=list)

    @property
    def changed(self) -> int:
        return sum(1 for device in self.devices if device.done)


def apply_to_root(plan: Plan, set_index: int, root: ET.Element, machine: Machine) -> list[DeviceOutcome]:
    """Apply the plan's picks for one set to its parsed document, in place; answers what each device did."""
    by_device = {ref.device: (choice, ref) for choice in plan.plugins for ref in choice.devices if ref.set == set_index}
    outcomes: list[DeviceOutcome] = []
    for device in PluginDevices(root):
        assert device.ordinal is not None
        found = by_device.get(device.ordinal)
        if found is None:
            continue
        choice, ref = found
        pick = choice.pick_for(set_index, device.ordinal)
        target = choice.target(pick)
        if target is None:
            continue
        # A hand-edited plan whose sets were removed or reordered numbers other sets' devices.
        answer = (
            carry(device, choice, target, machine)
            if device_key(device) == choice_key(choice)
            else f"the plan lists {choice.plugin} here, but this device holds another plugin; make a fresh plan"
        )
        reason = answer if isinstance(answer, str) else None
        if not isinstance(answer, str):
            try:
                write(device, answer)
            except ValueError as error:  # StateTransformError, or what the container refuses
                reason = str(error)
        outcomes.append(
            DeviceOutcome(
                device=device.ordinal,
                track=ref.track,
                plugin=choice.plugin,
                pick=pick,
                done=reason is None,
                reason=reason,
                evidence=target.evidence,
            )
        )
    return outcomes


def apply_plan(
    plan: Plan,
    *,
    save: Callable[[pathlib.Path, ET.Element], None],
    machine: Machine,
) -> list[SetOutcome]:
    """Apply ``plan`` to every set it covers, handing each changed document to ``save``.

    A set whose bytes changed since the plan was made is skipped: the devices
    and choices in the plan describe the set as it was. ``machine`` is this
    machine as the plan was checked against: its recovery routes, and the VST3
    parameters a device carrying Live's own list needs.
    """

    for choice in plan.plugins:
        choice.target(choice.pick)
        for override in choice.overrides:
            choice.target(override.pick)
    outcomes: list[SetOutcome] = []
    for index, planned in enumerate(plan.sets):
        path = planned.path
        if not path.exists():
            outcomes.append(SetOutcome(path=path, skipped="the set is gone"))
            continue
        if set_sha256(path) != planned.sha256:
            outcomes.append(SetOutcome(path=path, skipped="it changed since it was planned; plan it again"))
            continue
        root = live_document.read(path)
        devices = apply_to_root(plan, index, root, machine)
        outcome = SetOutcome(path=path, devices=devices)
        if outcome.changed:
            try:
                save(path, root)
            except OSError as error:  # one unwritable set must not lose the report of the sets already rewritten
                outcome = SetOutcome(path=path, skipped=f"could not save: {error}")
        outcomes.append(outcome)
    return outcomes
