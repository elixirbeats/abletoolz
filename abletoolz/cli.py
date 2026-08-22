"""Cli entry point."""

import argparse
import datetime
import json
import logging
import pathlib
import shutil
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import pydantic

from abletoolz import __version__, console, meta, report
from abletoolz.live_set import AbletonSet
from abletoolz.live_set.apply_ops import OpsDocument, apply_ops
from abletoolz.live_set.describe import DescribeLevel, describe_json
from abletoolz.misc import BACKUP_DIR, CB, B, C, ElementNotFound, G, M, R, SetError, Y
from abletoolz.plugin_parsers import (
    AbletoolzConfig,
    default_suggestions_path,
    get_all_parsers,
    load_config,
    plugin_db,
    render_targets_yaml,
    survey_machine,
)
from abletoolz.sample_databaser import create_db

logger = logging.getLogger(__name__)

# Flags that edit sets. Shared by the --db exclusivity check and the unsaved-changes
# reminder in process_set, so a new edit flag only needs registering here.
EDIT_FLAGS = (
    "analyze_plugins",
    "append_bars_bpm",
    "cue_out",
    "fix_plugins",
    "fix_samples_absolute",
    "fix_samples_collect",
    "fold",
    "gradient_tracks",
    "master_out",
    "prepend_version",
    "repair_plugins",
    "set_track_heights",
    "set_track_widths",
    "unfold",
    "upgrade_plugins",
)

# Flags that read or export without editing the set.
ANALYSIS_FLAGS = (
    "check_plugins",
    "check_samples",
    "dump_plugins",
    "list_tracks",
    "save",
    "xml",
    "describe",
)

# --apply-ops writes through its own explicit --output contract, bypassing
# process_set/-s entirely; still exclusive with --db like every other command.
AUTHORING_FLAGS = ("apply_ops",)

# Flags whose work is what a sidecar records. A run using any of them leaves a
# <set stem>.meta.yaml beside every set it looked at, unless --no-meta says not
# to, and reads that file back on the next pass to skip work it already did.
SCAN_FLAGS = (
    "analyze_plugins",
    "check_plugins",
    "check_samples",
    "fix_plugins",
    "fix_samples_absolute",
    "fix_samples_collect",
    "repair_plugins",
    "upgrade_plugins",
)

# Flags that rewrite devices, which is what lets a sidecar say "fixed nothing"
# rather than "never tried".
FIX_FLAGS = ("fix_plugins", "repair_plugins", "upgrade_plugins")


def get_pathlib_objects(srcs: list[str]) -> list[pathlib.Path]:
    """Get all ableton sets to parse.

    Args:
        srcs: path or paths to directories and set files.

    Returns:
        list of pathlib.Paths with all ableton sets to parse, excluding backup directories.

    """
    paths: list[pathlib.Path] = []
    for src in srcs:
        path = pathlib.Path(src)
        if path.is_dir():
            files = list(path.rglob("*.als")) + list(path.rglob("*.alc"))
            # Hacky but Path.rglob doesn't have options for filtering.
            files_to_process = []
            for file in files:
                if all(x not in file.parts[:-1] for x in ["Backup", "backup", BACKUP_DIR]) and not file.stem.startswith(
                    "._"
                ):
                    files_to_process.append(file)
            paths.extend(files_to_process)
        elif path.is_file():
            paths.append(path)
    return paths


def is_valid_dir_path(path: str) -> str:
    """Check if the path is valid.

    Mainly for windows, which uses backslashes instead and this causes problems for parsing command line arguments since
    backslash is used for escaping.
    """
    if sys.platform.startswith("win") and '"' in path:
        raise argparse.ArgumentTypeError(
            f"{R}Windows paths must not end in backslash: \n'C:\\somepath\\'(BAD)\n'C:\\somepath' "
            f"(GOOD)\nThis is due to a bug in how Windows handles backslashes before quotes."
        )
    return path


