"""Cli entry point."""

import argparse
import datetime
import logging
import pathlib
import shutil
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from abletoolz import __version__, console
from abletoolz.live_set import AbletonSet
from abletoolz.misc import BACKUP_DIR, CB, B, C, ElementNotFound, G, M, R, SetError, Y
from abletoolz.plugin_parsers import get_all_parsers, load_config
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
)


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
        help="Checks plugin VST paths and verifies they exists. Note: If Ableton finds the "
        "plugin name in a different path it will automatically update these paths the next time "
        "you save your project, so take it with a grain of salt. AU are not stored as paths in "
        "sets but abbreviated component names. Might possibly add support for them later.",
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
        "--dump-plugins",
        action="store_true",
        default=False,
        help="Dump plugin buffer data for reverse engineering. Shows raw hex, detected format, and "
        "attempts to decode buffer contents. Useful for developing new plugin parsers.",
    )
    plugin_tools.add_argument(
        "--list-parsers",
        action="store_true",
        default=False,
        help="List all registered plugin parsers and their buffer formats.",
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
    if args.db and (args.list_parsers or any(getattr(args, name) for name in EDIT_FLAGS + ANALYSIS_FLAGS)):
        parser.error("--db/--database cannot be used with other commands!")
    if not args.list_parsers and not args.srcs:
        parser.error("No sets or directories given, nothing to do.")
    return args


def process_set(args: argparse.Namespace, pathlib_obj: pathlib.Path, db: dict | None, config) -> int:
    """Process individual set."""
    logger.info("%sParsing: %s", C, pathlib_obj)
    ableton_set = AbletonSet(pathlib_obj)
    if not ableton_set.parse():
        return -2
    ableton_set.load_version()
    logger.info("%sSet name: %s, %sBPM: %s", C, pathlib_obj.stem, B, ableton_set.transport.bpm())
    ableton_set.find_project_root_folder()
    console.render_length(ableton_set.transport.length())

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

    if args.check_samples:
        console.render_missing_samples(ableton_set.samples.check())

    if args.fix_samples_absolute or args.fix_samples_collect:
        assert db is not None
        ableton_set.samples.fix(db, collect_and_save=args.fix_samples_collect)

    if args.check_plugins:
        console.render_plugins(ableton_set.plugins.scan([]))

    if args.analyze_plugins:
        analyses = ableton_set.plugins.analyze(config)
        issues_found = sum(1 for a in analyses if a.issues)
        fixable = sum(1 for a in analyses if a.can_fix)
        logger.info(
            "%sPlugin analysis complete: %s plugins analyzed, %s%s with issues%s, %s%s fixable%s",
            C, len(analyses),
            Y if issues_found else G, issues_found, C,
            G if fixable else Y, fixable, C,
        )

    if args.dump_plugins:
        logger.info("%sDumping plugin buffer data for reverse engineering...", C)
        ableton_set.plugins.dump()

    if args.fix_plugins:
        if db is None:
            logger.info("%sNo database loaded; run with --db first to build sample DB.", Y)
        else:
            ableton_set.plugins.fix(db, config)

    if args.upgrade_plugins:
        ableton_set.plugins.upgrade()

    if args.xml:
        ableton_set.save_xml()
    if args.save:
        # if backup == ableton_set:
        #     logger.info("%sSet has no changes from originally, not saving...", MB)
        #     return 0
        ableton_set.save_set(append_bars_bpm=args.append_bars_bpm, prepend_version=args.prepend_version)
    elif any(getattr(args, name) for name in EDIT_FLAGS):
        logger.info("%sNo changes saved, use -s/--save option to write changes to file.", Y)
    return 0


def list_parsers() -> None:
    """Print all registered plugin parsers and their buffer formats."""
    parsers = get_all_parsers()
    logger.info("%sRegistered plugin parsers:%s", C, "")
    for _name, parser_cls in parsers.items():
        p = parser_cls()
        logger.info(
            "  %s%s%s: %s (buffer format: %s%s%s)",
            G, p.name, C, p.description, M, p.buffer_format.value, C
        )


def process(args: argparse.Namespace) -> int:
    """Process arguments.

    Args:
        args: argparse.Namespace with parsed arguments.

    Returns:
        integer with exit code, zero indicating success, non-zero indicating error.

    """
    # Load config
    config = load_config()

    if args.db:
        create_db.create_or_update_db(args.srcs, db_path=args.db_path)
        return 0
    db = None
    if args.fix_samples_collect or args.fix_samples_absolute or args.fix_plugins:
        logger.info("%sLoading db...", M)
        db = create_db.load_db(args.db_path)

    start_time = time.time()
    pathlib_objects = get_pathlib_objects(srcs=args.srcs)
    if not pathlib_objects:
        logger.info("%sError, no sets to process!", R)
        return 1

    # Concurrent processing of sets
    jobs = args.jobs if isinstance(args.jobs, int) and args.jobs > 0 else min(8, max(1, len(pathlib_objects)))
    logger.info("%sUsing %s worker(s)", C, jobs)

    def _worker(path_obj: pathlib.Path) -> int:
        try:
            return process_set(args, path_obj, db, config)
        except (ElementNotFound, SetError):
            logger.info(traceback.format_exc())
            return -1
        finally:
            columns = shutil.get_terminal_size(fallback=(120, 20)).columns
            logger.info("%s\n\n%s\n\n", M, "^" * columns)

    executor = ThreadPoolExecutor(max_workers=jobs)
    try:
        futures = {executor.submit(_worker, p): p for p in pathlib_objects}
        failed = [futures[future] for future in as_completed(futures) if future.result() != 0]
    except KeyboardInterrupt:
        # Cancel everything still queued; sets already being processed finish
        # so no write is killed halfway.
        logger.error("%sInterrupted! Cancelling queued sets, letting in-flight sets finish...", R)
        executor.shutdown(wait=True, cancel_futures=True)
        return 130
    executor.shutdown()
    logger.info(
        "%sTook %s to process %s set(s): %s%s ok%s, %s%s failed",
        CB,
        datetime.timedelta(seconds=time.time() - start_time),
        len(pathlib_objects),
        G,
        len(pathlib_objects) - len(failed),
        CB,
        R if failed else G,
        len(failed),
    )
    if failed:
        for path in failed:
            logger.info("%sFailed to process: %s", R, path)
        return 1
    return 0


def main() -> None:
    """Entry point to cli."""
    args = parse_arguments()

    logging.getLogger("colormath").setLevel(logging.WARNING)
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
        datefmt="%H:%M:%S",
    )
    if args.list_parsers:
        list_parsers()
        sys.exit(0)
    sys.exit(process(args))


if __name__ == "__main__":
    main()
