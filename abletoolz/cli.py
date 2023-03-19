"""Cli entry point."""

import argparse
import copy
import datetime
import logging
import os
import pathlib
import sys
import time
import traceback
from typing import Dict, List, Optional

from abletoolz import __version__
from abletoolz.ableton_set import AbletonSet
from abletoolz.misc import BACKUP_DIR, CB, MB, B, C, ElementNotFound, M, R, Y
from abletoolz.sample_databaser import create_db

logger = logging.getLogger(__name__)


def get_pathlib_objects(srcs: List[str]) -> List[pathlib.Path]:
    """Get all ableton sets to parse.

    Args:
        srcs: path or paths to directories and set files.

    Returns:
        list of pathlib.Paths with all ableton sets to parse, excluding backup directories.
    """
    paths: List[pathlib.Path] = []
    for src in srcs:
        path = pathlib.Path(src)
        if path.is_dir():
            files = list(path.rglob("*.als"))
            # Hacky but Path.rglob doesn't have options for filtering.
            files_to_process = []
            for file in files:
                if all(x not in file.parts[:-1] for x in ["Backup", "backup", BACKUP_DIR]) and not file.stem.startswith(
                    ("._")
                ):
                    files_to_process.append(file)
            paths.extend(files_to_process)
        elif path.is_file():
            paths.append(path)
    return paths


def is_valid_dir_path(path: str) -> str:
    """Check if the path is a valid.

    Mainly for windows, which uses backslashes instead and this causes problems for parsing command line arguments since
    backslash is used for escaping.
    """
    if sys.platform.startswith("win") and '"' in path:
        raise Exception(
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
        help="Set(s) or directory(ies). All sub folders in directories are parsed for sets. NOTE: On WINDOWS remove "
        "the trailing backslash when processing folders! This is due to how windows and python interact with "
        "backslashes, which are normally escape characters.",
    )
    parser.add_argument(
        "--db",
        "--database",
        action="store_true",
        default=False,
        help="Instead of parsing sets, create/update sample database for fast lookups when fixing broken paths.",
    )

    # Output arguments.
    parser.add_argument(
        "-x",
        "--xml",
        action="store_true",
        default=False,
        help="dump the xml in same directory as set_name.xml(Useful to understand set structure).",
    )
    parser.add_argument(
        "-s",
        "--save",
        action="store_true",
        default=False,
        help="Saves file after parsing. This is only put here as a safety check, to make sure you know "
        "what you are doing! The original file is always renamed to "
        f"set_directory/{BACKUP_DIR}/set_name_xx.als, where xx will automatically increase to "
        "to avoid overwriting files.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=False,
        help="Adds extra verbosity, for some commands this will print more information.",
    )

    parser.add_argument(
        "--append-bars-bpm",
        action="store_true",
        default=False,
        help="Append furthest bar length and bpm to filename to help organize your set collection. "
        "For example, my_set.als --> my_set_32bars_90.00bpm.als Option only works with -s/--save",
    )
    parser.add_argument(
        "--prepend-version",
        action="store_true",
        default=False,
        help="Appends set version to set filename",
    )

    # Edit set arguments.
    parser.add_argument(
        "--unfold",
        action="store_true",
        default=False,
        help="unfolds all tracks/automation lanes in arrangement.",
    )
    parser.add_argument(
        "--fold",
        action="store_true",
        default=False,
        help="folds all tracks/automation lanes in arrangement.",
    )
    parser.add_argument("--set-track-heights", type=int, help="Set arrangement track heights")
    parser.add_argument("--set-track-widths", type=int, help="Set clip view track width.")
    parser.add_argument(
        "--cue-out",
        type=int,
        help="set Cue audio output tracks. Set to 1 for stereo 1/2, 2 for 3/4 etc",
    )
    parser.add_argument(
        "--master-out",
        type=int,
        help="number to set Master audio output tracks to. Same numbers as --cue-out",
    )
    parser.add_argument(
        "--fix-samples-absolute",
        action="store_true",
        default=False,
        help="Finds missing samples and fixes the broken references in your ableton sets. Does not copy sample into "
        "project folder. Run --db on folders first "
        "to create your database.",
    )
    parser.add_argument(
        "--fix-samples-collect",
        action="store_true",
        default=False,
        help="Collects and saves missing samples into the set's project folder, the same as collect and save in "
        "Ableton. Run --db on folders first to create your database.",
    )

    # Analysis arguments.
    parser.add_argument(
        "--list-tracks",
        action="store_true",
        default=False,
        help="Load and list all track information.",
    )
    parser.add_argument(
        "--check-samples",
        action="store_true",
        default=False,
        help="Checks relative and absolute sample paths and verifies if sample exists there.",
    )

    parser.add_argument(
        "--check-plugins",
        action="store_true",
        default=False,
        help="Checks plugin VST paths and verifies they exists. Note: If Ableton finds the "
        "plugin name in a different path it will automatically update these paths the next time "
        "you save your project, so take it with a grain of salt. AU are not stored as paths in "
        "sets but abbreviated component names. Might possibly add support for them later.",
    )

    args = parser.parse_args()
    assert not (
        args.fix_samples_absolute and args.fix_samples_collect
    ), "Can only use --fix-samples-collect or --fix-samples-absolute, not both!"
    assert not (args.fold and args.unfold), "Only set unfold or fold, not both."
    assert not (
        args.db
        and any(
            [
                # TODO(add rest of commands.)
                args.save,
                args.list_tracks,
                args.check_plugins,
                args.master_out,
                args.cue_out,
                args.fold,
                args.unfold,
                args.set_track_heights,
                args.set_track_widths,
                args.fix_samples,
            ]
        )
    ), "--db/--database cannot be used with other commands!"
    return args


