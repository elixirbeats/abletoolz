import colorama

colorama.init()

# Shorten color variables
RST = colorama.Fore.RESET
R = colorama.Fore.RED
G = colorama.Fore.GREEN
B = colorama.Fore.BLUE
Y = colorama.Fore.YELLOW
C = colorama.Fore.CYAN
M = colorama.Fore.MAGENTA
W = colorama.Fore.WHITE

SMALLEST_LANE_HEIGHT = 17
DEFAULT_LANE_HEIGHT = 24
BACKUP_DIR = 'abletoolz_backup'

STEREO_OUTPUTS = {
    1: {'target': 'AudioOut/External/S0', 'lower_display_string': '1/2'},
    2: {'target': 'AudioOut/External/S1', 'lower_display_string': '3/4'},
    3: {'target': 'AudioOut/External/S2', 'lower_display_string': '5/6'},
    4: {'target': 'AudioOut/External/S3', 'lower_display_string': '7/8'},
    5: {'target': 'AudioOut/External/S4', 'lower_display_string': '9/10'},
    6: {'target': 'AudioOut/External/S5', 'lower_display_string': '11/12'},
    7: {'target': 'AudioOut/External/S6', 'lower_display_string': '13/14'},
    8: {'target': 'AudioOut/External/S7', 'lower_display_string': '15/16'},
    9: {'target': 'AudioOut/External/S8', 'lower_display_string': '17/18'},
    10: {'target': 'AudioOut/External/S9', 'lower_display_string': '19/20'},
}


# Common functions.
def print_rst(message):
    """Always resets terminal color after message."""
    print(message)
    print(f'{RST}', end='')


def get_element(root, attribute_path, attribute=None, silent_error=False):
    """Gets element using Element tree xpath syntax."""
    element = root.findall(f"./{'/'.join(attribute_path.split('.'))}")
    if not element:
        raise Exception(f'{R}No element for path [{attribute_path}]{RST}')
    if attribute:
        return element[0].get(attribute)
    return element[0]


def note_translator(midi_note_number):
    """Returns note and octave from midi note number."""
    notes = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    octave = midi_note_number // 12 - 1
    noteIndex = midi_note_number % 12
    return notes[noteIndex], octave