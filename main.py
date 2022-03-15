#!/usr/bin/env python3

import argparse
import datetime
import pathlib
import sys
import time

from abletoolz.ableton_set import AbletonSet
from abletoolz import R, G, B, C, BACKUP_DIR


def get_pathlib_objects(filepath=None, directory=None):
    if directory:
        files = list(pathlib.Path(directory).rglob("*.als"))
        # Hacky but Path.rglob doesn't have options for filtering.
        files_to_process = []
        for file in files:
            if all(x not in file.parts[:-1] for x in ['Backup', 'backup', BACKUP_DIR]) and not file.stem.startswith(('._')):
                files_to_process.append(file)
        return files_to_process
    elif filepath:
        return [pathlib.Path(filepath)]

def is_valid_dir_path(path: str) -> str:
    """Checks if the path is a valid path"""
    if sys.platform.startswith('win') and "\"" in path:
        raise Exception(f'{R}Windows paths must not end in backslash: -d \'C:\\somepath\\\'(BAD) -d \'C:\\somepath\' '
                        f'(GOOD). This is due to a bug in how Windows handles backslashes before quotes.')
    return path


def parse_arguments():
    """Get command line arguments."""
    parser = argparse.ArgumentParser()

    # Input arguments.
    parser.add_argument('-d', '--directory', type=is_valid_dir_path,
                        help='directory to recursively parse sets in, cannot be combined with --file. If no options '
                             'are set defaults to present working directory. NOTE: ON WINDOWS do not include a trailing'
                             '"\\" !! ')
    parser.add_argument('-f', '--file', help='individual set file to parse, cannot be combined with --directory.')
    # Output arguments.
    parser.add_argument('-x', '--xml', action="store_true", default=False,
                        help='dump the xml in same directory as set_name.xml(Useful to understand set structure).')
    parser.add_argument('-s', '--save', action="store_true", default=False,
                        help='Saves file after parsing. This is only put here as a safety check, to make sure you know '
                             'what you are doing! The original file is always renamed to '
                             f'set_directory/{BACKUP_DIR}/set_name_xx.als, where xx will automatically increase to '
                             'to avoid overwriting files.')
    parser.add_argument('-v', '--verbose', action="store_true", default=False,
                        help='Adds extra verbosity, for some commands this will print more information.')

    parser.add_argument('--append-bars-bpm', action='store_true', default=False,
                        help='Append furthest bar length and bpm to filename to help organize your set collection. '
                             'For example, my_set.als --> my_set_32bars_90.00bpm.als Option only works with -s/--save')

    # Edit set arguments.
    parser.add_argument('--unfold', action="store_true", default=False,
                        help='unfolds all tracks/automation lanes in arrangement.')
    parser.add_argument('--fold', action="store_true", default=False,
                        help='folds all tracks/automation lanes in arrangement.')
    parser.add_argument('--set-track-heights', type=int, help='Set arrangement track heights')
    parser.add_argument('--set-track-widths', type=int, help='Set clip view track width.')
    parser.add_argument('--cue-out', type=int,
                        help='set Cue audio output tracks. Set to 1 for stereo 1/2, 2 for 3/4 etc')
    parser.add_argument('--master-out', type=int,
                        help=f'number to set Master audio output tracks to. Same numbers as --cue-out')

    # Analysis arguments.
    parser.add_argument('--list-tracks', action='store_true', default=False,
                        help='Load and list all track information.')
    parser.add_argument('--check-samples', action="store_true", default=False,
                        help='Checks relative and absolute sample paths and verifies if sample exists there.')
    parser.add_argument('--plugin-buffers', action="store_true", default=False,
                        help='Experimental feature, attempts to print each plugin\'s save buffer.')
    parser.add_argument('--check-plugins', action="store_true", default=False,
                        help='Checks plugin VST paths and verifies they exists. Note: If Ableton finds the '
                             'plugin name in a different path it will automatically update these paths the next time '
                             'you save your project, so take it with a grain of salt. AU are not stored as paths in '
                             'sets but abbreviated component names. Might possibly add support for them later.')
    args = parser.parse_args()

    assert not (args.fold and args.unfold), 'Only set unfold or fold, not both.'
    assert not (args.file and args.directory), 'Only set file or directory, not both.'
    return args

def process(args):
    """Process arguments."""
    start_time = time.time()
    pathlib_objects = get_pathlib_objects(filepath=args.file, directory=args.directory)
    if not pathlib_objects:
        print(f'{R} Error, no sets to process!')
        return

    for pathlib_obj in pathlib_objects:
        print(f'\n{C}Parsing : {pathlib_obj}')
        ableton_set = AbletonSet(pathlib_obj)
        if not ableton_set.parse():
            continue
        ableton_set.load_version()
        print(f'{C}Set name: {pathlib_obj.stem}, {B}BPM: {ableton_set.get_bpm()}')
        ableton_set.find_project_root_folder()
        ableton_set.find_furthest_bar()
        ableton_set.estimate_length()

        if args.master_out:
            ableton_set.set_audio_output(args.master_out, element_string='MasterTrack')
        if args.cue_out:
            ableton_set.set_audio_output(args.cue_out, element_string='PreHearTrack')
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
        if args.plugin_buffers:
            ableton_set.plugin_buffers(args.verbose)
        if args.check_plugins:
            ableton_set.list_plugins()
        if args.check_samples:
            try:
                ableton_set.list_samples(args.verbose)
            except Exception as e:
                import traceback
                print(traceback.format_exc())

        if args.xml:
            ableton_set.save_xml()
        if args.save:
            ableton_set.save_set(args.append_bars_bpm)
        elif any([args.master_out, args.cue_out, args.fold,args.unfold, args.set_track_heights, args.set_track_widths]):
            print(f'{Y}No changes saved, use -s/--save option to write changes(if any) to file.')

        # Unload from memory.
        del ableton_set

    print(f'Took {datetime.timedelta(seconds=time.time() - start_time)} to process {len(pathlib_objects)} set(s)')


if __name__ == "__main__":
    args = parse_arguments()
    process(args)