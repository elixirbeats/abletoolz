"""Every target a plugin can become here.

A plan file offers the plugin's own other formats, converters and recovery routes,
each by a name written by hand: a format's ``plan_name``, a route's id. Another
installed file of a VST2 and a name-matched guess are for Python only.
"""

from __future__ import annotations

import pathlib

from abletoolz.plugins.container.device import has_writer
from abletoolz.plugins.identity.ids import Identity, PluginKind, StoredId, Vst2Id, Vst3Id
from abletoolz.plugins.identity.plugin_db import FORMAT_LABELS
from abletoolz.plugins.plan.evidence import _map_by_id
from abletoolz.plugins.plan.machine import Machine
from abletoolz.plugins.plan.model import Route, Target
from abletoolz.plugins.recovery_routes import RecoveryTarget, TargetStatus
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.measured import MeasuredState
from abletoolz.plugins.state.plugin_map import PluginFormat, PluginMap


def _can_rewrite(kind: PluginKind, to: PluginKind) -> bool:
    """Whether a device of ``kind`` can be rebuilt as ``to`` at all: a format nothing writes yet is no target."""
    return kind is not to and has_writer(to)


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
) -> str:
    """The id of a target a plan file never offers: another installed file, or a guess."""
    if path is not None:
        where = f"file {path}"
    else:
        assert kind is not None
        where = f"{plugin} ({FORMAT_LABELS[kind]})"
    return f"try {where}" if route is Route.EXPERIMENT else where


def _crossing(written: MeasuredState, kind: PluginKind, read_from: PluginFormat | None) -> tuple[str, bool]:
    """A format target's evidence label and whether it is proved.

    The patch is read out of the device's format and written to another, and
    both sides have to be measured for the crossing to be, so a device read out
    of a format only the rig has asked about is labelled with that side too. A
    converter's own evidence already covers reading its source, and a crossing
    whose written side is unproved keeps that side's label.
    """
    if read_from is None or read_from.measured.predictable or not written.predictable:
        return written.annotation, written.predictable
    return f"{written.annotation}; read from the {FORMAT_LABELS[kind]}, {read_from.measured.annotation}", False


def mapped_targets(source: PluginMap, kind: PluginKind, machine: Machine) -> list[Target]:
    """The plugin's own other formats, then the plugins its converters reach."""
    found: list[Target] = []
    read_from = source.formats.get(kind)
    for converter in (None, *(item for item in CATALOG.converters() if item.source == source.id)):
        target_map = source if converter is None else _map_by_id(converter.target)
        route = Route.FORMAT if converter is None else Route.CONVERTER
        for to_kind, plugin_format in target_map.formats.items():
            if _can_rewrite(kind, to_kind) and target_map.can_write(to_kind):
                if converter is None:
                    evidence, proved = _crossing(plugin_format.measured, kind, read_from)
                else:
                    evidence, proved = converter.measured.annotation, converter.measured.predictable
                found.append(
                    Target(
                        id=plugin_format.plan_name,
                        route=route,
                        plugin=plugin_format.name,
                        format=to_kind,
                        evidence=evidence,
                        proved=proved,
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
        id=found.route.id,
        route=Route.RECOVERY,
        plugin=binary.name,
        format=PluginKind.VST,
        evidence=f"native recovery route, {found.route.state_evidence} -- audition before trusting",
        proved=False,
        installed=found.status is TargetStatus.AVAILABLE,
        route_id=found.route.id,
        path=found.path,
    )
