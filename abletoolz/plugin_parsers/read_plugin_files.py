"""Read plugin metadata without loading code."""

from __future__ import annotations

import ctypes
import os
import pathlib
import struct
from collections.abc import Iterable
from functools import lru_cache

from abletoolz.misc import default_vst_dirs


@lru_cache(maxsize=8192)
def _read_pe_arch(path: pathlib.Path) -> str | None:
    """Return 'x64' or 'x86' by parsing PE header."""
    try:
        with path.open("rb") as fd:
            mz = fd.read(2)
            if mz != b"MZ":
                return None
            fd.seek(0x3C)
            pe_off = struct.unpack("<I", fd.read(4))[0]
            fd.seek(pe_off)
            sig = fd.read(4)
            if sig != b"PE\x00\x00":
                return None
            fd.seek(pe_off + 0x18)  # OptionalHeader start
            magic = struct.unpack("<H", fd.read(2))[0]
            return "x64" if magic == 0x20B else ("x86" if magic == 0x10B else None)
    except OSError:
        return None


class _VS_FIXEDFILEINFO(ctypes.Structure):
    _fields_ = [
        ("dwSignature", ctypes.c_uint32),
        ("dwStrucVersion", ctypes.c_uint32),
        ("dwFileVersionMS", ctypes.c_uint32),
        ("dwFileVersionLS", ctypes.c_uint32),
        ("dwProductVersionMS", ctypes.c_uint32),
        ("dwProductVersionLS", ctypes.c_uint32),
        ("dwFileFlagsMask", ctypes.c_uint32),
        ("dwFileFlags", ctypes.c_uint32),
        ("dwFileOS", ctypes.c_uint32),
        ("dwFileType", ctypes.c_uint32),
        ("dwFileSubtype", ctypes.c_uint32),
        ("dwFileDateMS", ctypes.c_uint32),
        ("dwFileDateLS", ctypes.c_uint32),
    ]


@lru_cache(maxsize=8192)
def _get_file_version_strings(path: pathlib.Path) -> dict[str, str]:
    """Return version resource strings like ProductName, FileDescription."""
    strings: dict[str, str] = {}
    try:
        size = ctypes.windll.version.GetFileVersionInfoSizeW(str(path), None)  # type: ignore[attr-defined]
        if size == 0:
            return strings
        buf = ctypes.create_string_buffer(size)
        ok = ctypes.windll.version.GetFileVersionInfoW(str(path), 0, size, buf)  # type: ignore[attr-defined]
        if ok == 0:
            return strings
        lptr = ctypes.c_void_p()
        lsize = ctypes.c_uint32()
        # get list of languages/codepages
        ctypes.windll.version.VerQueryValueW(buf, "\\VarFileInfo\\Translation", ctypes.byref(lptr), ctypes.byref(lsize))  # type: ignore[attr-defined]
        translations = []
        if lptr.value and lsize.value >= 4:
            for i in range(0, lsize.value, 4):
                lang, code = struct.unpack_from("<HH", ctypes.string_at(lptr.value + i, 4))  # type: ignore[arg-type]
                translations.append(f"{lang:04x}{code:04x}")
        if not translations:
            translations = ["040904b0", "040904E4"]  # en-US fallbacks
        keys = ["ProductName", "FileDescription", "CompanyName", "FileVersion", "OriginalFilename"]
        for t in translations:
            for key in keys:
                sub_block = f"\\StringFileInfo\\{t}\\{key}"
                lptr = ctypes.c_void_p()
                lsize = ctypes.c_uint32()
                if ctypes.windll.version.VerQueryValueW(buf, sub_block, ctypes.byref(lptr), ctypes.byref(lsize)):  # type: ignore[attr-defined]
                    val = ctypes.wstring_at(lptr.value, lsize.value)  # type: ignore[arg-type]
                    if val:
                        strings[key] = val.strip("\x00")
        return strings
    except Exception:
        return strings


def _choose_name(info: dict[str, str], path: pathlib.Path) -> str:
    """Choose best human name."""
    return info.get("ProductName") or info.get("FileDescription") or path.stem


def _scan_vst2(dir_path: pathlib.Path) -> list[dict[str, str]]:
    """Scan VST2 .dll files and extract names/version/arch."""
    results: list[dict[str, str]] = []
    for dll in dir_path.rglob("*.dll"):
        info = _get_file_version_strings(dll)
        name = _choose_name(info, dll)
        arch = _read_pe_arch(dll) or "unknown"
        results.append(
            {
                "format": "VST2",
                "name": name,
                "company": info.get("CompanyName", ""),
                "version": info.get("FileVersion", ""),
                "arch": arch,
                "path": str(dll),
            }
        )
    return results


@lru_cache(maxsize=8192)
def _find_vst3_module(vst3_path: pathlib.Path) -> pathlib.Path | None:
    """Return module file within .vst3 bundle or file itself."""
    if vst3_path.is_file():
        return vst3_path
    for cand in vst3_path.rglob("*.vst3"):
        if cand.is_file():
            return cand
    # Some vendors ship DLL inside bundle
    for cand in vst3_path.rglob("*.dll"):
        if cand.is_file():
            return cand
    return None


def _scan_vst3(dir_path: pathlib.Path) -> list[dict[str, str]]:
    """Scan VST3 bundles/files and extract names/version/arch."""
    results: list[dict[str, str]] = []
    # Both directories and files with .vst3 extension
    cand_paths: Iterable[pathlib.Path] = list(dir_path.rglob("*.vst3"))
    for entry in cand_paths:
        module = _find_vst3_module(entry)
        if module is None:
            continue
        info = _get_file_version_strings(module)
        name = _choose_name(info, module)
        arch = _read_pe_arch(module) or "unknown"
        results.append(
            {
                "format": "VST3",
                "name": name,
                "company": info.get("CompanyName", ""),
                "version": info.get("FileVersion", ""),
                "arch": arch,
                "path": str(entry),
                "module": str(module),
            }
        )
    return results


def scan_plugin_dirs(paths: list[pathlib.Path]) -> list[dict[str, str]]:
    """Scan given directories for VST2/VST3 metadata on Windows."""
    results: list[dict[str, str]] = []
    for base in paths:
        if not base.exists():
            continue
        if os.name == "nt":
            results.extend(_scan_vst2(base))
            results.extend(_scan_vst3(base))
        # macOS support will be added later
    return results


if __name__ == "__main__":
    for rec in scan_plugin_dirs(default_vst_dirs()):
        print(f"{rec['format']:4} {rec['arch']:4} {rec['name']}  [{rec.get('company', '')}] -> {rec['path']}")