def parse_arguments() -> argparse.Namespace:
    """Get command line arguments."""
    parser = argparse.ArgumentParser(description=f"abletoolz {__version__}", add_help=True)

    # Input arguments.
    parser.add_argument(
        "srcs",
        action="store",
        nargs="*",
        type=is_valid_dir_path,
        help="Set(s) or directory(ies). All sub folders in directories are parsed for sets. NOTE: On WINDOWS remove "
        "the trailing backslash when processing folders! This is due to how windows and python interact with "
        "backslashes, which are normally escape characters.",
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
        help="Checks relative and absolute sample paths and verifies if sample exists there.",
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
        help="Finds missing samples and fixes the broken references in your ableton sets. Does not copy sample into "
        "project folder. Run --db on folders first "
        "to create your database.",
    )
    editing.add_argument(
        "--fix-samples-collect",
        action="store_true",
        default=False,
        help="Collects and saves missing samples into the set's project folder, the same as collect and save in "
        "Ableton. Run --db on folders first to create your database.",
    )
    editing.add_argument(
        "--gradient-tracks",
        action="store_true",
        default=False,
        help="Generate a random gradient over the tracks and color them. Ableton has a very limited set of available "
        "colors, so the results are limited, but you still can get some decent results. This uses the CIE2000 "
        "algorithm which helps create a gradient more natural to the human eye.",
    )

    saving = parser.add_argument_group("saving")
    saving.add_argument(
        "-s",
        "--save",
        action="store_true",
        default=False,
        help="Saves file after parsing. This is only put here as a safety check, to make sure you know "
        "what you are doing! The original file is always renamed to "
        f"set_directory/{BACKUP_DIR}/set_name_xx.als, where xx will automatically increase to "
        "to avoid overwriting files.",
    )
    saving.add_argument(
        "-x",
        "--xml",
        action="store_true",
        default=False,
        help="dump the xml in same directory as set_name.xml(Useful to understand set structure).",
    )
    saving.add_argument(
        "--append-bars-bpm",
        action="store_true",
        default=False,
        help="Append furthest bar length and bpm to filename to help organize your set collection. "
        "For example, my_set.als --> my_set_32bars_90.00bpm.als Option only works with -s/--save",
    )
    saving.add_argument(
        "--prepend-version",
        action="store_true",
        default=False,
        help="Appends set version to set filename",
    )
    saving.add_argument(
        "--output",
        type=pathlib.Path,
        default=None,
        metavar="PATH",
        help="Write somewhere else instead of in place. With -s/--save: a directory; every set the "
        "run changes is saved there under its own name (name collisions get __1/__2 suffixes like "
        "backups do), the original is never touched and no backup is made, and unchanged sets are "
        "not written at all. With --apply-ops: the output set path; the input set is copied there "
        "first, then edited in place. Refused if that path already exists.",
    )

    database = parser.add_argument_group("sample database (used by the fix-samples commands)")
    database.add_argument(
        "--db",
        "--database",
        action="store_true",
        default=False,
        help="Instead of parsing sets, create/update sample database for fast lookups when fixing broken paths.",
    )
    database.add_argument(
        "--db-path",
        type=pathlib.Path,
        default=None,
        help="Sample database file to create/use. Default: sample_db.json in the abletoolz config directory.",
    )

    plugin_tools = parser.add_argument_group("plugin tools (EXPERIMENTAL - reads plugin state inside sets)")
    plugin_tools.add_argument(
        "--analyze-plugins",
        action="store_true",
        default=False,
        help="Deep analysis of plugins using registered parsers. Shows issues like missing samples in "
        "Serato Sample, etc. More detailed than --check-plugins.",
    )
    plugin_tools.add_argument(
        "--fix-plugins",
        action="store_true",
        default=False,
        help="Fix supported plugin states (e.g. Serato Sample missing samples) using the sample database. "
        "Uses registered plugin parsers. Save with -s to write changes.",
    )
    plugin_tools.add_argument(
        "--upgrade-plugins",
        action="store_true",
        default=False,
        help="Upgrade plugin paths when possible (e.g. 32-bit to 64-bit/VST3). Save with -s to write changes.",
    )
    plugin_tools.add_argument(
        "--repair-plugins",
        action="store_true",
        default=False,
        help="Replace the plugin devices this machine can no longer load with what your mappings say "
        "they become, keeping their saved patch. Devices that still load are left alone. Mappings come "
        "from the seed table and plugin_translation.targets in config.yaml, and each mapping says which "
        "format it points at -- there is nothing to choose here. An entry may give just a name and let "
        "the class id be looked up in the plugin database. Save with -s to write changes.",
    )
    plugin_tools.add_argument(
        "--dump-plugins",
        action="store_true",
        default=False,
        help="Dump plugin buffer data for reverse engineering. Shows raw hex, detected format, and "
        "attempts to decode buffer contents. Useful for developing new plugin parsers.",
    )
    plugin_tools.add_argument(
        "--plugin-db",
        "--plugin-database",
        action="store_true",
        default=False,
        help="Instead of parsing sets, read every plugin installed on this machine into a local plugin "
        "database: Live's own plugin database, your plugin folders, the class ids inside installed VST3 "
        "bundles. Repair looks class ids up in it and --suggest-plugin-mappings reads it. Takes no set "
        "and no folders; add extra folders under plugin_database.paths in config.yaml.",
    )
    plugin_tools.add_argument(
        "--plugin-db-path",
        type=pathlib.Path,
        default=None,
        help="Plugin database file to create/use. Default: plugin_db.json in the abletoolz config directory.",
    )
    plugin_tools.add_argument(
        "--suggest-plugin-mappings",
        type=pathlib.Path,
        default=None,
        const=default_suggestions_path(),
        nargs="?",
        metavar="OUTPUT_YAML",
        help="Read the local plugin database and write the mappings it suggests to a YAML file for you "
        "to review and merge into config.yaml yourself. Every suggestion comes out commented; you "
        "enable one by uncommenting it. Takes no set: it reads the machine, not a project. Defaults to "
        f"{default_suggestions_path()}.",
    )
    plugin_tools.add_argument(
        "--list-parsers",
        action="store_true",
        default=False,
        help="List all registered plugin parsers and their buffer formats.",
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
        help="Apply a batch of write operations (see doc/ or apply_ops.py for the ops document shape) "
        "from a JSON file to the single input set given as srcs. Requires --output.",
    )

    records = parser.add_argument_group("records (machine-readable results)")
    records.add_argument(
        "--no-meta",
        dest="meta",
        action="store_false",
        default=True,
        help="Don't write the per-set sidecars. A run that checks or fixes plugins or samples "
        "normally leaves a <set stem>.meta.yaml beside every set it looked at, holding what it "
        "found next to your own status/notes, and reads it back next time: a set whose bytes have "
        "not changed since is not scanned again. The run report is written either way.",
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
        "--jobs",
        type=int,
        default=None,
        help="Number of threads to process sets concurrently (default: auto)",
    )

    args = parser.parse_args()
    if args.fix_samples_absolute and args.fix_samples_collect:
        parser.error("Can only use --fix-samples-collect or --fix-samples-absolute, not both!")
    if args.fold and args.unfold:
        parser.error("Only set unfold or fold, not both.")
    set_commands = any(getattr(args, name) for name in EDIT_FLAGS + ANALYSIS_FLAGS + AUTHORING_FLAGS)
    if args.db and (args.list_parsers or args.plugin_db or args.suggest_plugin_mappings is not None or set_commands):
        parser.error("--db/--database cannot be used with other commands!")
    if args.plugin_db and (args.list_parsers or args.suggest_plugin_mappings is not None or set_commands or args.srcs):
        parser.error(
            "--plugin-db/--plugin-database reads this machine's plugins; it takes no set and no other command!"
        )
    if args.apply_ops and not args.output:
        parser.error("--apply-ops requires --output.")
    if args.output and not args.apply_ops and not args.save:
        parser.error("--output only makes sense with -s/--save or --apply-ops.")
    if args.apply_ops and len(args.srcs) != 1:
        parser.error("--apply-ops takes exactly one input set (not a directory).")
    # --list-parsers, --plugin-db and --suggest-plugin-mappings all answer
    # questions about this machine rather than about a set, so none takes one.
    machine_command = args.list_parsers or args.plugin_db or args.suggest_plugin_mappings is not None
    if not machine_command and not args.srcs:
        parser.error("No sets or directories given, nothing to do.")
    return args


