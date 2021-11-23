import datetime
import gzip
import os
import re
import sys
import threading
import pathlib
from xml.etree import ElementTree

from abletoolz.ableton_track import AbletonTrack
from abletoolz import RST, R, G, B, Y, C, M, BACKUP_DIR, STEREO_OUTPUTS
from abletoolz import get_element

if sys.platform == "win32":
    import win32_setctime


def version_supported(supported_version, set_version):
    digits = set_version.split()[-1]
    if len([x for x in digits if x == '.']) < 2:
        digits = f'{digits}0'
    set_int = int(re.sub(r'\D|\.', '', digits))
    supported_int = int(supported_version.replace('.', ''))
    if set_int > supported_int:
        return True
    return False

def parse_version_tuple():
    pass

def above_version(supported_version):
    """Decorator to prevent Exceptions in older sets where the xml schema was different."""
    def wrapper(f):
        def wrapped_func(self, *args, **kwargs):
            if not version_supported(supported_version, self.creator):
                print(f'Function {f.__name__} is only supported for {supported_version} and above.')
                return None
            return f(self, *args, **kwargs)
        return wrapped_func
    return wrapper


class AbletonSet(object):
    """Set object"""

    def __init__(self, pathlib_obj):
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

        # Parsed set variables.
        self.master_output = None
        self.cue_output = None
        self.tempo = None
        self.key = None
        self.tracks = []
        self.furthest_bar = None
        self.bpm = None

    def load_version(self):
        """Loads version."""
        self.creator = self.root.get('Creator')
        # if self.creator.startswith('Ableton Live 11'):
        parsed = re.findall(r"Ableton Live ([1-9]{1,2})\.([0-9]{1,3})\.([0-9]{1,3})", self.creator)
        major, minor, patch = [int(x) for x in parsed[0]]
        self.schema_change = self.root.get('SchemaChangeCount')
        self.major_version = self.root.get('MajorVersion')
        self.minor_version = self.root.get('MinorVersion')
        print(f'{Y}Official version: {M}{self.creator}, {G}Schema version: {self.schema_change}')

    @staticmethod
    def human_readable_date(timestamp):
        """Returns easy to read date."""
        return datetime.datetime.fromtimestamp(timestamp).strftime("%m/%d/%Y %H:%M:%S")

    def parse(self):
        """Uncompresses ableton set and loads into element tree."""
        with open(self.pathlib_obj, 'rb') as fd:
            first_two_bytes = fd.read(2)
            if first_two_bytes == b'\xab\x1e':  # yes, it spells able :P
                print(f'{R}{self.pathlib_obj} Is an older unsupported version that is not compressed xml.')
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
            backup_path = backup_dir / (pathlib_obj.stem + "__" + str(ending_int) + pathlib_obj.suffix)
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

        max_folder_search_depth = 5
        for i, current_dir in enumerate(self.pathlib_obj.parents):
            if i > max_folder_search_depth:
                print(f'{R}Reached maximum search depth, exiting..')
                return
            elif pathlib.Path(current_dir / "Ableton Project Info").exists():
                self.project_root_folder = current_dir
                print(f'{G}Project root folder: {current_dir}')
                return
        print(f'{R}Did not find project root folder, relative paths will not be verifiable.')

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
        with xml_file as fd:  # with pathlib.Path(xml_file) as fd:
            fd.write_bytes(self.generate_xml())
        print(f'{G}Saved xml to {xml_file}')

    def get_file_times(self):
        if sys.platform == 'win32':
            self.creation_time = os.path.getctime(self.pathlib_obj)
        else:
            self.creation_time = os.stat(self.pathlib_obj).st_birthtime
        self.last_modification_time = os.path.getmtime(self.pathlib_obj)
        print(f'{Y}File creation time {self.human_readable_date(self.creation_time)}, '
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
        print(f'{G}Restored creation and modification times: {self.human_readable_date(self.creation_time)}, '
              f'{self.human_readable_date(self.last_modification_time)}')

    def write_set(self, pathlib_obj):
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
        thread = threading.Thread(target=self.write_set, args=(self.pathlib_obj,))
        thread.start()
        thread.join()

    # Data parsing functions.
    @above_version(supported_version='8.0.0')
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

    @above_version(supported_version='8.2.0')
    def get_bpm(self):
        """Gets bpm."""
        bpm = 'LiveSet.MasterTrack.DeviceChain.Mixer.Tempo.Manual'
        pre_10_bpm = 'LiveSet.MasterTrack.MasterChain.Mixer.Tempo.ArrangerAutomation.Events.FloatEvent'
        bpm_elem = get_element(self.root, bpm, attribute='Value', silent_error=True)
        if not bpm_elem:
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
        print(f'{Y}Longest clip or furthest arrangement position: {self.furthest_bar} bars. '
              f'{C}Estimated length(Only valid for 4/4 Time signature): {length}.')

    def set_track_heights(self, height):
        """In Arrangement view, sets all track lanes/automation lanes to specified height."""
        height = min(425, (max(17, height)))  # Clamp to valid range.
        [el.set('Value', str(height)) for el in self.root.iter('LaneHeight')]
        print(f'{G}Set track heights to {height}.')

    def set_track_widths(self, width):
        """Sets all track widths in Clip view to specified width."""
        width = min(264, (max(17, width)))  # Clamp to valid range.
        # Sesstion is how it's named in the set, not a typo.
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

    @above_version(supported_version='8.2.0')
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
        print(f"{G}Set {element_string} to {output_obj['lower_display_string']}")

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
            print(f"\n\n{R} Couldn't decode: {abs_hash_path}, Error: {e}\n\n")

    @staticmethod
    def _parse_windows_data(byte_data, abs_hash_path):
        """Parses hex byte data for absolute path of file on Windows.
        Windows hex bytes are utf-16 encoded with \x00 bytes in-between each character.
        """
        try:
            return byte_data.decode('utf-16').replace('\x00', '')  # Remove ending NULL byte.
        except UnicodeDecodeError as e:
            print(f"\n\n{R} Couldn't decode: {abs_hash_path}, Error: {e}\n\n")

    def _parse_hex_path(self, text):
        """Takes raw hex string from XML entry and parses."""
        if not text:
            return None
        # Strip new lines and tabs from raw text to have one long hex string.
        abs_hash_path = text.replace('\t', '').replace('\n', '')
        byte_data = bytearray.fromhex(abs_hash_path)
        if byte_data[0:3] == b'\x00' * 3:  # Header only on mac projects.
            return self._parse_mac_data(byte_data, abs_hash_path)
        else:
            return self._parse_windows_data(byte_data, abs_hash_path)

    # Plugin related functions.
    def list_plugins(self):
        """Iterates through all plugin references and checks paths for VSTs."""
        for plugin_element in self.root.iter('PluginDesc'):
            if plugin_element.findall('./VstPluginInfo'):
                name = get_element(plugin_element, 'VstPluginInfo.FileName', attribute='Value')
                path = self._parse_hex_path(get_element(plugin_element, 'VstPluginInfo.Dir.Data').text)
                if not path:
                    print(f"{Y}Couldn't parse absolute path for {name}")
                    continue
                # Some plugin paths end in the os path separator, others don't.
                if path[-1] == os.path.sep:
                    full_path = f'{path}{name}'
                else:
                    full_path = f'{path}{os.path.sep}{name}'
                exists = pathlib.Path(full_path).exists()
                color = G if exists else R
                print(f'{color}Plugin: {name}, {Y}Plugin folder path: {full_path}, {color}Exists: {exists}')
            elif plugin_element.findall('./AuPluginInfo'):
                name = get_element(plugin_element, 'AuPluginInfo.Name', attribute='Value')
                manufacturer = get_element(plugin_element, 'AuPluginInfo.Manufacturer', attribute='Value')
                print(f"{Y}Audio Units do not store paths. Plugin {manufacturer}: {name} cannot be verified.")
                # TODO figure out how to match different name from components installed to stored set plugin.
                # au_components = pathlib.Path('/Library/Audio/Plug-Ins/Components').rglob('*.component')

    # Sample related functions.
    def check_relative_path(self, name, sample_element):
        """Constructs absolute path from project root and relative path stored in set."""
        if not self.project_root_folder:
            print(f'{R}Error, project root folder is undefined, cant find relative paths.')
            return
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

    def list_samples_v11(self):
        """Ableton 11 sample find."""
        for sample_element in self.root.iter('SampleRef'):
            name = get_element(sample_element, 'FileRef.Name', attribute='Value')
            print(f'{C}Sample name: {name}')
            relative_path, from_project_root = self.check_relative_path(name, sample_element)
            if relative_path:
                rel_exists = pathlib.Path(relative_path).exists()
                color = G if rel_exists else R
                print(f'\t{color}Relative path to project root(highlighted): '
                      f'{Y}{self.project_root_folder}{os.path.sep}{M}{from_project_root}, {color}Exists: {rel_exists}')
            abs_path = self._parse_hex_path(get_element(sample_element, 'FileRef.Data').text)
            abs_exists = pathlib.Path(abs_path).exists() if abs_path else None
            color = G if abs_exists else R
            print(f'\t{color}Absolute path: {Y}{abs_path}, {color}Exists: {abs_exists}')

    def list_samples_pre_v11(self):
        """Iterates through all sample references and checks absolute and relative paths."""
        for sample_element in self.root.iter('SampleRef'):
            name = get_element(sample_element, 'FileRef.Name', attribute='Value')
            print(f'{C}Sample name: {name}')
            relative_path, from_project_root = self.check_relative_path(name, sample_element)
            if relative_path:
                rel_exists = pathlib.Path(relative_path).exists()
                color = G if rel_exists else R
                print(f'\t{color}Relative path to project root(highlighted): '
                      f'{Y}{self.project_root_folder}{os.path.sep}{M}{from_project_root}, {color}Exists: {rel_exists}')
            abs_path = self._parse_hex_path(get_element(sample_element, 'FileRef.Data').text)
            abs_exists = pathlib.Path(abs_path).exists() if abs_path else None
            color = G if abs_exists else R
            print(f'\t{color}Absolute path: {Y}{abs_path}, {color}Exists: {abs_exists}')

    def list_samples(self):
        if self.major_version >= 11:
            return self.list_samples_v11()
        return self.list_samples_pre_v11()