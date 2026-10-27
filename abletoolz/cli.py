"""The command line: what it parses, and which part of abletoolz each command hands its work to.

``main`` takes the arguments, the config and the computer's locations instead
of reading ``sys.argv``, the config file and the OS, and answers the exit code,
so a test calls it the way the console script does. A run over sets goes to
:func:`abletoolz.run.batch.run_sets`, ``abletoolz plugins`` to
:func:`abletoolz.plugins.commands.run_plugin_commands`.
"""

import argparse
import datetime
import json
import logging
import pathlib
import sys
from collections.abc import Sequence

import pydantic

from abletoolz import __version__
from abletoolz.ansi import CB, B, C, G, M, R, Y
from abletoolz.formats import live_document
from abletoolz.live_set import AbletonSet
from abletoolz.live_set.apply_ops import OpsDocument, apply_ops
from abletoolz.live_set.describe import DescribeLevel
from abletoolz.live_set.persistence import BACKUP_DIR, find_sets
from abletoolz.machine import Locations
from abletoolz.plugins.commands import run_plugin_commands
from abletoolz.plugins.config import AbletoolzConfig, load_config
from abletoolz.plugins.identity import plugin_db
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.run import batch, console, meta, report
from abletoolz.run.job import COMMAND_FLAGS, SetJob
from abletoolz.samples import database

logger = logging.getLogger(__name__)


def worker_argument(value: str) -> int | None:
    """Accept auto or a positive worker override."""
    if value == "auto":
        return None
    try:
        count = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Expected auto or a positive worker count") from error
    if count < 1:
        raise argparse.ArgumentTypeError("Worker count must be positive")
    return count


# The set command that is not a SetJob command: --apply-ops writes through its
# own --output, bypassing run_set and -s, yet is exclusive with --db like the rest.
AUTHORING_FLAGS = ("apply_ops",)


