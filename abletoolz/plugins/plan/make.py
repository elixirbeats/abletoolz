"""Making a plan from sets: every plugin in them and what each can become here, nothing picked."""

from __future__ import annotations

import datetime
import pathlib
from collections.abc import Iterable

from abletoolz.formats import live_document
from abletoolz.live_set.tracks import track_label
from abletoolz.plugins.container.device import PluginDevice, PluginDevices
from abletoolz.plugins.identity.ids import PluginKind, stored_id
from abletoolz.plugins.plan.carry import carry
from abletoolz.plugins.plan.machine import Machine
from abletoolz.plugins.plan.model import DeviceRef, Plan, PlannedSet, PluginChoice, Route, set_sha256
from abletoolz.plugins.plan.targets import mapped_targets, recovery_target, stored_label
from abletoolz.plugins.recovery import route_for
from abletoolz.plugins.state.all_plugins import CATALOG

# The order a plan lists targets in: the same plugin first, guesses last.
_ROUTE_ORDER = (Route.FORMAT, Route.RECOVERY, Route.FILE, Route.CONVERTER, Route.EXPERIMENT)


def _track_name(device: PluginDevice) -> str:
    assert device.document is not None
    track = device.track
    return "?" if track is None else track_label(track, device.document.version)


def make_plan(paths: Iterable[pathlib.Path], machine: Machine) -> Plan:
    """Read ``paths`` and plan every plugin device in them, every pick ``keep``; nothing is written."""
    return merge_plans(plan_set(index, path, machine) for index, path in enumerate(paths))


def plan_set(index: int, path: pathlib.Path, machine: Machine) -> tuple[PlannedSet, list[PluginChoice]]:
    """The set at ``path``, the ``index``-th of a plan, and its plugins planned; ``merge_plans`` joins sets."""
    path = path.resolve()
    planned = PlannedSet(path=path, sha256=set_sha256(path))
    choices: dict[tuple[PluginKind, str | None, str], PluginChoice] = {}
    for device in PluginDevices(live_document.read(path)):
        assert device.ordinal is not None
        identity = device.identity
        stored = stored_id(identity)
        key = device_key(device)
        if key not in choices:
            plugin_map = None if stored is None else CATALOG.find(stored)
            targets = [] if plugin_map is None else mapped_targets(plugin_map, device.kind, machine)
            route = route_for(device.kind, identity.name or "", identity.unique_id, machine.recovery)
            if route is not None:
                targets.append(recovery_target(route))
            targets.sort(key=lambda target: _ROUTE_ORDER.index(target.route))
            choices[key] = PluginChoice(
                plugin=(identity.name or "<unknown>") if plugin_map is None else plugin_map.name,
                format=device.kind,
                name=identity.name or "",
                stored=key[1],
                map_id=None if plugin_map is None else plugin_map.id,
                broken=False,
                devices=[],
                targets=targets,
            )
        choice = choices[key]
        choice.devices.append(DeviceRef(set=index, device=device.ordinal, track=_track_name(device)))
        if not machine.loadable(device.info):
            choice.broken = True
        # Counted while this set is loaded, so a plan over a library holds one parsed set at a time.
        for target in choice.targets:
            answer = carry(device, choice, target, machine)
            if isinstance(answer, str):
                target.refused[answer] = target.refused.get(answer, 0) + 1
                continue
            for cost in answer.warnings:
                target.warnings[cost] = target.warnings.get(cost, 0) + 1
    return planned, list(choices.values())


def merge_plans(parts: Iterable[tuple[PlannedSet, list[PluginChoice]]]) -> Plan:
    """One plan from ``plan_set``'s answers, in set order: each plugin's devices, breakage and counts added up."""
    sets: list[PlannedSet] = []
    choices: dict[tuple[PluginKind, str | None, str], PluginChoice] = {}
    for planned, part in parts:
        sets.append(planned)
        for choice in part:
            seen = choices.setdefault(choice_key(choice), choice)
            if seen is choice:
                continue
            seen.devices.extend(choice.devices)
            seen.broken = seen.broken or choice.broken
            for total, target in zip(seen.targets, choice.targets, strict=True):
                for count, more in ((total.refused, target.refused), (total.warnings, target.warnings)):
                    for reason, number in more.items():
                        count[reason] = count.get(reason, 0) + number
    ordered = sorted(choices.values(), key=lambda choice: (not choice.broken, choice.plugin.lower(), choice.format))
    return Plan(made=datetime.datetime.now(datetime.UTC), sets=sets, plugins=ordered)


def choice_key(choice: PluginChoice) -> tuple[PluginKind, str | None, str]:
    return (choice.format, choice.stored, choice.name)


def device_key(device: PluginDevice) -> tuple[PluginKind, str | None, str]:
    """The plugin a device holds, as a plan entry names it: format, stored id and name."""
    return (device.kind, stored_label(stored_id(device.identity)), device.identity.name or "")
