"""Read sample names and metadata within explicitly selected directory trees.

Every walk finds audio by default; a caller passes ``suffixes`` to find other
files too, such as the kits and instruments plugin patches point at. The
``._`` files macOS writes beside every file it copies to a foreign drive
(resource forks, often under ``__MACOSX``) carry the suffix of the file they
sit beside but hold no audio, so no walk finds them.
"""

from __future__ import annotations

import ctypes
import logging
import os
import pathlib
import struct
import sys
from collections.abc import Iterator
from ctypes import wintypes
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

type DiscoveryBackend = Literal["auto", "portable"]
AUDIO_SUFFIXES = frozenset({".aiff", ".aif", ".wav", ".mp3", ".flac", ".ogg", ".mp4", ".rx2"})


def _is_wanted(name: str, suffixes: frozenset[str]) -> bool:
    """Whether a file called ``name`` is one a walk finds: one of ``suffixes``, and not a macOS ``._`` file."""
    return not name.startswith("._") and os.path.splitext(name)[1].lower() in suffixes


def _skip_unreadable(directory: str | pathlib.Path, root: pathlib.Path, error: PermissionError) -> None:
    """Warn about a folder the walk may not open and let it carry on, unless it is the root the caller named.

    A drive root holds folders like ``System Volume Information`` that nobody
    may list; one of those must not end the scan of everything around it.
    """
    if pathlib.Path(directory) == root:
        raise error
    logger.warning("Skipping a folder that cannot be read: %s (%s)", directory, error)


@dataclass(frozen=True, slots=True)
class FileMetadata:
    st_size: int
    st_mtime_ns: int
    st_dev: int
    st_ino: int

    @property
    def st_mtime(self) -> float:
        return self.st_mtime_ns / 1_000_000_000


@dataclass(frozen=True, slots=True)
class AudioFile:
    path: str
    name: str
    metadata: FileMetadata

    def stat(self) -> FileMetadata:
        return self.metadata


def audio_entries(root: pathlib.Path, suffixes: frozenset[str] = AUDIO_SUFFIXES) -> Iterator[os.DirEntry[str]]:
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            scanner = os.scandir(directory)
        except PermissionError as error:
            _skip_unreadable(directory, root, error)
            continue
        with scanner as entries:
            for entry in entries:
                if entry.is_symlink() or entry.is_junction():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(pathlib.Path(entry.path))
                elif _is_wanted(entry.name, suffixes) and entry.is_file():
                    yield entry


def discover_audio_files(
    root: pathlib.Path, backend: DiscoveryBackend = "auto", suffixes: frozenset[str] = AUDIO_SUFFIXES
) -> Iterator[AudioFile]:
    if backend == "auto" and sys.platform == "win32":
        yield from _windows_files(root, suffixes)
    elif backend == "auto" and sys.platform == "darwin":
        yield from _macos_files(root, suffixes)
    else:
        for entry in audio_entries(root, suffixes):
            try:
                info = pathlib.Path(entry.path).stat() if os.name == "nt" else entry.stat()
            except FileNotFoundError:
                continue
            yield AudioFile(
                entry.path,
                entry.name,
                FileMetadata(
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_dev,
                    info.st_ino,
                ),
            )


def _windows_files(root: pathlib.Path, suffixes: frozenset[str]) -> Iterator[AudioFile]:
    """Get FILE_ID_EXTD_DIR_INFO batches from each opened directory, without volume enumeration."""
    if sys.platform != "win32":
        raise ValueError("Windows discovery requires Windows")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    volume_id = root.stat().st_dev
    pending = [str(root)]
    buffer = ctypes.create_string_buffer(64 * 1024)
    while pending:
        directory = pending.pop()
        # OPEN_REPARSE_POINT prevents an encountered directory link from being followed.
        handle = kernel.CreateFileW(directory, 1, 7, None, 3, 0x02200000, None)
        if handle == wintypes.HANDLE(-1).value:
            failure = ctypes.WinError(ctypes.get_last_error())
            if not isinstance(failure, PermissionError):
                raise failure
            _skip_unreadable(directory, root, failure)
            continue
        try:
            while kernel.GetFileInformationByHandleEx(handle, 19, buffer, len(buffer)):
                data = buffer.raw
                offset = 0
                while True:
                    if offset + 88 > len(data):
                        raise OSError("Truncated Windows directory record")
                    following = struct.unpack_from("<I", data, offset)[0]
                    modified, size = (
                        struct.unpack_from("<q", data, offset + 24)[0],
                        struct.unpack_from("<q", data, offset + 40)[0],
                    )
                    attributes, name_length = struct.unpack_from("<II", data, offset + 56)
                    end = offset + 88 + name_length
                    record_end = offset + following if following else len(data)
                    if name_length % 2 or end > record_end or record_end > len(data):
                        raise OSError("Invalid Windows directory record bounds")
                    name = data[offset + 88 : end].decode("utf-16-le")
                    if name not in (".", "..") and not attributes & 0x400:
                        path = os.path.join(directory, name)
                        if attributes & 0x10:
                            pending.append(path)
                        elif _is_wanted(name, suffixes):
                            file_id = int.from_bytes(data[offset + 72 : offset + 88], "little")
                            yield AudioFile(
                                path,
                                name,
                                FileMetadata(
                                    size,
                                    (modified - 116444736000000000) * 100,
                                    volume_id,
                                    file_id,
                                ),
                            )
                    if not following:
                        break
                    offset += following
            error = ctypes.get_last_error()
            if error != 18:
                raise ctypes.WinError(error)
        finally:
            kernel.CloseHandle(handle)