def process_set(
    args: argparse.Namespace,
    pathlib_obj: pathlib.Path,
    db: create_db.DatabaseT | None,
    config: AbletoolzConfig,
) -> report.SetRecord:
    """Process individual set, answering what the run did to it.

    Every finding the run makes is recorded on the way past rather than
    recomputed at the end: the record and the console are two readings of the
    same scan, the same repair report, the same sample check.
    """
    logger.info("%sParsing: %s", C, pathlib_obj)
    ableton_set = AbletonSet(pathlib_obj)
    if not ableton_set.parse():
        return report.SetRecord(path=str(pathlib_obj), error="could not read the set")
    # An --output sweep only writes sets the run actually changed and the report
    # says which those were, so remember how the tree serialized before any edit
    # flag touched it.
    editing = any(getattr(args, name) for name in EDIT_FLAGS)
    pristine_xml = ableton_set.generate_xml() if editing else None
    ableton_set.load_version()
    logger.info("%sSet name: %s, %sBPM: %s", C, pathlib_obj.stem, B, ableton_set.transport.bpm())
    ableton_set.find_project_root_folder()
    length = ableton_set.transport.length()
    console.render_length(length)

    # The sidecar answers for these exact bytes or not at all, so the hash comes
    # off the file as it was read, before any edit below.
    digest = meta.file_hash(pathlib_obj) if args.meta else None
    cached = meta.cached_scan(pathlib_obj, digest) if digest is not None else None
    fixes: list[report.DeviceFix] = []
    refusals: list[report.Refusal] = []
    plugins_missing: dict[str, int] | None = None
    samples_missing: dict[str, int] | None = None

    if args.master_out:
        ableton_set.tracks.set_audio_output(args.master_out, element_string="MasterTrack")
    if args.cue_out:
        ableton_set.tracks.set_audio_output(args.cue_out, element_string="PreHearTrack")
    if args.fold:
        ableton_set.tracks.fold()
    elif args.unfold:
        ableton_set.tracks.unfold()
    if args.set_track_heights:
        ableton_set.tracks.set_heights(args.set_track_heights)
    if args.set_track_widths:
        ableton_set.tracks.set_widths(args.set_track_widths)
    if args.gradient_tracks:
        ableton_set.tracks.gradient()

    if args.list_tracks:
        console.render_tracks(ableton_set.tracks.load())

    if args.describe:
        print(describe_json(ableton_set, DescribeLevel(args.describe)))

    if args.check_samples:
        if cached is not None and cached.samples_missing_by_name is not None:
            samples_missing = cached.samples_missing_by_name
            console.render_cached_samples(samples_missing)
        else:
            missing_samples = ableton_set.samples.check()
            console.render_missing_samples(missing_samples)
            samples_missing = report.count_names(ref.name for ref in missing_samples)

    if args.fix_samples_absolute or args.fix_samples_collect:
        assert db is not None
        ableton_set.samples.fix(db, collect_and_save=args.fix_samples_collect)

    if args.check_plugins:
        if cached is not None and cached.plugins_missing is not None:
            plugins_missing = cached.plugins_missing
            console.render_cached_plugins(plugins_missing)
        else:
            refs = ableton_set.plugins.scan([])
            console.render_plugins(refs)
            plugins_missing = report.scan_missing(refs)

    if args.analyze_plugins:
        analyses = ableton_set.plugins.analyze(config)
        issues_found = sum(1 for a in analyses if a.issues)
        fixable = sum(1 for a in analyses if a.can_fix)
        logger.info(
            "%sPlugin analysis complete: %s plugins analyzed, %s%s with issues%s, %s%s fixable%s",
            C,
            len(analyses),
            Y if issues_found else G,
            issues_found,
            C,
            G if fixable else Y,
            fixable,
            C,
        )

    if args.dump_plugins:
        logger.info("%sDumping plugin buffer data for reverse engineering...", C)
        ableton_set.plugins.dump()

    if args.fix_plugins:
        if db is None:
            logger.info("%sNo database loaded; run with --db first to build sample DB.", Y)
        else:
            fixes.extend(report.parser_fixes(ableton_set.plugins.fix(db, config)))

    if args.upgrade_plugins:
        fixes.extend(report.upgrade_fixes(ableton_set.plugins.upgrade()))

    if args.repair_plugins:
        repaired = ableton_set.plugins.repair()
        console.render_repair(repaired)
        fixes.extend(report.repair_fixes(repaired))
        refusals.extend(report.repair_refusals(repaired))
        if plugins_missing is None:
            # Only when --check-plugins did not already answer it: a repair pass
            # asks the same question of every device it can act on.
            plugins_missing = report.repair_missing(repaired)

    if args.xml:
        ableton_set.save_xml()
    changed = pristine_xml is not None and ableton_set.generate_xml() != pristine_xml
    saved = False
    if args.save:
        if args.output is not None and not changed:
            logger.info("%sSet is unchanged, nothing written to %s", Y, args.output)
        else:
            ableton_set.save_set(
                append_bars_bpm=args.append_bars_bpm,
                prepend_version=args.prepend_version,
                output_dir=args.output,
            )
            saved = True
    elif editing:
        logger.info("%sNo changes saved, use -s/--save option to write changes to file.", Y)

    if digest is not None and any(getattr(args, name) for name in SCAN_FLAGS):
        scan = meta.SetScan(
            scanned=meta.now(),
            scanned_with=meta.SCANNER,
            set_hash=digest,
            live_version=ableton_set.version,
            bars=length.bars,
            bpm=length.bpm,
            plugins_missing=plugins_missing,
            plugins_fixed=report.fix_counts(fixes) if any(getattr(args, name) for name in FIX_FLAGS) else None,
            samples_missing=None if samples_missing is None else sum(samples_missing.values()),
            samples_missing_by_name=samples_missing,
        )
        if cached is not None:
            scan = meta.carry_forward(cached, scan)
        # With --output nothing may be written next to the original, so the
        # sidecar goes with the copy or nowhere. ableton_set.path is where the
        # set actually landed, suffixes, renames and all.
        if args.output is None or saved:
            meta.write(ableton_set.path, scan, human_source=pathlib_obj)

    return report.SetRecord(
        path=str(pathlib_obj),
        written=str(ableton_set.path) if saved else None,
        changed=changed,
        fixes=fixes,
        refusals=refusals,
        plugins_missing=plugins_missing,
        samples_missing=None if samples_missing is None else sum(samples_missing.values()),
    )


