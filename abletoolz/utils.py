"""Random util functions."""

import datetime
import logging
import pathlib

from abletoolz.misc import BACKUP_DIR, B

logger = logging.getLogger(__name__)


def format_date(timestamp: float) -> str:
    """Return easy to read date."""
    return datetime.datetime.fromtimestamp(timestamp).strftime("%m/%d/%Y %H:%M:%S")


def unclaimed_path(pathlib_obj: pathlib.Path) -> pathlib.Path:
    """First name__N.suffix path beside ``pathlib_obj`` that doesn't exist yet."""
    ending_int = 1
    while True:
        candidate = pathlib_obj.parent / (pathlib_obj.stem + "__" + str(ending_int) + pathlib_obj.suffix)
        if not candidate.exists():
            return candidate
        ending_int += 1


def create_backup(pathlib_obj: pathlib.Path) -> None:
    """Move file to backup directory, does not replace previous files moved there."""
    backup_dir = pathlib_obj.parent / BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = unclaimed_path(backup_dir / pathlib_obj.name)
    logger.info(
        "%sMoving original file to backup directory:\n%s --> %s",
        B,
        pathlib_obj,
        backup_path,
    )
    # Rename creates a new pathlib object with the new path,
    # so pathlib_obj will still point to the original path when the file is saved.
    pathlib_obj.rename(backup_path)


def path_separator_type(path_str: str) -> str:
    """Get OS path string separator."""
    if "\\" in path_str:
        return "\\"
    elif "/" in path_str:
        return "/"
    else:
        raise ValueError(f"Couldn't parse OS path type! {path_str}")
