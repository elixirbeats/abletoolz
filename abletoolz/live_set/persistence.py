"""File-IO for .als documents: backups, renames and file times around :mod:`abletoolz.formats.live_document`."""

from __future__ import annotations

import datetime
import logging
import os
import pathlib
import re
import subprocess
import sys
import threading
from typing import TYPE_CHECKING

from abletoolz.formats import FormatError, live_document
from abletoolz.misc import BACKUP_DIR, B, G, M, R, Y

if TYPE_CHECKING:
    from abletoolz.live_set.document import AbletonSet

if sys.platform == "win32":
    import win32_setctime

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


def read_set(live_set: AbletonSet) -> bool:
    """Load the set's document into an element tree; log and say False when it is not one."""
    try:
        root = live_document.read(live_set.path)
    except live_document.Pre82DocumentError:
        logger.error("%s%sIs pre Ableton 8.2.x which is unsupported.", R, live_set.path)
        return False
    except FormatError as error:
        logger.error("%s%s", R, error)
        return False
    get_file_times(live_set)
    live_set._root = root
    return True


def to_xml_bytes(live_set: AbletonSet) -> bytes:
    """Serialize the tree with Ableton's xml header and trailing newline."""
    return live_document.to_xml_bytes(live_set.root)


def save_xml(live_set: AbletonSet) -> None:
    """Dump the decompressed XML next to the set."""
    xml_file = live_set.path.parent / (live_set.path.stem + ".xml")
    if xml_file.exists():
        create_backup(xml_file)
    xml_file.write_bytes(to_xml_bytes(live_set))
    logger.info("%sSaved xml to %s", G, xml_file)


def get_file_times(live_set: AbletonSet) -> None:
    """Record the set's creation and modification times for restoration after write."""
    if sys.platform == "win32":
        live_set.creation_time = os.path.getctime(live_set.path)
    else:
        # Linux filesystems expose no birth time; mtime is the best stand-in.
        stat_result = os.stat(live_set.path)
        live_set.creation_time = getattr(stat_result, "st_birthtime", stat_result.st_mtime)
    live_set.last_modification_time = os.path.getmtime(live_set.path)
    logger.debug(
        "%sFile creation time %s, Last modification time: %s",
        B,
        format_date(live_set.creation_time),
        format_date(live_set.last_modification_time),
    )


def restore_file_times(live_set: AbletonSet) -> None:
    """Restore original creation and modification times to the file."""
    if live_set.last_modification_time is None:
        logger.warning("No modification time! Can't restore original time...")
        return
    os.utime(live_set.path, (live_set.last_modification_time, live_set.last_modification_time))
    if live_set.creation_time is None:
        return
    if sys.platform == "win32":
        win32_setctime.setctime(live_set.path, live_set.creation_time)
    elif sys.platform == "darwin":
        date = format_date(live_set.creation_time)
        subprocess.run(["SetFile", "-d", date, str(live_set.path)], capture_output=True, check=False)
    logger.debug(
        "%sRestored set creation and modification times: %s, %s",
        G,
        format_date(live_set.creation_time),
        format_date(live_set.last_modification_time),
    )


def write_set(live_set: AbletonSet, document: bytes) -> None:
    """Write the set the way Live does. Run in a thread to avoid corruption on interrupt."""
    if live_set.path.exists():
        raise FileExistsError(
            f"File {live_set.path} already exists!(Did it not get moved to the backup folder?) Cannot overwrite..."
        )
    live_document.write(live_set.path, document)
    logger.info("%sSaved set to %s", G, live_set.path)
    restore_file_times(live_set)


def save_set(
    live_set: AbletonSet,
    append_bars_bpm: bool = False,
    prepend_version: bool = False,
    output_dir: pathlib.Path | None = None,
) -> None:
    """Back up the original, optionally rename, and write the set on a non-daemon thread.

    With ``output_dir`` the set is written into that directory under its own name and
    the original file is never touched -- writing elsewhere is the protection an
    in-place save gets from its backup, so no backup is made either.

    ``append_bars_bpm`` and ``prepend_version`` are deprecated: the ``.meta.yaml``
    sidecar beside each set records bars, bpm and the Live version on every scan.
    """
    # Serialize before touching anything on disk: a tree that cannot serialize
    # must fail loudly here, while the original file is still in place.
    document = live_document.encode(live_set.root)
    if output_dir is None:
        create_backup(live_set.path)
    if append_bars_bpm:
        logger.warning(
            "%s--append-bars-bpm is deprecated and will be removed in a future release. "
            "Bars and bpm are recorded in the set's .meta.yaml sidecar on every scan.",
            Y,
        )
        if live_set.bpm is None or live_set.furthest_bar is None:
            live_set.transport.bpm()
            live_set.transport.furthest_bar()
        cleaned_name = re.sub(r"_\d{1,3}bars_\d{1,3}\.\d{2}bpm", "", live_set.path.stem)
        new_filename = cleaned_name + f"_{live_set.furthest_bar}bars_{live_set.bpm:.2f}bpm{live_set.path.suffix}"
        live_set.path = pathlib.Path(live_set.path.parent / new_filename)
        logger.debug("%sAppending bars and bpm, new set name: %s.als", M, live_set.path.stem)

    if prepend_version:
        logger.warning(
            "%s--prepend-version is deprecated and will be removed in a future release. "
            "The Live version is recorded in the set's .meta.yaml sidecar on every scan.",
            Y,
        )
        if live_set.version_tuple:
            version_string = f"{live_set.version_tuple[0]}.{live_set.version_tuple[1]}.{live_set.version_tuple[2]}_"
            cleaned_name = re.sub(r"\d{1,2}\.\d{1,3}\.[b\d]{1,5}_", "", live_set.path.stem)
            live_set.path = live_set.path.parent / (version_string + cleaned_name + live_set.path.suffix)

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / live_set.path.name
        if target.exists():
            target = unclaimed_path(target)
            logger.info("%s%s already exists, saving as %s", Y, output_dir / live_set.path.name, target.name)
        live_set.path = target

    # Non daemon thread so the write is not forcibly killed if the parent process is.
    thread = threading.Thread(target=write_set, args=(live_set, document))
    thread.start()
    thread.join()