def is_valid_dir_path(path: str) -> str:
    """Refuse a Windows path whose trailing backslash escaped its closing quote."""
    if sys.platform.startswith("win") and '"' in path:
        raise argparse.ArgumentTypeError(
            f"{R}Windows paths must not end in backslash: \n'C:\\somepath\\'(BAD)\n'C:\\somepath' "
            f"(GOOD)\nThis is due to a bug in how Windows handles backslashes before quotes."
        )
    return path


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    """The command line's arguments, refused through ``parser.error`` (``SystemExit(2)``) when they conflict."""
    parser = argparse.ArgumentParser(
        description=f"abletoolz {__version__}",
        add_help=True,
        epilog="Plan and convert plugin devices: abletoolz plugins {plan,apply,routes} --help",
    )

    # Input arguments.
    parser.add_argument(
        "srcs",
        action="store",
        nargs="*",
        type=is_valid_dir_path,
        help="Set(s) or directory(ies). All sub folders in directories are parsed for sets. NOTE: On WINDOWS remove "
        "the trailing backslash from a folder: it escapes the closing quote.",
    )
    analysis = parser.add_argument_group("analysis (read-only)")
    analysis.add_argument(
        "--list-tracks",
        action="store_true",
        default=False,
        help="Load and list all track information.",
    )
    analysis.add_argument(
        "--check-samples",
        action="store_true",
        default=False,
        help="Checks relative and absolute sample paths and verifies if sample exists there, including the "
        "samples and kits plugins such as Serato Sample and Maschine keep inside their own state.",
    )
    analysis.add_argument(
        "--check-plugins",
        action="store_true",
        default=False,
        help="Checks installed VST and Audio Unit references. Note: If Ableton finds the "
        "plugin name in a different path it will automatically update these paths the next time "
        "you save your project, so take it with a grain of salt.",
    )

    editing = parser.add_argument_group("editing (in memory only; add -s/--save to write)")
    editing.add_argument(
        "--unfold",
        action="store_true",
        default=False,
        help="unfolds all tracks/automation lanes in arrangement.",
    )
    editing.add_argument(
        "--fold",
        action="store_true",
        default=False,
        help="folds all tracks/automation lanes in arrangement.",
    )
    editing.add_argument("--set-track-heights", type=int, help="Set arrangement track heights")
    editing.add_argument("--set-track-widths", type=int, help="Set clip view track width.")
    editing.add_argument(
        "--cue-out",
        type=int,
        help="set Cue audio output tracks. Set to 1 for stereo 1/2, 2 for 3/4 etc",
    )
    editing.add_argument(
        "--master-out",
        type=int,
        help="number to set Master audio output tracks to. Same numbers as --cue-out",
    )
    editing.add_argument(
        "--fix-samples-absolute",
        action="store_true",
        default=False,
        help="Finds missing samples and fixes the broken references in your ableton sets, including the ones "
        "plugins such as Serato Sample keep inside their own state. Does not copy sample into "
        "project folder. Run --db on folders first "
        "to create your database.",
    )
    editing.add_argument(
        "--fix-samples-collect",
        action="store_true",
        default=False,
        help="Collects and saves missing samples into the set's project folder, the same as collect and save in "
        "Ableton. A plugin's sample is copied there too and the plugin pointed at the copy by absolute path. "
        "Files only a plugin loads, like a Kontakt instrument, are relinked where they are and never copied. "
        "Run --db on folders first to create your database.",
    )
    editing.add_argument(
        "--gradient-tracks",
        action="store_true",
        default=False,
        help="Generate a random gradient over the tracks and color them. Ableton's palette is small, so results are "
        "approximate; the CIE2000 color distance keeps the gradient natural to the eye.",
    )

    saving = parser.add_argument_group("saving")
    saving.add_argument(
        "-s",
        "--save",
        action="store_true",
        default=False,
        help="Saves file after parsing; nothing is written without it. The original file is always renamed to "
        f"set_directory/{BACKUP_DIR}/set_name_xx.als, where xx increases to avoid overwriting files.",
    )
    saving.add_argument(
        "-x",
        "--xml",
        action="store_true",
        default=False,
        help="dump the xml in same directory as set_name.xml(Useful to understand set structure).",
    )
    saving.add_argument(
        "--output",
        type=pathlib.Path,
        default=None,
        metavar="PATH",
        help="Write somewhere else instead of in place. With -s/--save: a directory; every set the "
        "run changes is saved there under its own name (name collisions get __1/__2 suffixes like "
        "backups do), the original is never touched and no backup is made, and unchanged sets are "
        "not written at all. With --apply-ops: the output set path, written only when every op "
        "applies; the input set is never touched. Refused if that path already exists.",
    )

    database = parser.add_argument_group("sample database (used by the fix-samples commands)")
    database.add_argument(
        "--db",
        "--database",
        action="store_true",
        default=False,
        help="Instead of parsing sets, create/update sample database for fast lookups when fixing broken paths. "
        "Files only a plugin loads, such as Kontakt instruments and Maschine groups, go to a plugin files "
        "database beside it. With no folders, scans sample_database.paths in config.yaml.",
    )
    database.add_argument(
        "--db-path",
        type=pathlib.Path,
        default=None,
        help="Sample database file to create/use. Default: sample_db.sqlite3 in the abletoolz config directory. "
        "The plugin files database, plugin_files.sqlite3, sits beside it.",
    )
    database.add_argument(
        "--db-backend",
        choices=("auto", "portable"),
        default="auto",
        help="Directory reader for --db (default: native on Windows/macOS, scandir on Linux).",
    )
    database.add_argument(
        "--db-workers",
        type=worker_argument,
        default=None,
        metavar="N|auto",
        help="Concurrent checksum readers for --db (default: available CPUs).",
    )
    database.add_argument(
        "--db-prune",
        action="store_true",
        default=False,
        help="Remove every file from the sample database that no longer exists, including files on a drive "
        "that isn't connected; --db over that drive adds them back.",
    )
    database.add_argument(
        "--search-samples",
        default=None,
        metavar="WORDS",
        help="Search the SQLite sample database by filename and folder words without reading audio files.",
    )

    plugin_tools = parser.add_argument_group("plugin tools (EXPERIMENTAL - reads plugin state inside sets)")
    plugin_tools.add_argument(
        "--dump-plugins",
        action="store_true",
        default=False,
        help="Dump plugin buffer data for reverse engineering. Shows raw hex, detected format, and "
        "attempts to decode buffer contents. Useful for mapping a new plugin.",
    )
    plugin_tools.add_argument(
        "--plugin-db",
        "--plugin-database",
        action="store_true",
        default=False,
        help="Instead of parsing sets, record this machine's plugins in a local plugin database: what "
        "Live's own plugin database lists, plus the class ids in plugin_database.uid_db and in the VST3 "
        "bundles in your plugin folders. `abletoolz plugins plan` reads it to know what is installed here. "
        "Takes no set and no folders; add extra folders under plugin_database.paths in config.yaml.",
    )
    plugin_tools.add_argument(
        "--plugin-db-path",
        type=pathlib.Path,
        default=None,
        help="Plugin database file to create/use. Default: plugin_db.json in the abletoolz config directory.",
    )
    plugin_tools.add_argument(
        "--list-plugins",
        action="store_true",
        default=False,
        help="List every plugin abletoolz knows: the formats it can read and write, whether it relinks files "
        "inside the plugin's state, and the converters from one plugin into another. Takes no set.",
    )

    authoring = parser.add_argument_group("authoring (LLM-facing describe/apply surface)")
    authoring.add_argument(
        "--describe",
        choices=[level.value for level in DescribeLevel],
        default=None,
        const=DescribeLevel.STRUCTURE.value,
        nargs="?",
        help="Print a tiered JSON description of the set to stdout: structure (tracks only), "
        "patterns (deduplicated clip content and placement), or full (patterns plus per-note nuance). "
        "Defaults to structure when given with no value.",
    )
    authoring.add_argument(
        "--apply-ops",
        type=pathlib.Path,
        default=None,
        metavar="OPS_JSON",
        help="Apply a batch of write operations (doc/LIBRARY.md describes the ops document) "
        "from a JSON file to the single input set given as srcs. Requires --output.",
    )

    records = parser.add_argument_group("records (machine-readable results)")
    records.add_argument(
        "--no-meta",
        dest="meta",
        action="store_false",
        default=True,
        help="Don't write the per-set sidecars. A run that checks or fixes plugins or samples "
        "normally leaves a <set stem>.meta.yaml (<clip stem>.alc.meta.yaml for a clip) beside every set "
        "it looked at, holding what it found next to your own status/notes, and reads it back next time: "
        "a plugin check of a set whose bytes have not changed since is not run again. Sample checks "
        "always look at the disk. The run report is written either way.",
    )

    processing = parser.add_argument_group("processing")
    processing.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=False,
        help="Adds extra verbosity, for some commands this will print more information.",
    )
    processing.add_argument(
        "-j",
        "--jobs",
        type=worker_argument,
        default=None,
        metavar="N|auto",
        help="Processes parsing independent sets (default: available CPUs, bounded by set count).",
    )

    args = parser.parse_args(argv)
    try:
        SetJob.from_args(args)
    except ValueError as error:
        parser.error(str(error))
    set_commands = any(getattr(args, name) for name in COMMAND_FLAGS + AUTHORING_FLAGS)
    if args.db and (args.list_plugins or args.plugin_db or set_commands):
        parser.error("--db/--database cannot be used with other commands!")
    if args.search_samples is not None and (
        args.db or args.list_plugins or args.plugin_db or set_commands or args.srcs
    ):
        parser.error("--search-samples searches the sample database; it takes no folders, sets, or other command.")
    if args.db_prune and (
        args.db or args.list_plugins or args.plugin_db or set_commands or args.srcs or args.search_samples is not None
    ):
        parser.error("--db-prune cleans the sample database; it takes no folders, sets, or other command.")
    if args.plugin_db and (args.list_plugins or set_commands or args.srcs):
        parser.error(
            "--plugin-db/--plugin-database reads this machine's plugins; it takes no set and no other command!"
        )
    if args.apply_ops and not args.output:
        parser.error("--apply-ops requires --output.")
    if args.output and not args.apply_ops and not args.save:
        parser.error("--output only makes sense with -s/--save or --apply-ops.")
    if args.output and args.fix_samples_collect:
        # Collecting copies into the set's own project, which --output promises not to touch.
        parser.error("--fix-samples-collect copies into each set's project; use --fix-samples-absolute with --output.")
    if args.apply_ops and len(args.srcs) != 1:
        parser.error("--apply-ops takes exactly one input set (not a directory).")
    # These answer questions about this machine, not a set, so none takes one;
    # --db without folders scans the ones in config.yaml.
    machine_command = args.db or args.db_prune or args.list_plugins or args.plugin_db or args.search_samples is not None
    if not machine_command and not args.srcs:
        parser.error("No sets or directories given, nothing to do.")
    return args


