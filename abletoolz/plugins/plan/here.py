"""Plans on this computer: made, kept as YAML and applied the way ``abletoolz plugins`` does.

``plan_sets`` is ``abletoolz plugins plan``, ``write_plan`` and ``read_plan``
are the plan file, and ``apply_in_place`` is ``abletoolz plugins apply``. Each
builds this computer's :class:`Machine` unless it is handed one, so a plan made
here is applied against what was checked here.
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import Iterable, Sequence

from abletoolz.live_set.persistence import is_backup_set, save_in_place
from abletoolz.machine import Locations
from abletoolz.plugins.config import AbletoolzConfig, load_config
from abletoolz.plugins.identity.ids import PluginKind
from abletoolz.plugins.plan.apply import SetOutcome, apply_plan
from abletoolz.plugins.plan.machine import Machine
from abletoolz.plugins.plan.make import make_plan, seed_from_plan
from abletoolz.plugins.plan.model import DEFAULT_FORMATS, Pick, Plan, dump_plan, load_plan
from abletoolz.plugins.recovery_routes import RecoveryTarget, load_recovery_routes, resolve_recovery_targets


def recovery_targets(
    plugin_dirs: Iterable[str | os.PathLike[str]] | None = None,
    *,
    config: AbletoolzConfig | None = None,
    locations: Locations | None = None,
) -> tuple[RecoveryTarget, ...]:
    """The measured recovery routes whose binaries are in this computer's plugin folders, or in ``plugin_dirs``."""
    config, locations = _this_computer(config, locations)
    folders = locations.vst_dirs if plugin_dirs is None else [pathlib.Path(folder) for folder in plugin_dirs]
    return resolve_recovery_targets(load_recovery_routes(), folders)


def this_machine(
    plugin_dirs: Iterable[str | os.PathLike[str]] | None = None,
    *,
    config: AbletoolzConfig | None = None,
    locations: Locations | None = None,
) -> Machine:
    """This computer as a plan sees it: its plugins, the folders config.yaml adds, and its recovery binaries.

    ``plugin_dirs`` limits where recovery binaries are looked for, like
    ``--plugin-dir``. ``config`` and ``locations`` stand in for config.yaml and
    this computer, each read only when it is None.
    """
    config, locations = _this_computer(config, locations)
    routes = recovery_targets(plugin_dirs, config=config, locations=locations)
    return Machine.here(routes, config=config, locations=locations)


def plan_sets(
    sources: Iterable[str | os.PathLike[str]],
    *,
    pick: Pick = Pick.BROKEN,
    formats: Sequence[PluginKind] = DEFAULT_FORMATS,
    picks_from: Plan | None = None,
    machine: Machine | None = None,
) -> Plan:
    """Plan every plugin device in the sets ``sources`` name, folders searched recursively; nothing is written.

    ``picks_from`` carries an edited plan's picks and overrides over, like ``--from``.
    """
    plan = make_plan(_set_paths(sources), this_machine() if machine is None else machine, pick=pick, formats=formats)
    if picks_from is not None:
        seed_from_plan(plan, picks_from)
    return plan


def write_plan(plan: Plan, path: str | os.PathLike[str]) -> None:
    """Write ``plan`` as the YAML file people edit."""
    pathlib.Path(path).write_text(dump_plan(plan), encoding="utf-8")


def read_plan(path: str | os.PathLike[str]) -> Plan:
    """Read a plan file, edits included."""
    return load_plan(pathlib.Path(path).read_text(encoding="utf-8"))


def apply_in_place(plan: Plan, *, machine: Machine | None = None) -> list[SetOutcome]:
    """Rewrite the sets ``plan`` covers as it says, each original copied to ``abletoolz_backup`` the way ``-s`` does."""
    return apply_plan(plan, save=save_in_place, machine=this_machine() if machine is None else machine)


def _this_computer(config: AbletoolzConfig | None, locations: Locations | None) -> tuple[AbletoolzConfig, Locations]:
    locations = Locations.this_computer() if locations is None else locations
    config = load_config(locations.config_file) if config is None else config
    return config, locations.with_plugin_folders(config.plugin_paths)


def _set_paths(sources: Iterable[str | os.PathLike[str]]) -> list[pathlib.Path]:
    found: set[pathlib.Path] = set()
    for source in map(pathlib.Path, sources):
        if not source.exists():
            raise FileNotFoundError(source)
        paths = source.rglob("*") if source.is_dir() else (source,)
        for candidate in paths:
            if candidate.suffix.casefold() == ".als" and candidate.is_file() and not is_backup_set(candidate):
                found.add(candidate.resolve())
    if not found:
        raise ValueError("No sets found outside backup folders")
    return sorted(found, key=str)
