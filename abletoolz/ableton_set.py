import datetime
import enum
import gzip
import os
import pathlib
import re
import subprocess
import sys
import threading
import xml
from typing import Tuple, List, Optional

from xml.etree import ElementTree
import chardet

from abletoolz.ableton_track import AbletonTrack
from abletoolz import (
    R, RB, G, GB, B, BB, Y, YB, C, CB, M, MB, BACKUP_DIR, RST, STEREO_OUTPUTS, get_element,
)

if sys.platform == 'win32':
    import win32_setctime


class SetOperatingSystem(enum.Enum):
    """Pre ableton 11, sets store data differently."""
    MAC_OS = enum.auto()
    WINDOWS_OS = enum.auto()
    UNSET = enum.auto()


def version_supported(set_version, supported_version):
    for set_v, supported_v in zip(set_version, supported_version):
        if set_v > supported_v:
            return True
        elif set_v < supported_v:
            return False
    return True


def above_version(supported_version):
    """Decorator to handle function support for changing XML schemas across Ableton versions."""
    # https://help.ableton.com/hc/en-us/articles/360000841004-Backward-Compatibility
    def wrapper(f):
        def wrapped_func(self, *args, **kwargs):
            if not version_supported(self.major_minor_patch, supported_version):
                print(f'Function {f.__name__} is only supported for {supported_version} and above.')
                return None
            return f(self, *args, **kwargs)

        return wrapped_func

    return wrapper


