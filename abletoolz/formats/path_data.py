"""The pre-11 ``FileRef/Data`` blob: an absolute path, stored as hex text.

Live 11 switched every path to a plain ``Path`` string. Before it, a sample or
plugin path is binary behind :mod:`abletoolz.formats.hex_text`, and which
binary says which OS saved the set: Windows writes the path as UTF-16LE with a
terminating NUL; a Mac writes an alias record, which opens with three zero
bytes and is read here by its length-prefixed fields rather than decoded
whole.

Only the Windows form is written. A Mac alias holds volume and catalog data
this library has never derived, so :func:`encode` writes the Windows form
whatever read it, as the library always has.
"""

from __future__ import annotations

import dataclasses
import logging

from abletoolz.formats import hex_text
from abletoolz.misc import R, SetOperatingSystem

logger = logging.getLogger(__name__)

_MAC_HEADER = b"\x00" * 3
# The alias record's first bytes are a header carrying the record's size.
_MAC_FIELDS_START = 6


@dataclasses.dataclass(frozen=True, slots=True)
class PathData:
    """A decoded path, and the OS its encoding says wrote it.

    ``path`` is None when the bytes are that OS's form but do not decode, which
    older sets hold for moved or renamed volumes; the error is logged where it
    is found.
    """

    os: SetOperatingSystem
    path: str | None


def decode(payload: bytes) -> PathData:
    """The path ``payload`` holds."""
    if payload[:3] == _MAC_HEADER:
        return PathData(SetOperatingSystem.MAC_OS, _mac_path(payload))
    return PathData(SetOperatingSystem.WINDOWS_OS, _windows_path(payload))


def read(text: str) -> PathData | None:
    """The path in a ``Data`` element's hex text, or None when it holds none."""
    payload = hex_text.decode(text)
    return decode(payload) if payload else None


def encode(path: str) -> bytes:
    """``path`` in the Windows form: UTF-16LE and a terminating NUL."""
    return path.encode("utf-16-le") + b"\x00\x00"


def _windows_path(payload: bytes) -> str | None:
    try:
        return payload.decode("utf-16").replace("\x00", "")
    except UnicodeDecodeError as error:
        logger.error("\n\n%s Couldn't decode: %s, Error: %s\n\n", R, payload.hex().upper(), error)
    return None


def _mac_path(payload: bytes) -> str | None:
    """The alias record's last two length-prefixed fields: the path, then the volume.

    The record is ``length:data:NUL`` fields among stretches that follow no
    pattern at all, so every run that parses as a field is collected and the
    last two are taken.
    """
    last_index = len(payload) - 1
    fields: list[bytes] = []
    i = _MAC_FIELDS_START
    while i <= last_index:
        if payload[i] in (0, 0xFF):
            i += 1
            continue
        end = i + payload[i] + 1  # Where the terminating NUL would be.
        if end < last_index and 0 not in payload[i + 1 : end] and payload[end] == 0:
            fields.append(payload[i + 1 : end])
            i = end + 1
            continue
        i += 1
    if len(fields) < 2:
        return None
    try:
        return f"{fields[-1].decode()}{fields[-2].decode()}"
    except UnicodeDecodeError as error:
        logger.error("\n\n%s Couldn't decode: %s, Error: %s\n\n", R, payload.hex().upper(), error)
    return None