def list_parsers() -> None:
    """Print all registered plugin parsers and their buffer formats."""
    parsers = get_all_parsers()
    logger.info("%sRegistered plugin parsers:%s", C, "")
    for _name, parser_cls in parsers.items():
        p = parser_cls()
        logger.info("  %s%s%s: %s (buffer format: %s%s%s)", G, p.name, C, p.description, M, p.buffer_format.value, C)


def run_build_plugin_db(db_path: pathlib.Path | None) -> int:
    """Read this machine's plugins into the local plugin database.

    A machine command, not a set command, and the plugin half of ``--db``: the
    scan is slow and every plugin command wants the same answers, so it happens
    once and lands in a file.
    """
    path = db_path if db_path is not None else plugin_db.default_plugin_db_path()
    console.render_plugin_db(plugin_db.create_or_update_db(load_config(), path), path)
    return 0


def run_suggest_mappings(output: pathlib.Path, db_path: pathlib.Path | None) -> int:
    """Survey this machine's plugins and write suggested mappings to ``output``.

    A machine command, not a set command: it reads the local plugin database and
    writes a file the user reviews. It never touches config.yaml, because a
    mapping nobody checked is a device that loads as something else.
    """
    report = survey_machine(load_config(), db_path=db_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_targets_yaml(report), encoding="utf-8")
    console.render_mapping_suggestions(report, output)
    return 0


