"""Plans on this computer: made, kept as YAML and applied the way ``abletoolz plugins`` does.

``plan_sets`` is ``abletoolz plugins plan``, ``write_plan`` and ``read_plan``
are the plan file, and ``apply_in_place`` is ``abletoolz plugins apply``. Each
builds this computer's :class:`Machine` unless it is handed one. ``read_plan``
reads the sets again, so what is applied is what is on disk now.
"""

from __future__ import annotations

import multiprocessing
import os
import pathlib
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor

from abletoolz.live_set.persistence import is_backup_set, save_in_place
from abletoolz.machine import Locations
from abletoolz.plugins.config import AbletoolzConfig, load_config
from abletoolz.plugins.plan.apply import SetOutcome, apply_plan
from abletoolz.plugins.plan.machine import Machine
from abletoolz.plugins.plan.make import make_plan, merge_plans, plan_set
from abletoolz.plugins.plan.model import Plan, PlannedSet, PluginChoice, dump_plan, load_plan, take_picks
from abletoolz.plugins.recovery_routes import RecoveryTarget, load_recovery_routes, resolve_recovery_targets
from abletoolz.workers import set_workers


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
    machine: Machine | None = None,
    jobs: int | None = 1,
    plugin_dirs: Iterable[str | os.PathLike[str]] | None = None,
    config: AbletoolzConfig | None = None,
    locations: Locations | None = None,
) -> Plan:
    """Plan every plugin device in the sets ``sources`` name, folders searched recursively; nothing is written.

    ``jobs`` sets are read at once (None: one per CPU), each worker process
    building this computer's :class:`Machine` from ``plugin_dirs``, ``config``
    and ``locations`` as :func:`this_machine` does. A ``machine`` handed in
    cannot cross into a worker, so it plans in this process only.
    """
    paths = _set_paths(sources)
    workers = set_workers(jobs, len(paths))
    if machine is not None:
        if workers != 1:
            raise ValueError("A machine handed in plans in this process only; pass jobs=1")
        return make_plan(paths, machine)
    config, locations = _this_computer(config, locations)
    dirs = None if plugin_dirs is None else [pathlib.Path(folder) for folder in plugin_dirs]
    if workers == 1:
        return make_plan(paths, this_machine(dirs, config=config, locations=locations))
    pool = ProcessPoolExecutor(
        max_workers=workers,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_start_worker,
        initargs=(dirs, config, locations),
    )
    try:
        return merge_plans(pool.map(_plan_in_worker, range(len(paths)), paths))
    finally:
        pool.shutdown(cancel_futures=True)


def write_plan(plan: Plan, path: str | os.PathLike[str]) -> None:
    """Write ``plan`` as the YAML file people edit."""
    pathlib.Path(path).write_text(dump_plan(plan), encoding="utf-8")


def read_plan(
    path: str | os.PathLike[str],
    *,
    machine: Machine | None = None,
    jobs: int | None = 1,
    plugin_dirs: Iterable[str | os.PathLike[str]] | None = None,
    config: AbletoolzConfig | None = None,
    locations: Locations | None = None,
) -> Plan:
    """The sets a plan file names, planned again as they are now, with the file's convert_to as each pick.

    ``jobs`` and the rest are :func:`plan_sets`'s.
    """
    file = load_plan(pathlib.Path(path).read_text(encoding="utf-8"))
    plan = plan_sets(file.sets, machine=machine, jobs=jobs, plugin_dirs=plugin_dirs, config=config, locations=locations)
    take_picks(plan, file)
    return plan


def apply_in_place(plan: Plan, *, machine: Machine | None = None) -> list[SetOutcome]:
    """Rewrite the sets ``plan`` covers as it says, each original copied to ``abletoolz_backup`` the way ``-s`` does."""
    return apply_plan(plan, save=save_in_place, machine=this_machine() if machine is None else machine)


def _this_computer(config: AbletoolzConfig | None, locations: Locations | None) -> tuple[AbletoolzConfig, Locations]:
    locations = Locations.this_computer() if locations is None else locations
    config = load_config(locations.config_file) if config is None else config
    return config, locations.with_plugin_folders(config.plugin_paths)


# This worker process's machine, built once when the worker starts.
_machine: Machine | None = None


def _start_worker(plugin_dirs: list[pathlib.Path] | None, config: AbletoolzConfig, locations: Locations) -> None:
    global _machine
    _machine = this_machine(plugin_dirs, config=config, locations=locations)


def _plan_in_worker(index: int, path: pathlib.Path) -> tuple[PlannedSet, list[PluginChoice]]:
    assert _machine is not None
    return plan_set(index, path, _machine)


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
