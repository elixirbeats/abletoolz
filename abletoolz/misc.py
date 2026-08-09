"""Define root level vars and functions."""

import enum
import os
import pathlib
import sys
from typing import Literal, overload
from xml.etree import ElementTree as ET

import colorama

colorama.init(autoreset=True)


def _get_user_config_dir() -> pathlib.Path:
    """Get platform-specific user config directory for abletoolz."""
    if sys.platform == "win32":
        base = pathlib.Path(os.environ.get("APPDATA", pathlib.Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = pathlib.Path.home() / "Library" / "Application Support"
    else:
        xdg_config = os.environ.get("XDG_CONFIG_HOME")
        base = pathlib.Path(xdg_config) if xdg_config else pathlib.Path.home() / ".config"
    return base / "abletoolz"


def default_ableton_user_library() -> pathlib.Path | None:
    """Live's default User Library location for this OS, or None when it can't be found.

    Library code never guesses paths; CLI layers call this and fall back to
    asking the user when it returns None (moved library, Linux, etc.).
    """
    if sys.platform == "win32":
        candidate = pathlib.Path.home() / "Documents" / "Ableton" / "User Library"
    elif sys.platform == "darwin":
        candidate = pathlib.Path.home() / "Music" / "Ableton" / "User Library"
    else:
        return None
    return candidate if candidate.is_dir() else None


def default_vst_dirs() -> list[pathlib.Path]:
    """Standard plugin install locations for this OS, existing dirs only."""
    if sys.platform == "win32":
        program_files = pathlib.Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        candidates = [
            program_files / "VstPlugins",
            program_files / "Steinberg" / "VstPlugins",
            program_files / "Common Files" / "VST3",
        ]
    elif sys.platform == "darwin":
        candidates = [
            pathlib.Path("/Library/Audio/Plug-Ins/VST"),
            pathlib.Path("/Library/Audio/Plug-Ins/VST3"),
            pathlib.Path.home() / "Library" / "Audio" / "Plug-Ins" / "VST",
            pathlib.Path.home() / "Library" / "Audio" / "Plug-Ins" / "VST3",
        ]
    else:
        candidates = [
            pathlib.Path.home() / ".vst",
            pathlib.Path.home() / ".vst3",
            pathlib.Path("/usr/lib/vst3"),
            pathlib.Path("/usr/local/lib/vst3"),
        ]
    return [candidate for candidate in candidates if candidate.is_dir()]

# These are the hex values of the drop down color menu, arranged in the same order of rows and columns.
# yapf: disable
# fmt: off
ableton_colors = [
    0xFF94A6, 0xFFA428, 0xCD9827, 0xF6F57C, 0xBEFA00, 0x21FF41, 0x25FEA9, 0x5DFFE9, 0x8AC5FE, 0x5480E4, 0x93A6FF, 0xD86CE4, 0xE552A1, 0xFFFEFE,
    0xFE3637, 0xF66D02, 0x99734A, 0xFEF134, 0x87FF67, 0x3DC201, 0x01BEAF, 0x18E9FE, 0x10A4EE, 0x007DC0, 0x886CE4, 0xB776C6, 0xFE38D4, 0xD1D0D1,
    0xE3665A, 0xFEA274, 0xD2AD70, 0xEDFFAE, 0xD3E499, 0xBAD175, 0x9AC58D, 0xD4FCE0, 0xCCF0F8, 0xB8C1E2, 0xCDBBE4, 0xAF98E4, 0xE5DDE0, 0xA9A8A8,
    0xC6938A, 0xB68257, 0x98826A, 0xBEBB69, 0xA6BE00, 0x7CB04C, 0x89C3BA, 0x9BB3C4, 0x84A5C3, 0x8392CD, 0xA494B5, 0xBF9FBE, 0xBD7096, 0x7B7A7A,
    0xAF3232, 0xA95131, 0x734E41, 0xDAC200, 0x84971F, 0x529E31, 0x0A9C8E, 0x236285, 0x1A2F96, 0x2E52A3, 0x624BAD, 0xA24AAD, 0xCD2E6F, 0xFFFEFE,
]


STEREO_OUTPUTS: dict[int, dict[str, str]] = {
    1: {"target": "AudioOut/External/S0",  "lower_display_string": "1/2"},
    2: {"target": "AudioOut/External/S1", "lower_display_string": "3/4"},
    3: {"target": "AudioOut/External/S2", "lower_display_string": "5/6"},
    4: {"target": "AudioOut/External/S3", "lower_display_string": "7/8"},
    5: {"target": "AudioOut/External/S4", "lower_display_string": "9/10"},
    6: {"target": "AudioOut/External/S5", "lower_display_string": "11/12"},
    7: {"target": "AudioOut/External/S6", "lower_display_string": "13/14"},
    8: {"target": "AudioOut/External/S7", "lower_display_string": "15/16"},
    9: {"target": "AudioOut/External/S8", "lower_display_string": "17/18"},
    10: {"target": "AudioOut/External/S9","lower_display_string": "19/20"},
}
# yapf: enable
# fmt: on


# Shorten color variables
RST = colorama.Fore.RESET
if sys.platform == "win32":
    BOLD = "[1m"  # pylint: disable=invalid-character-esc
    # Windows terminal colors are hard to see, use bright.
    R = colorama.Fore.LIGHTRED_EX
    G = colorama.Fore.LIGHTGREEN_EX
    B = colorama.Fore.LIGHTBLUE_EX
    Y = colorama.Fore.LIGHTYELLOW_EX
    C = colorama.Fore.LIGHTCYAN_EX
    M = colorama.Fore.LIGHTMAGENTA_EX
else:
    BOLD = "\033[1m"
    R = colorama.Fore.RED
    G = colorama.Fore.GREEN
    B = colorama.Fore.BLUE
    Y = colorama.Fore.YELLOW
    C = colorama.Fore.CYAN
    M = colorama.Fore.MAGENTA

RB = R + colorama.Style.BRIGHT + BOLD
GB = G + colorama.Style.BRIGHT + BOLD
BB = B + colorama.Style.BRIGHT + BOLD
YB = Y + colorama.Style.BRIGHT + BOLD
CB = C + colorama.Style.BRIGHT + BOLD
MB = M + colorama.Style.BRIGHT + BOLD

# Database stored alongside config in user config dir
DEFAULT_DB_PATH = _get_user_config_dir() / "sample_db.json"
BACKUP_DIR = "abletoolz_backup"


class AbletoolzError(Exception):
    """Error running Abletoolz."""


class ElementNotFound(Exception):
    """Element doesnt exist within the xml hierarchy where expected."""


class SetError(Exception):
    """Ableton set parse error."""


class SetOperatingSystem(enum.Enum):
    """Pre ableton 11, sets store data differently.

    Sets do not store any OS information, but we can guess based on encoding of data, AU units vs VSTs and some
    other differences.
    """

    MAC_OS = enum.auto()
    WINDOWS_OS = enum.auto()
    UNSET = enum.auto()


@overload
def get_element(
    root: ET.Element,
    attribute_path: str,
) -> ET.Element: ...


@overload
def get_element(
    root: ET.Element,
    attribute_path: str,
    *,
    silent_error: Literal[False] = False,
    attribute: Literal[None] = None,
) -> ET.Element: ...


@overload
def get_element(
    root: ET.Element,
    attribute_path: str,
    *,
    silent_error: Literal[True],
    attribute: Literal[None] = None,
) -> ET.Element | None: ...


@overload
def get_element(
    root: ET.Element,
    attribute_path: str,
    *,
    silent_error: Literal[False] = False,
    attribute: str,
) -> str: ...


@overload
def get_element(
    root: ET.Element,
    attribute_path: str,
    *,
    attribute: str,
    silent_error: Literal[True],
) -> str | None: ...


def get_element(
    root: ET.Element,
    attribute_path: str,
    *,
    silent_error: bool = False,
    attribute: str | None = None,
) -> ET.Element | str | None:
    """Get element using Element tree xpath syntax."""
    element = root.findall(f"./{'/'.join(attribute_path.split('.'))}")
    if not element:
        if silent_error:
            return None
        # ElementTree.dump(root)
        raise ElementNotFound(f"{R}No element for path [{attribute_path}]")
    if attribute:
        attr = element[0].get(attribute)
        if attr is None:
            raise ElementNotFound(f"{R}Element {attribute}is empty!")
        return attr
    return element[0]


@overload
def search_element(
    root: ET.Element,
    search_name: str,
    attribute: Literal[None] = None,
) -> ET.Element | None: ...


@overload
def search_element(root: ET.Element, search_name: str, attribute: str) -> str | None: ...


def search_element(root: ET.Element, search_name: str, attribute: str | None = None) -> ET.Element | str | None:
    """Similar to get_element but searches entire tree instead of absolute path."""
    element = root.find(f".//{search_name}")
    if element is None:
        return None
    if attribute:
        return element.get(attribute)
    return element


def note_translator(midi_note_number: int) -> tuple[str, int]:
    """Return note and octave from midi note number."""
    notes = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    octave = midi_note_number // 12 - 1
    note_index = midi_note_number % 12
    return notes[note_index], octave