class AbletonSet(object):
    """Set object"""

    def __init__(self, pathlib_obj):
        self.name = pathlib_obj.name
        self.pathlib_obj = pathlib_obj
        self.last_modification_time = None
        self.creation_time = None
        self.project_root_folder = None  # Folder where Ableton Project Info resides.
        self.tree = None
        self.root = None
        self.creator = None  # Official Ableton live version.
        self.schema_change = None  # Xml schema change version.
        self.major_version = None
        self.minor_version = None
        self.major_minor_patch = None
        self.set_os: SetOperatingSystem = SetOperatingSystem.UNSET

        # Parsed set variables.
        self.master_output = None
        self.cue_output = None
        self.tempo = None
        self.key = None
        self.tracks = []
        self.furthest_bar = None
        self.bpm = None
        self.missing_absolute_samples = []
        self.missing_relative_samples = []
        self.found_vst_dirs: List[pathlib.Path] = []

    def open_folder(self):
        if sys.platform == 'win32':
            Popen_arg = f'explorer /select, "{self.pathlib_obj}"'
            subprocess.Popen(Popen_arg)

    def load_version(self):
        """Loads version."""
        self.creator = self.root.get('Creator')
        parsed = re.findall(r'Ableton Live ([0-9]{1,2})\.([0-9]{1,3})[\.b]{0,1}([0-9]{1,3}){0,1}', self.creator)[0]
        parsed = [int(x) if x.isdigit() else x for x in parsed if x != '']
        if len(parsed) == 3:
            major, minor, patch = parsed
        elif len(parsed) == 2:
            major, minor = parsed
            patch = 0
        else:
            raise Exception(f'Could not parse version from: {self.creator}')
        self.major_minor_patch = major, minor, patch
        print(f'{B}Set version: {M}{self.creator}')
        if 'b' in self.creator.split()[-1]:
            print(f'{Y}Set is from a beta version, some commands might not work properly!')

    @staticmethod
    def human_readable_date(timestamp):
        """Returns easy to read date."""
        return datetime.datetime.fromtimestamp(timestamp).strftime("%m/%d/%Y %H:%M:%S")

    def parse(self):
        """Uncompresses ableton set and loads into element tree."""
        with open(self.pathlib_obj, 'rb') as fd:
            first_two_bytes = fd.read(2)
            if first_two_bytes == b'\xab\x1e':  # yes, it spells able :P
                print(f'{R}{self.pathlib_obj} Is pre Ableton 8.2.x which is unsupported.')
                return False
            elif first_two_bytes != b'\x1f\x8b':
                print(f'{R}{self.pathlib_obj} File format is not gzip!, cannot open.')
                return False
        self.get_file_times()
        with gzip.open(self.pathlib_obj, 'r') as fd:
            data = fd.read().decode('utf-8')
            if not data:
                print(f'{R}Error loading data {self.pathlib_obj}!')
                return False
            self.root = ElementTree.fromstring(data)
            return True

    @staticmethod
    def move_original_file_to_backup_dir(pathlib_obj):
        """Moves file to backup directory, does not replace previous files moved there."""
        backup_dir = pathlib_obj.parent / BACKUP_DIR
        backup_dir.mkdir(parents=True, exist_ok=True)
        ending_int = 1
        while True:
            backup_path = backup_dir / (pathlib_obj.stem + '__' + str(ending_int) + pathlib_obj.suffix)
            if pathlib.Path(backup_path).exists():
                ending_int += 1
            else:
                print(f'{B}Moving original file to backup directory:\n{pathlib_obj} --> {backup_path}')
                # Rename creates a new pathlib object with the new path,
                # so pathlib_obj will still point to the original path when the file is saved.
                pathlib_obj.rename(backup_path)
                return

    def find_project_root_folder(self):
        """Finds project root folder for set."""
        # TODO Parse project .cfg file and print information.
        if self.project_root_folder:
            return self.project_root_folder

        max_folder_search_depth = 10
        for i, current_dir in enumerate(self.pathlib_obj.parents):
            if i > max_folder_search_depth:
                print(f'{R}Reached maximum search depth, exiting..')
                break
            elif pathlib.Path(current_dir / 'Ableton Project Info').exists():
                self.project_root_folder = current_dir
                print(f'{C}Project root folder: {current_dir}')
                return
        print(f'{R}Could not find project folder(Ableton Project Info), unable to validate relative paths!')

    def generate_xml(self):
        """Add header and footer to xml data."""
        header = '<?xml version="1.0" encoding="UTF-8"?>\n'.encode('utf-8')
        footer = '\n'.encode('utf-8')
        xml_output = ElementTree.tostring(self.root, encoding='utf-8')
        return header + xml_output + footer

    def save_xml(self):
        xml_file = self.pathlib_obj.parent / (self.pathlib_obj.stem + '.xml')
        if xml_file.exists():
            self.move_original_file_to_backup_dir(xml_file)
        with xml_file as fd:
            fd.write_bytes(self.generate_xml())
        print(f'{G}Saved xml to {xml_file}')

    def get_file_times(self):
        if sys.platform == 'win32':
            self.creation_time = os.path.getctime(self.pathlib_obj)
        else:
            self.creation_time = os.stat(self.pathlib_obj).st_birthtime
        self.last_modification_time = os.path.getmtime(self.pathlib_obj)
        print(f'{B}File creation time {self.human_readable_date(self.creation_time)}, '
              f'Last modification time: {self.human_readable_date(self.last_modification_time)}')

    def restore_file_times(self, pathlib_obj):
        """Restore original creation and modification times to file."""
        os.utime(self.pathlib_obj, (self.last_modification_time, self.last_modification_time))
        if sys.platform == 'win32':
            win32_setctime.setctime(self.pathlib_obj, self.creation_time)
        elif sys.platform == 'darwin':
            date = self.human_readable_date(self.creation_time)
            path = str(pathlib_obj).replace(' ', r'\ ')
            os.system(f'SetFile -d "{date}" {path} >/dev/null')
        print(f'{G}Restored set creation and modification times: {self.human_readable_date(self.creation_time)}, '
              f'{self.human_readable_date(self.last_modification_time)}')

    def write_set(self):
        """Recompresses set to gzip. Used in thread to help prevent file getting corrupted mid write."""
        with gzip.open(self.pathlib_obj, 'wb') as fd:
            fd.write(self.generate_xml())
        print(f'{G}Saved set to {self.pathlib_obj}')
        self.restore_file_times(self.pathlib_obj)

    def save_set(self, append_bars_bpm=False):
        self.move_original_file_to_backup_dir(self.pathlib_obj)
        if append_bars_bpm:
            cleaned_name = re.sub(r'_\d{1,3}bars_\d{1,3}\.\d{2}bpm', '', self.pathlib_obj.stem)
            new_filename = cleaned_name + f'_{self.furthest_bar}bars_{self.bpm:.2f}bpm.als'
            self.pathlib_obj = pathlib.Path(self.pathlib_obj.parent / new_filename)
            print(f'{M}Appending bars and bpm, new set name: {self.pathlib_obj.stem}.als')

        # Create non daemon thread so that it is not forcibly killed when parent process dies.
        thread = threading.Thread(target=self.write_set)
        thread.start()
        thread.join()

    # Data parsing functions.
    @above_version(supported_version=(8, 0, 0))
    def load_tracks(self):
        """Load tracks into AbletonTrack src."""
        tracks = get_element(self.root, 'LiveSet.Tracks')
        for track in tracks:
            self.tracks.append(AbletonTrack(track))

    def print_tracks(self):
        """Prints track info."""
        print('\n'.join([str(x) for x in self.tracks]))

    def find_furthest_bar(self):
        """Finds the max of the longest clip or furthest bar something is in Arrangement."""
        current_end_times = [int(float(end_times.get('Value'))) for end_times in self.root.iter('CurrentEnd')]
        self.furthest_bar = int(max(current_end_times) / 4) if current_end_times else 0
        return self.furthest_bar

    @above_version(supported_version=(8, 2, 0))
    def get_bpm(self):
        """Gets bpm."""
        post_10_bpm = 'LiveSet.MasterTrack.DeviceChain.Mixer.Tempo.Manual'
        pre_10_bpm = 'LiveSet.MasterTrack.MasterChain.Mixer.Tempo.ArrangerAutomation.Events.FloatEvent'
        major, minor, patch = self.major_minor_patch
        if major >= 10 or major >= 9 and minor >= 7:
            bpm_elem = get_element(self.root, post_10_bpm, attribute='Value', silent_error=True)
        else:
            bpm_elem = get_element(self.root, pre_10_bpm, attribute='Value')
        self.bpm = round(float(bpm_elem), 6)
        return self.bpm

    def estimate_length(self):
        """Multiply the longest bar with length per bar by inverting BPM."""
        if self.bpm is None or self.furthest_bar is None:
            print(f"{R}Can't estimate length without bpm and furthest bar.")
            return
        # TODO improve this to find the time signature from the set and use it here instead of only 4/4.
        seconds_total = ((4 * int(self.furthest_bar)) / self.bpm) * 60
        length = f"{int(seconds_total // 60)}:{round(seconds_total % 60):02d}"
        print(f'{M}Longest clip or furthest arrangement position: {self.furthest_bar} bars. '
              f'{C}Estimated length(Only valid for 4/4): {length}')

    def set_track_heights(self, height):
        """In Arrangement view, sets all track lanes/automation lanes to specified height."""
        height = min(425, (max(17, height)))  # Clamp to valid range.
        [el.set('Value', str(height)) for el in self.root.iter('LaneHeight')]
        print(f'{G}Set track heights to {height}.')

    def set_track_widths(self, width):
        """Sets all track widths in Clip view to specified width."""
        width = min(264, (max(17, width)))  # Clamp to valid range.
        # Sesstion is how it's named in the set, not a typo!
        [el.set('Value', str(width)) for el in self.root.iter('ViewStateSesstionTrackWidth')]
        print(f'{G}Set track widths to {width}.')

    def fold_tracks(self):
        """Fold all tracks."""
        [el.set('Value', 'false') for el in self.root.iter('TrackUnfolded')]
        print(f'{G}Folded all tracks.')

    def unfold_tracks(self):
        """Unfold all tracks."""
        [el.set('Value', 'true') for el in self.root.iter('TrackUnfolded')]
        print(f'{G}Unfolded all tracks.')

    @above_version(supported_version=(8, 2, 0))
    def set_audio_output(self, output_number, element_string):
        """Sets audio output."""
        if output_number not in STEREO_OUTPUTS:
            raise Exception(f'{R}Output number invalid!. Available options: \n{STEREO_OUTPUTS}{RST}')
        output_obj = STEREO_OUTPUTS[output_number]
        out_target_element = get_element(self.root, f'LiveSet.{element_string}.DeviceChain.AudioOutputRouting.Target',
                                         silent_error=True)
        if not isinstance(out_target_element, ElementTree.Element):
            out_target_element = get_element(  # ableton 8 sets use "MasterChain" for master track.
                self.root, f'LiveSet.{element_string}.MasterChain.AudioOutputRouting.Target')
            lower_display_string_element = get_element(
                self.root, f'LiveSet.{element_string}.MasterChain.AudioOutputRouting.LowerDisplayString')
        else:
            lower_display_string_element = get_element(
                self.root, f'LiveSet.{element_string}.DeviceChain.AudioOutputRouting.LowerDisplayString')
        out_target_element.set('Value', output_obj['target'])
        lower_display_string_element.set('Value', output_obj['lower_display_string'])
        print(f'{G}Set {element_string} to {output_obj["lower_display_string"]}')

    @staticmethod
    def _parse_mac_data(byte_data, abs_hash_path, debug=False):
        """Parses hex data for absolute path of file on MacOS.
        This hex byte data seems to be some type of struct with data_length_header:data:NULL sections of data, but other
        areas don't follow that pattern at all. This function attempts to grab all 'valid' data_length_header:data:NULL
        sections. The last two are always filepath, and the volume the file is on.
        """
        last_possible_index = len(byte_data) - 1
        itm_lst = []
        i = 6  # First 5 bytes are always a header containing the total amount of bytes.
        while i <= last_possible_index:
            if byte_data[i] == 0 or byte_data[i] == 0xFF:
                i += 1
                continue
            potential_length = byte_data[i]
            potential_end = i + potential_length + 1  # Potential ending NULL byte.
            if (potential_end < last_possible_index
                    and 0 not in byte_data[i + 1:potential_end] and byte_data[potential_end] == 0):
                if debug:
                    print(f'\t{Y}i: {i}, value at i: {byte_data[i]}, potential (e)ndex: {potential_end}, '
                          f'data: {byte_data[i + 1: potential_end]}')
                itm_lst.append(byte_data[i + 1:potential_end])
                i = potential_end + 1
                continue
            i += 1
        try:
            return f'{itm_lst[-1].decode()}{itm_lst[-2].decode()}' if len(itm_lst) >= 2 else None
        except UnicodeDecodeError as e:
            print(f'\n\n{R} Couldn\'t decode: {abs_hash_path}, Error: {e}\n\n')

    @staticmethod
    def _parse_windows_data(byte_data, abs_hash_path):
        """Parses hex byte data for absolute path of file on Windows.
        Windows hex bytes are utf-16 encoded with \x00 bytes in-between each character.
        """
        try:
            return byte_data.decode('utf-16').replace('\x00', '')  # Remove ending NULL byte.
        except UnicodeDecodeError as e:
            print(f'\n\n{R} Couldn\'t decode: {abs_hash_path}, Error: {e}\n\n')

    def _parse_hex_path(self, text):
        """Takes raw hex string from XML entry and parses."""
        if not text:
            return None
        # Strip new lines and tabs from raw text to have one long hex string.
        abs_hash_path = text.replace('\t', '').replace('\n', '')
        byte_data = bytearray.fromhex(abs_hash_path)
        if byte_data[0:3] == b'\x00' * 3:  # Header only on mac projects.
            self.set_os = SetOperatingSystem.MAC_OS
            return self._parse_mac_data(byte_data, abs_hash_path)
        else:
            self.set_os = SetOperatingSystem.WINDOWS_OS
            return self._parse_windows_data(byte_data, abs_hash_path)

    def path_separator_type(self, path_str: str) -> str:
        """Get OS path string separator."""
        if '\\' in path_str:
            return '\\'
        elif '/' in path_str:
            return '/'
        else:
            raise Exception(f'Couldn\'t parse OS path type! {path_str}')

    def search_plugins(self, plugin_name) -> Optional[pathlib.Path]:
        if sys.platform == 'win32':
            drive = os.environ['SYSTEMDRIVE']
            _WINDOWS_VST3 = pathlib.Path(f'{drive}\Program Files\Common Files\VST3')
            vst3_plugins = list(_WINDOWS_VST3.rglob('*.dll')) + list(_WINDOWS_VST3.rglob('*.vst3'))
            for vst3 in vst3_plugins:
                if plugin_name == vst3.name:
                    return vst3
            for directory in self.found_vst_dirs:
                for dll in directory.rglob('*.dll'):
                    if plugin_name == dll.name or plugin_name == dll.name.replace('.32', '').replace('.64', ''):
                        return dll
            return None
        else:
            # TODO: Implement MacOS VST3 logic
            print(f'{RB}Mac OS Vst3 not implemented yet.')
            return None

    # Plugin related functions.
    def parse_vst_element(self, vst_element: xml.etree.ElementTree.Element) -> Tuple[Optional[pathlib.Path], Optional[str], Optional[pathlib.Path]]:
        for plugin_path in ['Dir', 'Path']:
            path_results = vst_element.findall(f'.//{plugin_path}')
            if len(path_results):
                if plugin_path == 'Path':
                    full_path = path_results[0].get('Value')
                    if not '/' in full_path and not '\\' in full_path:
                        if search_result := self.search_plugins(full_path):
                            return None, search_result.name, search_result
                        return None, full_path, None
                    path_separator = self.path_separator_type(full_path)
                    name = full_path.split(path_separator)[-1]
                    return pathlib.Path(full_path), name, None
                elif plugin_path == 'Dir':
                    dir_bin = path_results[0].find('Data').text
                    path = self._parse_hex_path(dir_bin)
                    name = vst_element.find('FileName').get('Value')
                    if not path:
                        print(f'{Y}Couldn\'t parse absolute path for {name}')
                        return None, name, None
                    path_separator = self.path_separator_type(path)
                    if path[-1] == path_separator:
                        full_path = f'{path}{name}'
                    else:
                        full_path = f'{path}{path_separator}{name}'
                    return pathlib.Path(full_path), name, None
        else:
            print(f'{R}Couldn\'t parse plugin!')
            return None, None, None

    def dump_element(self):
        if self.last_elem is not None:
            ElementTree.dump(self.last_elem)

    def list_plugins(self, verbose: bool, vst_dirs):
        """Iterates through all plugin references and checks paths for VSTs."""
        self.found_vst_dirs.extend(vst_dirs)
        for plugin_element in self.root.iter('PluginDesc'):
            self.last_elem = plugin_element
            for vst_element in plugin_element.iter('VstPluginInfo'):
                full_path, name, potential = self.parse_vst_element(vst_element)
                exists = True if full_path and full_path.exists() else False
                if exists and full_path.parent not in self.found_vst_dirs:
                    self.found_vst_dirs.append(full_path.parent)
                elif not exists:
                    # Did not find plugin in saved path, try to search
                    potential = self.search_plugins(name)
                color = G if exists else R
                if potential and color == R:
                    color = Y
                print(f'{color}Plugin: {name}, {M}Plugin folder path: {full_path}, {color}Exists: {exists}')
                if potential:
                    print(f'\t{CB}Potential alternative path for {name} found: {M}{potential}')
            for au_element in plugin_element.iter('AuPluginInfo'):
                name = au_element.find('Name').get('Value')
                manufacturer = get_element(plugin_element, 'AuPluginInfo.Manufacturer', attribute='Value')
                print(f'{M}Mac OS Audio Units are not saved with paths. Plugin {manufacturer}: {name} cannot be verified.')
                # TODO figure out how to match different name from components installed to stored set plugin.
                # au_components = pathlib.Path('/Library/Audio/Plug-Ins/Components').rglob('*.component')
        return self.found_vst_dirs

    # Sample related functions.
    @above_version(supported_version=(8, 2, 0))
    def check_relative_path(self, name, sample_element):
        """Constructs absolute path from project root and relative path stored in set."""
        if not self.project_root_folder:
            return None, None
        relative_path_enabled = get_element(sample_element, 'FileRef.HasRelativePath', attribute='Value')
        relative_path_type = get_element(sample_element, 'FileRef.RelativePathType', attribute='Value')
        if relative_path_enabled == 'true' and relative_path_type == '3':
            relative_path_element = get_element(sample_element, 'FileRef.RelativePath')
            sub_directory_path = []
            for path in relative_path_element:
                sub_directory_path.append(path.get('Dir'))
            from_project_root = f'{os.path.sep.join(sub_directory_path)}{os.path.sep}{name}'
            full_path = self.project_root_folder / os.path.sep.join(sub_directory_path) / name
            return full_path, from_project_root
        return None, None

    def list_samples_pre_11(self, verbose: bool = False, dump_xml: bool = True) -> None:
        """Iterates through all sample references and checks absolute and relative paths.

        file paths are mixed binary data for MacOs and Windows.
        """
        missing_samples = 0
        for sample_element in self.root.iter('SampleRef'):
            self.last_elem = sample_element
            name = get_element(sample_element, 'FileRef.Name', attribute='Value')
            relative_path, from_project_root = self.check_relative_path(name, sample_element)
            abs_str = self._parse_hex_path(get_element(sample_element, 'FileRef.Data').text)
            absolute_path = pathlib.Path(abs_str)
            file_ref = sample_element.find('FileRef')

            for file_size_str in ['OriginalFileSize', 'FileSize']:
                file_size = file_ref.findall(f'.//{file_size_str}')
                if len(file_size):
                    saved_filesize = int(file_size[0].get('Value'))
                    break
            else:
                raise Exception('Could not parse saved sample size.')
            if not self._parse_samplepaths(absolute_path, relative_path, verbose, saved_filesize):
                missing_samples += 1

        self.sample_results(missing_samples)

    def list_samples_post_11(self, verbose: bool = False, dump_xml: bool = False) -> None:
        """Post Ableton 11 sample parser. Format changed from binary encoded paths to simple strings for all OSes."""
        missing_samples = 0
        for sample_element in self.root.iter('SampleRef'):
            self.last_elem = sample_element
            file_ref = sample_element.find('FileRef')
            absolute_path = pathlib.Path(file_ref.find('Path').get('Value'))
            relative_path_type = file_ref.find('RelativePathType').get('Value')
            relative_path = None
            if str(absolute_path).startswith("Samples"):
                relative_path = pathlib.Path(self.project_root_folder) / file_ref.find('Path').get('Value')
                absolute_path = None
            if relative_path_type == '3':
                relative_path = pathlib.Path(self.project_root_folder) / file_ref.find('RelativePath').get('Value')

            saved_filesize = int(file_ref.find('OriginalFileSize').get('Value'))
            if not self._parse_samplepaths(absolute_path, relative_path, verbose, saved_filesize):
                missing_samples += 1

        self.sample_results(missing_samples)

    def _parse_samplepaths(self, absolute_path: Optional[pathlib.Path], relative_path: Optional[pathlib.Path], verbose: bool,
                           saved_filesize: int) -> bool:
        absolute_found = absolute_path and absolute_path.exists()
        relative_found = relative_path and relative_path.exists()
        if not absolute_found and not relative_found:
            if absolute_path and absolute_path not in self.missing_absolute_samples:
                self.missing_absolute_samples.append(absolute_path)
            if relative_path and relative_path not in self.missing_relative_samples:
                self.missing_relative_samples.append(relative_path)
            return False
        else:
            if absolute_found:
                local_filesize = absolute_path.stat().st_size
                if verbose:
                    size_match = saved_filesize == local_filesize
                    color = G if size_match else R
                    print(f'{G}Absolute path sample found: {absolute_path}\n\tFile size '
                          f'{local_filesize} matches saved filesize {saved_filesize}: {color}{size_match}')
            if relative_found:
                local_filesize = relative_path.stat().st_size
                size_match = saved_filesize == local_filesize
                color = G if size_match else R
                if verbose:
                    print(f'{G}Relative(collect and save) sample found: {relative_path}\n\tFile size '
                          f'{local_filesize} matches saved filesize {saved_filesize}: {color}{size_match}')
            if absolute_found or relative_found:
                return True
        return False

    def sample_results(self, missing_samples: int) -> None:
        """Print results of sample search."""
        color = G if not missing_samples else Y
        print(f'{color}Total missing sample references: {M}{missing_samples}{color}, this can include duplicate '
              f'references to the same sample so only unique paths are listed here. Relative paths are created using '
              f'collect-and-save. If either sample path is found Ableton will load the sample.')
        if self.missing_relative_samples:
            rel_string = '\n\t'.join((str(x) for x in self.missing_relative_samples))
            print(f'{Y}Missing Relative paths:{R}\n\t{rel_string}')
        if self.missing_absolute_samples:
            abs_string = '\n\t'.join((str(x) for x in self.missing_absolute_samples))
            print(f'{Y}Missing Absolute paths:{R}\n\t{abs_string}')

    def list_samples(self, verbose: bool) -> None:
        """Select correct sample parsing function."""
        if self.major_minor_patch[0] >= 11:
            return self.list_samples_post_11(verbose)
        return self.list_samples_pre_11(verbose)

    # Plugin related functions.
    @above_version(supported_version=(9, 0, 0))
    def plugin_buffers(self, verbose: bool) -> None:
        for plugin in self.root.iter('VstPluginInfo'):
            self.last_elem = plugin
            # ElementTree.dump(plugin)
            for path_query in ['Path', 'Dir']:
                path_ele = plugin.findall(f'.//{path_query}')
                if len(path_ele):
                    if path_query == 'Dir':
                        data = path_ele[0].findall('.//Data')
                        path_str = self._parse_hex_path(data[0].text)
                        break
                    else:
                        path_str = path_ele[0].get('Value')

            name = plugin.find('PlugName').get('Value')
            buffer_str = plugin.findall('.//Buffer')[0].text
            if not buffer_str:
                continue
            parsed_buf = buffer_str.replace('\t', '').replace('\n', '')
            buffer_bytes = bytes.fromhex(parsed_buf)

            encodings = ['utf-8', 'ascii', 'utf-16', 'Windows-1254', 'ISO-8859-1', 'IBM866']
            detected_enc = chardet.detect(buffer_bytes)['encoding']
            if detected_enc:
                encodings.insert(0, detected_enc)
            for encoding in encodings:
                try:
                    decoded = buffer_bytes.decode(encoding=encoding, errors='ignore')
                    print(name, encoding, decoded[:500] if len(decoded) >= 1000 else decoded)
                    # break
                except UnicodeDecodeError as exc:
                    if verbose:
                        print(f'{R}Couldn\'t decode with {encoding} for bytes: {exc}, data:\n{buffer_bytes}')
                    decoded = 'COULD NOT DECODE'

            end_index = min(len(decoded), 800) if not verbose else len(decoded)
            if not verbose and len(decoded) > 800:
                print(f'{G}Name: {name}, {B}Path: {path_str}, {C}Encoding: {detected_enc}, '
                      f'Buffer(truncated if more than 800 characters):\n{decoded[:end_index]}')
            else:
                print(f'{G}Name: {name}, {B}Path: {path_str}, {C}Encoding: {detected_enc}, Buffer:\n{decoded}\n\n')
