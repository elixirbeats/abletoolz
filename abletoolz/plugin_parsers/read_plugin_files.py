"""Read plugin metadata without loading code.

Windows keeps a plugin in a file and macOS in a bundle directory, so the two
sides read different things -- a version resource and a PE header against an
Info.plist and a Mach-O header -- to answer the same four questions: what is it
called, who made it, which version, and which architecture.
"""

from __future__ import annotations

import ctypes
import pathlib
import struct
import sys
from collections.abc import Callable, Iterable, Mapping
from functools import lru_cache

from abletoolz.misc import default_vst_dirs, read_plist


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


@lru_cache(maxsize=8192)
def _get_file_version_strings(path: pathlib.Path) -> dict[str, str]:
    """Return version resource strings like ProductName, FileDescription.

    A version resource is a Windows file format read through a Windows API, so
    every other OS gets an empty answer rather than a guess -- and ``windll``
    only exists to a type checker on Windows, so the guard has to be here.
    """
    strings: dict[str, str] = {}
    if sys.platform != "win32":
        return strings
    try:
        size = ctypes.windll.version.GetFileVersionInfoSizeW(str(path), None)
        if size == 0:
            return strings
        buf = ctypes.create_string_buffer(size)
        ok = ctypes.windll.version.GetFileVersionInfoW(str(path), 0, size, buf)
        if ok == 0:
            return strings
        lptr = ctypes.c_void_p()
        lsize = ctypes.c_uint32()
        # get list of languages/codepages
        ctypes.windll.version.VerQueryValueW(buf, "\\VarFileInfo\\Translation", ctypes.byref(lptr), ctypes.byref(lsize))
        translations = []
        if lptr.value and lsize.value >= 4:
            for i in range(0, lsize.value, 4):
                lang, code = struct.unpack_from("<HH", ctypes.string_at(lptr.value + i, 4))
                translations.append(f"{lang:04x}{code:04x}")
        if not translations:
            translations = ["040904b0", "040904E4"]  # en-US fallbacks
        keys = ["ProductName", "FileDescription", "CompanyName", "FileVersion", "OriginalFilename"]
        for t in translations:
            for key in keys:
                sub_block = f"\\StringFileInfo\\{t}\\{key}"
                lptr = ctypes.c_void_p()
                lsize = ctypes.c_uint32()
                found = ctypes.windll.version.VerQueryValueW(buf, sub_block, ctypes.byref(lptr), ctypes.byref(lsize))
                # A truthy answer means the pointer was written; asking for the
                # address as well is what says so in a way a type checker reads.
                if found and lptr.value:
                    val = ctypes.wstring_at(lptr.value, lsize.value)
                    if val:
                        strings[key] = val.strip("\x00")
        return strings
    except Exception:
        # Broad on purpose: this walks raw ctypes/Windows version-resource structs off
        # arbitrary third-party plugin DLLs, which can be truncated or malformed in ways
        # that surface as almost anything (OSError, struct.error, access violations
        # ctypes turns into other exceptions). One bad file must not kill the whole scan.
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


# -- macOS bundles ----------------------------------------------------------
# A mac plugin is a directory: Thing.vst3/Contents/Info.plist is where its name
# and version are written down, and Contents/MacOS/<CFBundleExecutable> is the
# binary whose Mach-O header says which processors it was built for.
#
# .component (Audio Unit) bundles are left out. An AU is identified by its
# type/subtype/manufacturer codes rather than by name and path, and a scan
# record has nowhere to put them; abletoolz.live_set.plugins indexes components
# by that identity instead.
_BUNDLE_FORMATS: dict[str, str] = {".vst": "VST2", ".vst3": "VST3"}

# Mach-O magics: the four single-architecture headers, each with the byte order
# to read the cpu type in, and the two fat headers, each with the size of the
# per-architecture record that follows the count.
_THIN_MAGICS: dict[bytes, str] = {
    b"\xcf\xfa\xed\xfe": "<",
    b"\xce\xfa\xed\xfe": "<",
    b"\xfe\xed\xfa\xcf": ">",
    b"\xfe\xed\xfa\xce": ">",
}
_FAT_MAGICS: dict[bytes, int] = {b"\xca\xfe\xba\xbe": 20, b"\xca\xfe\xba\xbf": 32}
_FAT_HEADER = 8  # magic and architecture count
_MAX_SLICES = 16  # a shipped universal binary holds two or three; the cap bounds a corrupt count

