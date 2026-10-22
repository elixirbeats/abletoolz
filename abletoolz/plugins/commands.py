"""Inspect measured recovery routes and write plugin repairs to explicit copies."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
from collections.abc import Sequence
from xml.etree import ElementTree as ET

from pydantic import BaseModel, ConfigDict

from abletoolz.formats import live_document
from abletoolz.live_set.persistence import is_backup_set
from abletoolz.plugins.recovery import RecoveryPlan, plan_recovery, recovered_document
from abletoolz.plugins.recovery_routes import load_recovery_routes, resolve_recovery_targets


class RecoveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: RecoveryPlan
    written: pathlib.Path | None = None
    written_sha256: str | None = None
    report: pathlib.Path | None = None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="abletoolz plugins", description="Recover plugin references and prepared native states."
    )
    verbs = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("routes", "List verified recovery routes and available native binaries"),
        ("plan", "Inspect sets and report recoverable devices without writing files"),
        ("repair", "Apply the same recovery plan to new set copies"),
    ):
        command = verbs.add_parser(name, help=help_text)
        command.add_argument(
            "--plugin-dir",
            action="append",
            type=pathlib.Path,
            metavar="DIR",
            help="Search only these plugin folders instead of the installed defaults; repeatable",
        )
        command.add_argument("--json", action="store_true", help="Print the complete result as JSON")
        command.add_argument(
            "--prepared",
            action="append",
            type=pathlib.Path,
            metavar="MANIFEST.json",
            help="Include an explicit prepared VST3 conversion manifest; repeatable; no plugin execution",
        )
        if name != "routes":
            command.add_argument("srcs", nargs="+", type=pathlib.Path, metavar="SET_OR_DIR")
        if name == "repair":
            output = command.add_mutually_exclusive_group(required=True)
            output.add_argument(
                "--output", type=pathlib.Path, metavar="COPY.als", help="New file for one explicit input set"
            )
            output.add_argument(
                "--output-dir",
                type=pathlib.Path,
                metavar="DIR",
                help="New copies under this folder, retaining the inputs' relative directory structure",
            )
    return parser


def _sources(inputs: Sequence[pathlib.Path], excluded: pathlib.Path | None) -> list[pathlib.Path]:
    sources: set[pathlib.Path] = set()
    for source in inputs:
        if not source.exists():
            raise FileNotFoundError(source)
        paths = source.rglob("*") if source.is_dir() else (source,)
        for candidate in paths:
            if (
                candidate.suffix.casefold() not in (".als", ".alc")
                or not candidate.is_file()
                or is_backup_set(candidate)
            ):
                continue
            resolved = candidate.resolve()
            if excluded is None or not resolved.is_relative_to(excluded):
                sources.add(resolved)
    if not sources:
        raise ValueError("No input sets found outside backup and output folders")
    return sorted(sources, key=str)


def _destinations(
    plans: Sequence[RecoveryPlan],
    output: pathlib.Path | None,
    directory: pathlib.Path | None,
    source_root: pathlib.Path,
) -> list[tuple[RecoveryPlan, pathlib.Path]]:
    destinations: list[tuple[RecoveryPlan, pathlib.Path]] = []
    inputs = {plan.source for plan in plans}
    used: set[pathlib.Path] = set()
    for plan in plans:
        if output is not None:
            target = output.resolve()
        elif directory is not None:
            target = (directory / plan.source.relative_to(source_root)).resolve()
        else:
            raise ValueError("An explicit output is required")
        report = target.with_name(target.name + ".recovery.json")
        if target in inputs or report in inputs:
            raise ValueError("Recovery output must differ from every input set")
        if target in used or report in used:
            raise ValueError(f"Multiple outputs select the same path: {target}")
        if target.exists() or report.exists():
            raise FileExistsError(f"Recovery never overwrites existing output: {target}")
        if target.suffix.casefold() != plan.source.suffix.casefold():
            raise ValueError("Recovery output must retain the input document extension")
        if not plan.ready_count:
            continue
        used.update((target, report))
        destinations.append((plan, target))
    return destinations


def _write_copies(
    plans: Sequence[RecoveryPlan],
    output: pathlib.Path | None,
    directory: pathlib.Path | None,
    source_root: pathlib.Path,
) -> list[RecoveryResult]:
    destinations = _destinations(plans, output, directory, source_root)
    prepared = [(plan, target, recovered_document(plan)) for plan, target in destinations]
    results = {plan.source: RecoveryResult(plan=plan) for plan in plans}
    for plan, target, document in prepared:
        target.parent.mkdir(parents=True, exist_ok=True)
        report = target.with_name(target.name + ".recovery.json")
        result = RecoveryResult(
            plan=plan, written=target, written_sha256=hashlib.sha256(document).hexdigest(), report=report
        )
        live_document.write_new(target, document)
        live_document.write_new(report, (result.model_dump_json(indent=2) + "\n").encode("utf-8"))
        results[plan.source] = result
    return [results[plan.source] for plan in plans]


def _render(results: Sequence[RecoveryResult]) -> None:
    for result in results:
        plan = result.plan
        print(f"{plan.source}: {plan.ready_count} recoverable, {plan.blocked_count} unresolved")
        for device in plan.devices:
            if device.status.value == "not_applicable":
                continue
            print(f"  [{device.device}] {device.source_name}: {device.status.value}; {device.reason}")
        if result.written is not None:
            print(f"  Written: {result.written}")
            if plan.project_relative_file_refs:
                print(
                    f"  {plan.project_relative_file_refs} project-relative media references retained; "
                    "assets not relocated"
                )


def run_plugin_commands(argv: Sequence[str]) -> int:
    """Run structured plugin commands without legacy save, sidecar or Live behavior."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "repair" and args.output is not None:
        if len(args.srcs) != 1 or not args.srcs[0].is_file():
            parser.error("--output takes one explicit input file; use --output-dir for folders or batches")
    try:
        routes = load_recovery_routes() if args.prepared is None else load_recovery_routes(args.prepared)
        targets = resolve_recovery_targets(routes, args.plugin_dir)
        if args.command == "routes":
            if args.json:
                print(json.dumps([target.model_dump(mode="json") for target in targets], indent=2))
            else:
                for target in targets:
                    print(f"{target.route.id}: {target.status.value} ({target.route.state_evidence})")
                    print(f"  {', '.join(target.route.source_names)}")
                    print(f"  {target.path if target.path is not None else target.reason}")
            return 0
        directory = args.output_dir.resolve() if args.command == "repair" and args.output_dir is not None else None
        sources = _sources(args.srcs, directory)
        plans = [plan_recovery(source, targets) for source in sources]
        if args.command == "repair":
            source_root = pathlib.Path(
                os.path.commonpath(
                    [source.resolve() if source.is_dir() else source.resolve().parent for source in args.srcs]
                )
            )
            results = _write_copies(plans, args.output, directory, source_root)
        else:
            results = [RecoveryResult(plan=plan) for plan in plans]
    except (OSError, ValueError, ET.ParseError) as error:
        if args.json:
            print(json.dumps({"command": args.command, "error": str(error)}))
        else:
            print(f"Plugin recovery failed: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(
            json.dumps(
                {"command": args.command, "sets": [result.model_dump(mode="json") for result in results]}, indent=2
            )
        )
    else:
        _render(results)
    return 0
