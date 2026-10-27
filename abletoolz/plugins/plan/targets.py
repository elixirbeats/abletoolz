"""Every target a plugin can become here, and which one a new plan picks."""

from __future__ import annotations

import pathlib
from collections.abc import Sequence

from abletoolz.plugins.container.device import has_writer
from abletoolz.plugins.identity.ids import Identity, PluginKind, StoredId, Vst2Id, Vst3Id
from abletoolz.plugins.identity.plugin_db import FORMAT_LABELS
from abletoolz.plugins.plan.evidence import _converter, _map_by_id
from abletoolz.plugins.plan.machine import Machine
from abletoolz.plugins.plan.model import KEEP, Pick, PluginChoice, Route, Target
from abletoolz.plugins.recovery_routes import RecoveryTarget, TargetStatus
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence
from abletoolz.plugins.state.plugin_map import PluginMap

_EVIDENCE_STRENGTH = {StateEvidence.EAR: 3, StateEvidence.DECLARED: 3, StateEvidence.HOSTED: 2}
# Evidence a route has without a MeasuredState: a recovery route was read back by
# the native plugin, a file of the same id is only the same id, an experiment is a guess.
_ROUTE_STRENGTH = {Route.RECOVERY: 2, Route.FILE: 1, Route.EXPERIMENT: 0}
# Routes that keep the plugin the device already is.
_SAME_PLUGIN = frozenset({Route.FORMAT, Route.FILE, Route.RECOVERY})


def _can_rewrite(kind: PluginKind, to: PluginKind) -> bool:
    """Whether a device of ``kind`` can be rebuilt as ``to`` at all: a format nothing writes yet is no target."""
    return kind is not to and has_writer(to)


def _strength(measured: MeasuredState) -> int:
    return max((_EVIDENCE_STRENGTH.get(item, 1) for item in measured.evidence), default=0)


def stored_label(stored: StoredId | None) -> str | None:
    if stored is None:
        return None
    if isinstance(stored, Vst2Id):
        return f"vst2 {stored.unique_id}"
    if isinstance(stored, Vst3Id):
        return "vst3 " + " ".join(str(field) for field in stored.uid_fields)
    return "au " + " ".join(str(code) for code in stored.identifier)


def _target_id(
    route: Route,
    *,
    plugin: str | None = None,
    kind: PluginKind | None = None,
    path: pathlib.Path | None = None,
    route_id: str | None = None,
) -> str:
    """How a plan file names a target. A pick is one of these, so the spelling lives here and nowhere else."""
    if route is Route.RECOVERY:
        return f"recovery {route_id}"
    if path is not None:
        where = f"file {path}"
    else:
        assert plugin is not None and kind is not None
        where = f"{plugin} ({FORMAT_LABELS[kind]})"
    return f"try {where}" if route is Route.EXPERIMENT else where


def mapped_targets(source: PluginMap, kind: PluginKind, machine: Machine) -> list[Target]:
    """The plugin's own other formats, then the plugins its converters reach."""
    found: list[Target] = []
    for converter in (None, *(item for item in CATALOG.converters() if item.source == source.id)):
        target_map = source if converter is None else _map_by_id(converter.target)
        route = Route.FORMAT if converter is None else Route.CONVERTER
        for to_kind, plugin_format in target_map.formats.items():
            if _can_rewrite(kind, to_kind) and target_map.can_write(to_kind):
                measured = plugin_format.measured if converter is None else converter.measured
                found.append(
                    Target(
                        id=_target_id(route, plugin=plugin_format.name, kind=to_kind),
                        route=route,
                        plugin=plugin_format.name,
                        format=to_kind,
                        evidence=measured.annotation,
                        proved=measured.predictable,
                        installed=machine.installed(plugin_format.id),
                        map_id=target_map.id,
                    )
                )
    return found


def file_targets(unique_id: int, machine: Machine) -> list[Target]:
    """Every installed file of the same VST2 plugin, the patch going across as it is."""
    return [
        Target(
            id=_target_id(Route.FILE, path=entry.path),
            route=Route.FILE,
            plugin=entry.name,
            format=PluginKind.VST,
            evidence="same plugin id in another file; patch kept as it is -- audition before trusting",
            proved=False,
            installed=True,
            path=entry.path,
            unique_id=entry.unique_id,
        )
        for entry in machine.vst2
        if entry.unique_id == unique_id
    ]


def experiments(identity: Identity, machine: Machine) -> list[Target]:
    """Installed plugins named like an unmapped one, by its own vendor: guesses, never picked for you.

    A guess names its VST3 by class id and nothing else, so it can name a VST3
    and no Audio Unit, whose codes and registration only a plugin map holds.
    """
    found: list[Target] = []
    if _can_rewrite(identity.kind, PluginKind.VST3) and (guessed := machine.guess(identity, PluginKind.VST3)):
        vst3_name, why = guessed
        found.append(
            Target(
                id=_target_id(Route.EXPERIMENT, plugin=vst3_name, kind=PluginKind.VST3),
                route=Route.EXPERIMENT,
                plugin=vst3_name,
                format=PluginKind.VST3,
                evidence=f"guess: {why}",
                proved=False,
                installed=True,
                uid_fields=machine.vst3[vst3_name],
            )
        )
    if identity.kind is PluginKind.VST and (guessed := machine.guess(identity, PluginKind.VST)):
        entry = next(item for item in machine.vst2 if item.name == guessed[0])
        found.append(
            Target(
                id=_target_id(Route.EXPERIMENT, path=entry.path),
                route=Route.EXPERIMENT,
                plugin=entry.name,
                format=PluginKind.VST,
                evidence=f"guess: {guessed[1]}",
                proved=False,
                installed=True,
                path=entry.path,
                unique_id=entry.unique_id,
            )
        )
    return found


def recovery_target(found: RecoveryTarget) -> Target:
    """The target a plugin's verified recovery route offers, with the binary found for it here."""
    binary = found.binary or found.route.targets[0]
    return Target(
        id=_target_id(Route.RECOVERY, route_id=found.route.id),
        route=Route.RECOVERY,
        plugin=binary.name,
        format=PluginKind.VST,
        evidence=f"native recovery route, {found.route.state_evidence} -- audition before trusting",
        proved=False,
        installed=found.status is TargetStatus.AVAILABLE,
        route_id=found.route.id,
        path=found.path,
    )


def _route_strength(choice: PluginChoice, target: Target) -> int:
    if target.route is Route.FORMAT:
        assert target.map_id is not None
        return _strength(_map_by_id(target.map_id).plugin_format(target.format).measured)
    if target.route is Route.CONVERTER:
        assert choice.map_id is not None and target.map_id is not None
        return _strength(_converter(choice.map_id, target.map_id).measured)
    return _ROUTE_STRENGTH[target.route]


def initial_pick(choice: PluginChoice, policy: Pick, formats: Sequence[PluginKind]) -> str:
    """The target a new plan picks: the best one installed here, where the policy wants one.

    The same plugin before another plugin, then stronger evidence, then the
    format order. An experiment is never picked for you.
    """
    if policy is Pick.KEEP or (policy is Pick.BROKEN and not choice.broken):
        return KEEP

    def rank(target: Target) -> tuple[int, int, int]:
        format_rank = formats.index(target.format) if target.format in formats else len(formats)
        return (0 if target.route in _SAME_PLUGIN else 1, -_route_strength(choice, target), format_rank)

    candidates = [target for target in choice.targets if target.installed and target.route is not Route.EXPERIMENT]
    best = min(candidates, key=rank, default=None)
    return KEEP if best is None else best.id
