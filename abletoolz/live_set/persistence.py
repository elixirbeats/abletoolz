"""File-IO for .als documents: the gzip container, backups, and file times."""

from __future__ import annotations

import gzip
import logging
import os
import pathlib
import re
import subprocess
import sys
import threading
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

from abletoolz import utils
from abletoolz.misc import B, G, M, R, Y

if TYPE_CHECKING:
    from abletoolz.live_set.document import AbletonSet

if sys.platform == "win32":
    import win32_setctime

logger = logging.getLogger(__name__)

GZIP_MAGIC = b"\x1f\x8b"
PRE_8_2_MAGIC = b"\xab\x1e"  # yes, it spells able :P
XML_MAGIC = b"<?"  # hand-unzipped/exported sets are plain XML; Live opens those too


def read_set(live_set: AbletonSet) -> bool:
    """Uncompress an .als and load it into an element tree."""
    with open(live_set.path, "rb") as fd:
        first_two_bytes = fd.read(2)
    if first_two_bytes == PRE_8_2_MAGIC:
        logger.error("%s%sIs pre Ableton 8.2.x which is unsupported.", R, live_set.path)
        return False
    if first_two_bytes not in (GZIP_MAGIC, XML_MAGIC):
        logger.error(
            "%s%sFile is not .als or is an older format that doesn't use gzip!, cannot open...",
            R,
            live_set.path,
        )
        return False
    get_file_times(live_set)
    if first_two_bytes == XML_MAGIC:
        data = live_set.path.read_text(encoding="utf-8")
    else:
        with gzip.open(live_set.path, "r") as fd:
            data = fd.read().decode("utf-8")
    if not data:
        logger.error("%s%s is an empty gzip archive — the file is truncated or corrupt.", R, live_set.path)
        return False
    live_set._root = ET.fromstring(data)
    return True


def to_xml_bytes(live_set: AbletonSet) -> bytes:
    """Serialize the tree with Ableton's xml header and trailing newline."""
    header = b'<?xml version="1.0" encoding="UTF-8"?>\n'
    footer = b"\n"
    xml_bytes: bytes = ET.tostring(live_set.root, encoding="utf-8")
    return header + xml_bytes + footer


def save_xml(live_set: AbletonSet) -> None:
    """Dump the decompressed XML next to the set."""
    xml_file = live_set.path.parent / (live_set.path.stem + ".xml")
    if xml_file.exists():
        utils.create_backup(xml_file)
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
        utils.format_date(live_set.creation_time),
        utils.format_date(live_set.last_modification_time),
    )


def restore_file_times(live_set: AbletonSet) -> None:
    """Restore original creation and modification times to the file."""
    if live_set.last_modification_time is None:
        logger.warning("No modification time! Can't restore original time...")
        return
    os.utime(live_set.path, (live_set.last_modification_time, live_set.last_modification_time))
    if sys.platform == "win32" and live_set.creation_time is not None:
        win32_setctime.setctime(live_set.path, live_set.creation_time)
    elif sys.platform == "darwin":
        date = utils.format_date(live_set.creation_time)
        subprocess.run(["SetFile", "-d", date, str(live_set.path)], capture_output=True, check=False)
    if live_set.creation_time is not None and live_set.last_modification_time is not None:
        logger.debug(
            "%sRestored set creation and modification times: %s, %s",
            G,
            utils.format_date(live_set.creation_time),
            utils.format_date(live_set.last_modification_time),
        )


def write_set(live_set: AbletonSet, xml_bytes: bytes) -> None:
    """Recompress the set to gzip. Run in a thread to avoid corruption on interrupt."""
    if live_set.path.exists():
        raise FileExistsError(
            f"File {live_set.path} already exists!(Did it not get moved to the backup folder?) Cannot overwrite..."
        )
    with gzip.open(live_set.path, "wb") as fd:
        fd.write(xml_bytes)
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
    """
    # Serialize before touching anything on disk: a tree that cannot serialize
    # must fail loudly here, while the original file is still in place.
    xml_bytes = to_xml_bytes(live_set)
    if output_dir is None:
        utils.create_backup(live_set.path)
    if append_bars_bpm:
        if live_set.bpm is None or live_set.furthest_bar is None:
            live_set.transport.bpm()
            live_set.transport.furthest_bar()
        cleaned_name = re.sub(r"_\d{1,3}bars_\d{1,3}\.\d{2}bpm", "", live_set.path.stem)
        new_filename = cleaned_name + f"_{live_set.furthest_bar}bars_{live_set.bpm:.2f}bpm{live_set.path.suffix}"
        live_set.path = pathlib.Path(live_set.path.parent / new_filename)
        logger.debug("%sAppending bars and bpm, new set name: %s.als", M, live_set.path.stem)

    if live_set.version_tuple and prepend_version:
        version_string = f"{live_set.version_tuple[0]}.{live_set.version_tuple[1]}.{live_set.version_tuple[2]}_"
        cleaned_name = re.sub(r"\d{1,2}\.\d{1,3}\.[b\d]{1,5}_", "", live_set.path.stem)
        live_set.path = live_set.path.parent / (version_string + cleaned_name + live_set.path.suffix)

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / live_set.path.name
        if target.exists():
            target = utils.unclaimed_path(target)
            logger.info("%s%s already exists, saving as %s", Y, output_dir / live_set.path.name, target.name)
        live_set.path = target

    # Non daemon thread so the write is not forcibly killed if the parent process is.
    thread = threading.Thread(target=write_set, args=(live_set, xml_bytes))
    thread.start()
    thread.join()