def _macos_files(root: pathlib.Path, suffixes: frozenset[str]) -> Iterator[AudioFile]:
    """Read getattrlistbulk batches; fixed common fields use Apple's four-byte attribute alignment."""
    if sys.platform != "darwin":
        raise ValueError("macOS discovery requires macOS")

    class Attributes(ctypes.Structure):
        _fields_ = [
            ("bitmapcount", ctypes.c_ushort),
            ("reserved", ctypes.c_ushort),
            ("common", ctypes.c_uint32),
            ("volume", ctypes.c_uint32),
            ("directory", ctypes.c_uint32),
            ("file", ctypes.c_uint32),
            ("fork", ctypes.c_uint32),
        ]

    library = ctypes.CDLL(None, use_errno=True)
    library.getattrlistbulk.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(Attributes),
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint64,
    ]
    library.getattrlistbulk.restype = ctypes.c_int
    common = 0x80000000 | 0x20000000 | 0x1 | 0x2 | 0x8 | 0x400 | 0x40000 | 0x02000000
    attributes = Attributes(5, 0, common, 0, 4, 0x200, 0)
    buffer = ctypes.create_string_buffer(64 * 1024)
    pending = [str(root)]
    while pending:
        directory = pending.pop()
        directory_flag: int = os.O_DIRECTORY
        nofollow_flag: int = os.O_NOFOLLOW
        try:
            descriptor = os.open(directory, os.O_RDONLY | directory_flag | nofollow_flag)
        except PermissionError as denied:
            _skip_unreadable(directory, root, denied)
            continue
        try:
            while True:
                count = library.getattrlistbulk(descriptor, ctypes.byref(attributes), buffer, len(buffer), 8)
                if count < 0:
                    error = ctypes.get_errno()
                    raise OSError(error, os.strerror(error), directory)
                if count == 0:
                    break
                data = buffer.raw
                offset = 0
                for _index in range(count):
                    if offset + 72 > len(data):
                        raise OSError("Truncated macOS directory record")
                    length = struct.unpack_from("<I", data, offset)[0]
                    returned = struct.unpack_from("<IIIII", data, offset + 4)
                    error = struct.unpack_from("<I", data, offset + 24)[0]
                    if returned[0] & 0x20000000 and error:
                        raise OSError(error, os.strerror(error), directory)
                    required = common & ~(0x80000000 | 0x20000000)
                    if returned[0] & required != required:
                        raise OSError("macOS did not return required sample metadata")
                    name_offset, name_length = struct.unpack_from("<iI", data, offset + 28)
                    name_start = offset + 28 + name_offset
                    name_end = name_start + name_length
                    fields_end = offset + 72
                    # Directory and file attribute groups depend on the returned object type.
                    mount_status = 0
                    size = 0
                    if returned[2] & 4:
                        mount_status = struct.unpack_from("<I", data, fields_end)[0]
                        fields_end += 4
                    if returned[3] & 0x200:
                        size = struct.unpack_from("<q", data, fields_end)[0]
                        fields_end += 8
                    if (
                        offset + length < fields_end
                        or offset + length > len(data)
                        or not (fields_end <= name_start < name_end <= offset + length)
                        or data[name_end - 1] != 0
                    ):
                        raise OSError("Invalid macOS directory record bounds")
                    name = os.fsdecode(data[name_start : name_end - 1])
                    device, kind = struct.unpack_from("<II", data, offset + 36)
                    seconds, nanoseconds = struct.unpack_from("<qq", data, offset + 44)
                    flags, file_id = struct.unpack_from("<IQ", data, offset + 60)
                    path = os.path.join(directory, name)
                    if kind == 2 and not (flags & 0x00800000 or mount_status):
                        pending.append(path)
                    elif kind == 1 and _is_wanted(name, suffixes):
                        if not returned[3] & 0x200:
                            raise OSError(f"macOS did not return file size: {path}")
                        yield AudioFile(
                            path,
                            name,
                            FileMetadata(
                                size,
                                seconds * 1_000_000_000 + nanoseconds,
                                device,
                                file_id,
                            ),
                        )
                    offset += length
        finally:
            os.close(descriptor)