# cpu_type_t, the 0x01000000 bit meaning 64 bit. Verified 2026-08-22 against
# /System/Library/Components/CoreAudio.component, a fat binary of x86_64 + arm64.
_CPU_TYPES: dict[int, str] = {7: "x86", 12: "arm", 0x01000007: "x86_64", 0x0100000C: "arm64"}


@lru_cache(maxsize=8192)
def _read_macho_arch(path: pathlib.Path) -> str | None:
    """Return the architecture a Mach-O declares: one name, or 'universal' for several."""
    try:
        with path.open("rb") as fd:
            magic = fd.read(4)
            byte_order = _THIN_MAGICS.get(magic)
            if byte_order is not None:
                return _CPU_TYPES.get(struct.unpack(f"{byte_order}I", fd.read(4))[0])
            slice_size = _FAT_MAGICS.get(magic)
            if slice_size is None:
                return None
            count = struct.unpack(">I", fd.read(4))[0]
            names: list[str] = []
            for index in range(min(count, _MAX_SLICES)):
                fd.seek(_FAT_HEADER + index * slice_size)
                name = _CPU_TYPES.get(struct.unpack(">I", fd.read(4))[0])
                if name is not None and name not in names:
                    names.append(name)
    except (OSError, struct.error):
        return None
    if not names:
        return None
    return names[0] if len(names) == 1 else "universal"


def _plist_text(plist: Mapping[str, object], *keys: str) -> str | None:
    """First of these keys the plist declares as a non-empty string."""
    for key in keys:
        value = plist.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _bundle_binary(bundle: pathlib.Path, plist: Mapping[str, object]) -> pathlib.Path | None:
    """The binary a bundle runs: the executable its plist names, else what MacOS holds."""
    macos = bundle / "Contents" / "MacOS"
    executable = _plist_text(plist, "CFBundleExecutable")
    if executable is not None and (macos / executable).is_file():
        return macos / executable
    return next((entry for entry in sorted(macos.glob("*")) if entry.is_file()), None)


def scan_mac_bundles(dir_path: pathlib.Path) -> list[dict[str, str]]:
    """Scan macOS .vst/.vst3 bundles and extract names/version/arch.

    No company: an Info.plist describes the product, not who made it, so the
    vendor Windows reads off a version resource has no equivalent to read here.
    """
    results: list[dict[str, str]] = []
    for suffix, plugin_format in _BUNDLE_FORMATS.items():
        for bundle in dir_path.rglob(f"*{suffix}"):
            plist_path = bundle / "Contents" / "Info.plist"
            if not plist_path.is_file():
                continue
            plist = read_plist(plist_path)
            binary = _bundle_binary(bundle, plist)
            arch = None if binary is None else _read_macho_arch(binary)
            record = {
                "format": plugin_format,
                "name": _plist_text(plist, "CFBundleDisplayName", "CFBundleName") or bundle.stem,
                "company": "",
                "version": _plist_text(plist, "CFBundleShortVersionString", "CFBundleVersion") or "",
                "arch": arch or "unknown",
                "path": str(bundle),
            }
            if binary is not None:
                record["module"] = str(binary)
            results.append(record)
    return results


# Which readers a folder gets, first one to reach a plugin describing it. A Mac
# reads the file forms too -- a Windows plugin folder on a shared drive is a real
# thing to point a scan at, and a PE header reads the same from either OS -- but
# it reads bundles first, so a bundle carrying both platforms' binaries is
# described by the one this OS would load.
_SCANNERS: dict[str, tuple[Callable[[pathlib.Path], list[dict[str, str]]], ...]] = {
    "win32": (_scan_vst2, _scan_vst3),
    "darwin": (scan_mac_bundles, _scan_vst2, _scan_vst3),
}


def scan_plugin_dirs(paths: list[pathlib.Path], *, platform: str = sys.platform) -> list[dict[str, str]]:
    """Scan given directories for VST2/VST3 metadata.

    ``platform`` picks the readers, so asking what a Mac would find in a folder
    does not take a Mac. One plugin is reported once however many readers reach
    it, and however many of the given directories contain it.
    """
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for base in paths:
        if not base.exists():
            continue
        for scanner in _SCANNERS.get(platform, ()):
            for record in scanner(base):
                if record["path"] not in seen:
                    seen.add(record["path"])
                    results.append(record)
    return results


if __name__ == "__main__":
    for rec in scan_plugin_dirs(default_vst_dirs()):
        print(f"{rec['format']:4} {rec['arch']:4} {rec['name']}  [{rec.get('company', '')}] -> {rec['path']}")