def run_build_plugin_db(db_path: pathlib.Path | None, config: AbletoolzConfig, locations: Locations) -> int:
    """Read this machine's plugins into the local plugin database.

    A machine command, not a set command: every plugin command wants the same
    answers, so they are read once and land in a file.
    """
    path = db_path if db_path is not None else locations.plugin_db
    console.render_plugin_db(plugin_db.create_or_update_db(config, locations, path), path)
    return 0


def run_apply_ops(args: argparse.Namespace) -> int:
    """Apply an ops document to the input set and write the result to --output as a new file.

    The input set is never touched, and --output is written only once every op
    ran and the tree encoded, so a failed run leaves nothing behind. An
    existing --output is refused rather than overwritten.
    """
    output_path: pathlib.Path = args.output
    if output_path.exists():
        logger.error("%sOutput %s already exists, refusing to overwrite.", R, output_path)
        return 1
    ableton_set = AbletonSet(pathlib.Path(args.srcs[0]))
    if not ableton_set.parse():
        return 1
    try:
        ops_document = OpsDocument.model_validate(json.loads(args.apply_ops.read_text(encoding="utf-8")))
        results = apply_ops(ableton_set, ops_document.ops)
        live_document.write_new(output_path, live_document.encode(ableton_set.root))
    # TypeError and LookupError are a value of the wrong kind for a setting, or a setting this set lacks.
    except (pydantic.ValidationError, ValueError, TypeError, LookupError, OSError) as exc:
        logger.error("%sCould not apply ops: %s", R, exc)
        return 1
    document = {
        "applied": len(results),
        "output": str(output_path),
        "results": [result.model_dump(mode="json") for result in results],
    }
    print(json.dumps(document, separators=(",", ":")))
    console.render_applied_ops(results)
    logger.info("%sApplied %s op(s) and saved to %s", G, len(results), output_path)
    return 0