def process_set(args: argparse.Namespace, pathlib_obj: pathlib.Path, db: Optional[Dict]) -> int:
    """Process individual set."""
    logger.info("%sParsing: %s", C, pathlib_obj)
    ableton_set = AbletonSet(pathlib_obj)
    if not ableton_set.parse():
        return -2
    ableton_set.load_version()
    logger.info("%sSet name: %s, %sBPM: %s", C, pathlib_obj.stem, B, ableton_set.get_bpm())
    ableton_set.find_project_root_folder()
    ableton_set.find_furthest_bar()
    ableton_set.estimate_length()
    if args.save:
        backup = copy.deepcopy(ableton_set)

    if args.master_out:
        ableton_set.set_audio_output(args.master_out, element_string="MasterTrack")
    if args.cue_out:
        ableton_set.set_audio_output(args.cue_out, element_string="PreHearTrack")
    if args.fold:
        ableton_set.fold_tracks()
    elif args.unfold:
        ableton_set.unfold_tracks()
    if args.set_track_heights:
        ableton_set.set_track_heights(args.set_track_heights)
    if args.set_track_widths:
        ableton_set.set_track_widths(args.set_track_widths)

    if args.list_tracks:
        ableton_set.load_tracks()
        ableton_set.print_tracks()

    if args.check_samples:
        ableton_set.list_samples()

    if args.fix_samples_absolute or args.fix_samples_collect:
        ableton_set.fix_samples(db, collect_and_save=args.fix_samples_collect)

    if args.check_plugins:
        vst_dirs: List[pathlib.Path] = []
        result = ableton_set.list_plugins(vst_dirs)
        vst_dirs = list(set(result))

    if args.xml:
        ableton_set.save_xml()
    if args.save:
        # if backup == ableton_set:
        #     logger.info("%sSet has no changes from originally, not saving...", MB)
        #     return 0
        ableton_set.save_set(append_bars_bpm=args.append_bars_bpm, prepend_version=args.prepend_version)
    elif any(
        [
            args.master_out,
            args.cue_out,
            args.fold,
            args.unfold,
            args.set_track_heights,
            args.set_track_widths,
            args.fix_samples_absolute,
            args.fix_samples_collect,
        ]
    ):
        logger.info("%sNo changes saved, use -s/--save option to write changes to file.", Y)
    return 0


def process(args: argparse.Namespace) -> int:
    """Process arguments.

    Args:
        args: argparse.Namespace with parsed arguments.

    Returns:
        integer with exit code, zero indicating success, non-zero indicating error.
    """
    if args.db:
        create_db.create_or_update_db(args.srcs)
        return 0
    db = None
    if args.fix_samples_collect or args.fix_samples_absolute:
        logger.info("%sLoading db...", M)
        db = create_db.load_db()

    start_time = time.time()
    pathlib_objects = get_pathlib_objects(srcs=args.srcs)
    if not pathlib_objects:
        logger.info("%sError, no sets to process!", R)
        return -1

    for pathlib_obj in pathlib_objects:
        try:
            process_set(args, pathlib_obj, db)
        except ElementNotFound:
            logger.info(traceback.format_exc())
        logger.info("%s\n\n%s\n\n", M, "^" * os.get_terminal_size().columns)
    logger.info(
        "%sTook %s to process %s set(s)", CB, datetime.timedelta(seconds=time.time() - start_time), len(pathlib_objects)
    )
    return 0


def main() -> None:
    """Entry point to cli."""
    args = parse_arguments()
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
        datefmt="%H:%M:%S",
    )
    sys.exit(process(args))


if __name__ == "__main__":
    main()
