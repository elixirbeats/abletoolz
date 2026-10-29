"""``abletoolz plugins``: plan what plugin devices become, apply the plan, list recovery routes.

``plan`` reads sets and writes a plan file listing every plugin in them, with
what each can become on this machine above it; ``apply`` converts every plugin
whose ``convert_to`` names a target, in place, keeping the usual backup. ``routes``
lists the measured native recovery routes and the binaries found for them.
See :mod:`abletoolz.plugins.plan`.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections.abc import Sequence
from xml.etree import ElementTree as ET

from abletoolz.machine import Locations
from abletoolz.plugins.config import AbletoolzConfig
from abletoolz.plugins.plan import apply_in_place, plan_sets, read_plan, this_machine, write_plan
from abletoolz.plugins.plan.here import recovery_targets
from abletoolz.run import console

DEFAULT_PLAN = pathlib.Path("plugin_plan.yaml")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="abletoolz plugins",
        description="Plan what plugin devices become, apply the plan, and list native recovery routes.",
    )
    verbs = parser.add_subparsers(dest="command", required=True)

    def recovery_options(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--plugin-dir",
            action="append",
            type=pathlib.Path,
            metavar="DIR",
            help="Search only these plugin folders for recovery binaries instead of the installed defaults",
        )

    routes = verbs.add_parser("routes", help="List verified recovery routes and available native binaries")
    recovery_options(routes)
    routes.add_argument("--json", action="store_true", help="Print the routes as JSON")

    plan = verbs.add_parser(
        "plan",
        help="Write a plan of what every plugin device in some sets can become; nothing is changed",
    )
    plan.add_argument("srcs", nargs="+", type=pathlib.Path, metavar="SET_OR_DIR")
    plan.add_argument(
        "-o",
        "--output",
        type=pathlib.Path,
        default=DEFAULT_PLAN,
        metavar="PLAN.yaml",
        help=f"Where the plan is written (default: {DEFAULT_PLAN})",
    )
    plan.add_argument("--force", action="store_true", help="Overwrite an existing plan file")
    recovery_options(plan)

    apply = verbs.add_parser(
        "apply", help="Convert every plugin whose convert_to names a target, keeping a backup of each set"
    )
    apply.add_argument("plan", type=pathlib.Path, metavar="PLAN.yaml")
    apply.add_argument("--json", action="store_true", help="Print what was done as JSON")
    recovery_options(apply)
    return parser


def run_plugin_commands(argv: Sequence[str], *, config: AbletoolzConfig, locations: Locations) -> int:
    """Run ``abletoolz plugins`` against the machine at ``locations``; answers the exit code.

    A command line argparse refuses raises its ``SystemExit``, for the caller to turn into the exit code.
    """
    args = _parser().parse_args(argv)
    try:
        if args.command == "routes":
            routes = recovery_targets(args.plugin_dir, config=config, locations=locations)
            if args.json:
                print(json.dumps([target.model_dump(mode="json") for target in routes], indent=2))
            else:
                console.render_recovery_routes(routes)
            return 0
        machine = this_machine(args.plugin_dir, config=config, locations=locations)
        if args.command == "plan":
            output = args.output.resolve()
            if output.exists() and not args.force:
                raise FileExistsError(f"{output} exists; pass --force to overwrite it")
            plan = plan_sets(args.srcs, machine=machine)
            write_plan(plan, output)
            console.render_plan(plan, output)
            return 0
        outcomes = apply_in_place(read_plan(args.plan, machine=machine), machine=machine)
    except (OSError, ValueError, ET.ParseError) as error:
        print(f"abletoolz plugins {args.command} failed: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([outcome.model_dump(mode="json") for outcome in outcomes], indent=2))
    else:
        console.render_applied_plan(outcomes)
    return 0
