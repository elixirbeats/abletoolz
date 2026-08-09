"""Random util functions."""
import datetime
import logging
import pathlib

from abletoolz.misc import BACKUP_DIR, B, R, Y

logger = logging.getLogger(__name__)


def format_date(timestamp: float) -> str:
    """Return easy to read date."""
    return datetime.datetime.fromtimestamp(timestamp).strftime("%m/%d/%Y %H:%M:%S")


def create_backup(pathlib_obj: pathlib.Path) -> None:
    """Move file to backup directory, does not replace previous files moved there."""
    backup_dir = pathlib_obj.parent / BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    ending_int = 1
    while True:
        backup_path = backup_dir / (pathlib_obj.stem + "__" + str(ending_int) + pathlib_obj.suffix)
        if pathlib.Path(backup_path).exists():
            ending_int += 1
        else:
            logger.info(
                "%sMoving original file to backup directory:\n%s --> %s",
                B,
                pathlib_obj,
                backup_path,
            )
            # Rename creates a new pathlib object with the new path,
            # so pathlib_obj will still point to the original path when the file is saved.
            pathlib_obj.rename(backup_path)
            return


def parse_mac_data(byte_data: bytes | bytearray, abs_hash_path: str, debug: bool = False) -> str | None:
    """Parse hex data for absolute path of file on MacOS.

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
        if (
            potential_end < last_possible_index
            and 0 not in byte_data[i + 1 : potential_end]
            and byte_data[potential_end] == 0
        ):
            if debug:
                logger.info(
                    "\t%si: %s, value at i: %s, potential (e)ndex: %s, data: %s",
                    Y,
                    i,
                    byte_data[i],
                    potential_end,
                    byte_data[i + 1 : potential_end],
                )
            itm_lst.append(byte_data[i + 1 : potential_end])
            i = potential_end + 1
            continue
        i += 1
    try:
        return f"{itm_lst[-1].decode()}{itm_lst[-2].decode()}" if len(itm_lst) >= 2 else None
    except UnicodeDecodeError as e:
        logger.error("\n\n%s Couldn't decode: %s, Error: %s\n\n", R, abs_hash_path, e)
    return None


def parse_windows_data(byte_data: bytes | bytearray, abs_hash_path: str) -> str | None:
    r"""Parse hex byte data for absolute path of file on Windows.

    Windows hex bytes are utf-16 encoded with \x00 bytes in-between each character.
    """
    try:
        return byte_data.decode("utf-16").replace("\x00", "")  # Remove ending NULL byte.
    except UnicodeDecodeError as e:
        logger.error("\n\n%s Couldn't decode: %s, Error: %s\n\n", R, abs_hash_path, e)
    return None


def parse_hex_path(text: str) -> str | None:
    """Take raw hex string from XML entry and parses.

    Only necessary for older 8/9/10 sets. In 11 they
    switched all paths to strings.
    """
    if not text:
        return None
    # Strip new lines and tabs from raw text to have one long hex string.
    abs_hash_path = text.replace("\t", "").replace("\n", "")
    byte_data = bytearray.fromhex(abs_hash_path)
    if byte_data[0:3] == b"\x00" * 3:  # Header only on mac projects.
        return parse_mac_data(byte_data, abs_hash_path)
    else:
        return parse_windows_data(byte_data, abs_hash_path)


def path_separator_type(path_str: str) -> str:
    """Get OS path string separator."""
    if "\\" in path_str:
        return "\\"
    elif "/" in path_str:
        return "/"
    else:
        raise ValueError(f"Couldn't parse OS path type! {path_str}")