def run_apply_ops(args: argparse.Namespace) -> int:
    """Copy the input set to --output, apply an ops document to the copy, and save it.

    Refuses an existing --output rather than overwriting it: the copy step is
    the only backup an apply-ops run gets, so it has to land somewhere new.
    """
    output_path: pathlib.Path = args.output
    if output_path.exists():
        logger.error("%sOutput %s already exists, refusing to overwrite.", R, output_path)
        return 1
    input_path = pathlib.Path(args.srcs[0])
    shutil.copy2(input_path, output_path)

    ableton_set = AbletonSet(output_path)
    if not ableton_set.parse():
        return -2
    try:
        ops_document = OpsDocument.model_validate(json.loads(args.apply_ops.read_text(encoding="utf-8")))
        results = apply_ops(ableton_set, ops_document.ops)
    except (pydantic.ValidationError, ValueError, OSError) as exc:
        logger.error("%sCould not apply ops: %s", R, exc)
        return 1
    ableton_set.save_set()
    print(json.dumps({"applied": len(results), "output": str(output_path), "results": results}, separators=(",", ":")))
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


def process(args: argparse.Namespace) -> int:
    """Process arguments.

    Args:
        args: argparse.Namespace with parsed arguments.

    Returns:
        integer with exit code, zero indicating success, non-zero indicating error.

    """
    if args.apply_ops:
        return run_apply_ops(args)

    # Load config
    config = load_config()

    if args.db:
        create_db.create_or_update_db(args.srcs, db_path=args.db_path)
        return 0
    db = None
    if args.fix_samples_collect or args.fix_samples_absolute or args.fix_plugins:
        logger.info("%sLoading db...", M)
        db = create_db.load_db(args.db_path)

    started = datetime.datetime.now().astimezone()
    pathlib_objects = get_pathlib_objects(srcs=args.srcs)
    if not pathlib_objects:
        logger.info("%sError, no sets to process!", R)
        return 1

    # Concurrent processing of sets
    jobs = args.jobs if isinstance(args.jobs, int) and args.jobs > 0 else min(8, max(1, len(pathlib_objects)))
    logger.info("%sUsing %s worker(s)", C, jobs)

    def _worker(path_obj: pathlib.Path) -> report.SetRecord:
        try:
            return process_set(args, path_obj, db, config)
        except (ElementNotFound, SetError) as error:
            logger.info(traceback.format_exc())
            return report.SetRecord(path=str(path_obj), error=f"{type(error).__name__}: {error}")
        finally:
            columns = shutil.get_terminal_size(fallback=(120, 20)).columns
            logger.info("%s\n\n%s\n\n", M, "^" * columns)

    executor = ThreadPoolExecutor(max_workers=jobs)
    try:
        futures = {executor.submit(_worker, p): p for p in pathlib_objects}
        records = [future.result() for future in as_completed(futures)]
    except KeyboardInterrupt:
        # Cancel everything still queued; sets already being processed finish
        # so no write is killed halfway.
        logger.error("%sInterrupted! Cancelling queued sets, letting in-flight sets finish...", R)
        executor.shutdown(wait=True, cancel_futures=True)
        return 130
    executor.shutdown()
    run = report.build(records, command=sys.argv[1:], started=started)
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


def main() -> None:
    """Entry point to cli."""
    args = parse_arguments()

    logging.getLogger("colormath").setLevel(logging.WARNING)
    # --describe and --apply-ops write a machine-readable document to stdout, so
    # everything a human reads -- banners, progress, warnings -- moves to stderr
    # and a consumer can pipe stdout straight into a JSON parser.
    machine_readable = bool(args.describe or args.apply_ops)
    logging.basicConfig(
        stream=sys.stderr if machine_readable else sys.stdout,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
        datefmt="%H:%M:%S",
    )
    if args.list_parsers:
        list_parsers()
        sys.exit(0)
    if args.plugin_db:
        sys.exit(run_build_plugin_db(args.plugin_db_path))
    if args.suggest_plugin_mappings is not None:
        sys.exit(run_suggest_mappings(args.suggest_plugin_mappings, args.plugin_db_path))
    sys.exit(process(args))


if __name__ == "__main__":
    main()
