"""File-IO for .als documents: backups, renames and file times around :mod:`abletoolz.formats.live_document`."""

from __future__ import annotations

import datetime
import logging
import os
import pathlib
import shutil
import subprocess
import sys
from collections.abc import Iterable
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

from abletoolz.ansi import B, G, R, Y
from abletoolz.formats import FormatError, live_document

if TYPE_CHECKING:
    from abletoolz.live_set.document import AbletonSet

if sys.platform == "win32":
    import win32_setctime

logger = logging.getLogger(__name__)

BACKUP_DIR = "abletoolz_backup"

# Live's own backup folder, and the one abletoolz writes. A set inside either is
# a copy of one the user already has.
_BACKUP_DIRS = frozenset({"Backup", "backup", BACKUP_DIR})


def is_backup_set(path: pathlib.Path) -> bool:
    """Whether ``path`` is a copy of a set rather than a set worth acting on.

    Two kinds of copy: a file inside a backup folder, and a macOS ``._``
    resource fork that only looks like a set. Only the parent directories are
    checked, so a set *named* "Backup.als" is still a real set.

    The collector and the sidecar writer share this predicate because they
    must agree: when they disagree, files land in Ableton's own Backup folder.
    """
    return path.name.startswith("._") or not _BACKUP_DIRS.isdisjoint(path.parts[:-1])


def find_sets(paths: Iterable[str | os.PathLike[str]]) -> list[pathlib.Path]:
    """Every set and clip under ``paths``, folders searched recursively, backups skipped, each file once."""
    found: list[pathlib.Path] = []
    for src in paths:
        path = pathlib.Path(src)
        if path.is_dir():
            # Path.rglob takes one pattern and no filter.
            files = list(path.rglob("*.als")) + list(path.rglob("*.alc"))
            found.extend(file for file in files if not is_backup_set(file))
        elif path.is_file() and not is_backup_set(path):
            found.append(path)
    unique: dict[tuple[int, int] | pathlib.Path, pathlib.Path] = {}
    for path in found:
        identity: tuple[int, int] | pathlib.Path = path.resolve()
        try:
            status = path.stat()
        except OSError:
            pass
        else:
            if status.st_ino:
                identity = (status.st_dev, status.st_ino)
        unique.setdefault(identity, path)
    return list(unique.values())


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
    """Copy file to the backup directory, never replacing an earlier backup there.

    A copy, not a move: the original stays at its path until the new file
    replaces it in one step, so an interrupted save leaves the old file there.
    """
    backup_dir = pathlib_obj.parent / BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = unclaimed_path(backup_dir / pathlib_obj.name)
    logger.info(
        "%sCopying original file to backup directory:\n%s --> %s",
        B,
        pathlib_obj,
        backup_path,
    )
    # Through a temp file, so an interrupted copy never leaves half a backup under the backup's name.
    partial = backup_path.with_name(backup_path.name + ".tmp")
    shutil.copy2(pathlib_obj, partial)
    os.replace(partial, backup_path)


def read_set(live_set: AbletonSet) -> bool:
    """Load the set's document into an element tree; log and say False when it is not one."""
    return load_set(live_set) is None


def load_set(live_set: AbletonSet) -> str | None:
    """Load the set's document into an element tree; None once loaded, else why it cannot be, logged."""
    try:
        root = live_document.read(live_set.path)
    except FormatError as error:
        # read() puts the path in front; the reason alone is its cause.
        reason = str(error.__cause__ if isinstance(error.__cause__, FormatError) else error)
        logger.error("%s%s: %s", R, live_set.path, reason)
        return reason
    get_file_times(live_set)
    live_set._root = root
    return None


def to_xml_bytes(live_set: AbletonSet) -> bytes:
    """Serialize the tree with Ableton's xml header and trailing newline."""
    return live_document.to_xml_bytes(live_set.root)


def save_xml(live_set: AbletonSet, output_dir: pathlib.Path | None = None) -> None:
    """Dump the decompressed XML next to the set, or into ``output_dir`` so the original's folder stays untouched."""
    folder = live_set.path.parent if output_dir is None else output_dir
    folder.mkdir(parents=True, exist_ok=True)
    xml_file = folder / (live_set.path.stem + ".xml")
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
    """Write the set the way Live does, replacing the file at its path in one step."""
    live_document.write(live_set.path, document)
    logger.info("%sSaved set to %s", G, live_set.path)
    restore_file_times(live_set)


def save_set(live_set: AbletonSet, output_dir: pathlib.Path | None = None) -> None:
    """Back up the original and replace it with the edited set.

    With ``output_dir`` the set is written into that directory under its own name,
    the original is never touched, and no backup is made.
    """
    # Serialize before touching anything on disk: a tree that cannot serialize
    # must fail loudly here, while the original file is still in place.
    document = live_document.encode(live_set.root)
    if output_dir is None:
        create_backup(live_set.path)
        write_set(live_set, document)
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    wanted = output_dir / live_set.path.name
    target = unclaimed_path(wanted) if wanted.exists() else wanted
    # Published without replacing anything: another worker may save a set of the same name here at the same time.
    while True:
        try:
            live_document.write_new(target, document)
            break
        except FileExistsError:
            target = unclaimed_path(wanted)
    if target != wanted:
        logger.info("%s%s already exists, saving as %s", Y, wanted, target.name)
    live_set.path = target
    logger.info("%sSaved set to %s", G, target)
    restore_file_times(live_set)


def save_in_place(path: pathlib.Path, root: ET.Element) -> None:
    """Write ``root`` over the set at ``path`` the way ``-s`` does: original to the backup folder, times kept."""
    document = live_document.encode(root)
    times = path.stat()
    create_backup(path)
    live_document.write(path, document)
    os.utime(path, (times.st_atime, times.st_mtime))