def report_directory(args: argparse.Namespace) -> pathlib.Path:
    """Where the run report lands: beside what was processed, or in --output.

    With --output nothing at all may be written next to the originals, and a
    report is a write like any other.
    """
    if args.output is not None:
        return pathlib.Path(args.output)
    first = pathlib.Path(args.srcs[0])
    return first if first.is_dir() else first.parent


def process(args: argparse.Namespace, config: AbletoolzConfig, locations: Locations, argv: Sequence[str]) -> int:
    """Run the command ``args`` names and answer the exit code; ``argv`` is recorded in the run report."""
    db_path = args.db_path if args.db_path is not None else locations.sample_db
    if args.db_prune:
        database.prune_db(db_path)
        return 0
    if args.db:
        folders = args.srcs or [str(path) for path in config.sample_paths]
        if not folders:
            logger.error("%sNo folders given, and none under sample_database.paths in config.yaml.", R)
            return 1
        database.create_or_update_db(
            folders,
            db_path=db_path,
            backend=args.db_backend,
            workers=args.db_workers,
        )
        return 0
    db = None
    plugin_files_db = None
    if args.fix_samples_collect or args.fix_samples_absolute:
        logger.info("%sLoading db...", M)
        db = database.load_db(db_path)
        plugin_files_path = database.plugin_files_db_path(db_path)
        if plugin_files_path.exists():
            plugin_files_db = database.load_plugin_files_db(plugin_files_path)
        else:
            logger.info("%sNo plugin files database at %s; run --db to build it", Y, plugin_files_path)

    started = datetime.datetime.now().astimezone()
    try:
        pathlib_objects = find_sets(args.srcs)
        if not pathlib_objects:
            logger.info("%sError, no sets to process!", R)
            return 1
        try:
            jobs = batch.set_workers(args.jobs, len(pathlib_objects))
        except ValueError as error:
            logger.error("%s%s", R, error)
            return 2
        logger.info("%sUsing %s set worker(s)", C, jobs)
        job = SetJob.from_args(args)
        records = batch.run_sets(
            job,
            pathlib_objects,
            jobs=jobs,
            locations=locations,
            db=db,
            plugin_files_db=plugin_files_db,
            fingerprint=meta.scan_fingerprint(config, locations) if job.meta and job.scans else None,
            verbose=args.verbose,
        )
    finally:
        for opened in (db, plugin_files_db):
            if opened is not None:
                opened.close()
    if records is None:
        return 130
    run = report.build(records, command=list(argv), started=started)
    failed = [record.path for record in records if record.error is not None]
    logger.info(
        "%sTook %s to process %s set(s): %s%s ok%s, %s%s failed",
        CB,
        run.finished - started,
        len(pathlib_objects),
        G,
        len(pathlib_objects) - len(failed),
        CB,
        R if failed else G,
        len(failed),
    )
    logger.info("%sWrote the run report to %s%s", M, B, report.write(run, report_directory(args)))
    if failed:
        for path in failed:
            logger.info("%sFailed to process: %s", R, path)
        return 1
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    config: AbletoolzConfig | None = None,
    locations: Locations | None = None,
) -> int:
    """Run the command line ``argv`` (``sys.argv[1:]`` when None) and answer the exit code.

    ``locations`` stands in for this computer and ``config`` for config.yaml,
    each read only when it is None. A command line argparse refuses answers 2,
    the code argparse exits with.
    """
    argv = sys.argv[1:] if argv is None else list(argv)
    locations = Locations.this_computer() if locations is None else locations
    try:
        if argv[:1] == ["plugins"]:
            config = load_config(locations.config_file) if config is None else config
            return run_plugin_commands(argv[1:], config=config, locations=locations)
        args = parse_arguments(argv)
    except SystemExit as exited:  # argparse exits on --help (0) and on a command line it refuses (2)
        return exited.code if isinstance(exited.code, int) else 0

    machine_readable = bool(args.describe or args.apply_ops or args.search_samples is not None)
    console.configure_logging(machine_readable=machine_readable, verbose=args.verbose)
    if args.list_plugins:
        console.render_catalog(CATALOG)
        return 0
    if args.apply_ops:
        return run_apply_ops(args)
    if args.search_samples is not None:
        with database.load_db(args.db_path if args.db_path is not None else locations.sample_db) as catalog:
            print(json.dumps(catalog.search(args.search_samples), ensure_ascii=False))
        return 0
    config = load_config(locations.config_file) if config is None else config
    locations = locations.with_plugin_folders(config.plugin_paths)
    if args.plugin_db:
        return run_build_plugin_db(args.plugin_db_path, config, locations)
    return process(args, config, locations, argv)


if __name__ == "__main__":
    sys.exit(main())
