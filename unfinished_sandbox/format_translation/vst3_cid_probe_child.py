"""Read the class ids out of one VST3 module, in a process of its own.

A VST3 module is a DLL exporting ``GetPluginFactory``, which hands back a COM
object whose vtable enumerates the classes the module provides. Reading that
enumeration is the only way to learn a single-file plugin's class ids: bundles
declare them in moduleinfo.json, single-file .vst3 plugins declare them nowhere.

Every plugin here is third-party native code loaded into this interpreter, so
this runs as its own process: a module that crashes in DllMain, blocks on a
licence dialog or refuses to unload takes only this process with it. The
orchestrator kills it on a timeout and moves on.

``createInstance`` (vtable slot 6) is never resolved, let alone called -- that
is where plugins run their authorisation UI.

Usage: ``python vst3_cid_probe_child.py <module path>``. Result JSON is written
to stdout between the two sentinel lines, because plugins print to stdout
themselves while loading.
"""

from __future__ import annotations

import ctypes
import dataclasses
import json
import os
import pathlib
import sys
from ctypes import wintypes

BEGIN = "<<<VST3PROBE"
END = "VST3PROBE>>>"


class PFactoryInfo(ctypes.Structure):
    """Steinberg::PFactoryInfo -- who wrote the module."""

    _fields_ = [
        ("vendor", ctypes.c_char * 64),
        ("url", ctypes.c_char * 256),
        ("email", ctypes.c_char * 128),
        ("flags", ctypes.c_int32),
    ]


class PClassInfo(ctypes.Structure):
    """Steinberg::PClassInfo -- one class the factory offers.

    ``cid`` is a raw byte array rather than ``c_char * 16`` on purpose: a class
    id contains NUL bytes, which ctypes' char arrays truncate at.
    """

    _fields_ = [
        ("cid", ctypes.c_ubyte * 16),
        ("cardinality", ctypes.c_int32),
        ("category", ctypes.c_char * 32),
        ("name", ctypes.c_char * 64),
    ]


_GET_PLUGIN_FACTORY = ctypes.WINFUNCTYPE(ctypes.c_void_p)
_INIT_DLL = ctypes.WINFUNCTYPE(wintypes.BOOL)
_EXIT_DLL = ctypes.WINFUNCTYPE(wintypes.BOOL)
_GET_FACTORY_INFO = ctypes.WINFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.POINTER(PFactoryInfo))
_COUNT_CLASSES = ctypes.WINFUNCTYPE(ctypes.c_int32, ctypes.c_void_p)
_GET_CLASS_INFO = ctypes.WINFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_int32, ctypes.POINTER(PClassInfo))

# IPluginFactory's vtable, in declaration order. Slot 6 is createInstance and is
# deliberately absent: nothing here may instantiate a plugin.
_SLOT_GET_FACTORY_INFO = 3
_SLOT_COUNT_CLASSES = 4
_SLOT_GET_CLASS_INFO = 5
_VTABLE_SLOTS = 6

_K_RESULT_OK = 0


@dataclasses.dataclass(frozen=True, slots=True)
class ProbedClass:
    """One class as the factory describes it, byte for byte."""

    index: int
    cid_raw_hex: str
    cardinality: int
    category: str
    name: str


@dataclasses.dataclass(frozen=True, slots=True)
class ProbeResult:
    """What the module said about itself."""

    module: str
    ok: bool
    vendor: str = ""
    url: str = ""
    email: str = ""
    classes: list[ProbedClass] = dataclasses.field(default_factory=list)
    error: str = ""


def _vtable_slots(instance: int) -> list[int]:
    """The function pointers of a COM object's vtable.

    A COM object is a pointer to a struct whose first member points at the
    vtable, so this is two dereferences.
    """
    vtable = ctypes.c_void_p.from_address(instance).value
    if vtable is None:
        raise ValueError("factory has a null vtable")
    return [slot for slot in (ctypes.c_void_p * _VTABLE_SLOTS).from_address(vtable)]


def _read_classes(factory: int, slots: list[int]) -> list[ProbedClass]:
    """Every class the factory enumerates."""
    count = _COUNT_CLASSES(slots[_SLOT_COUNT_CLASSES])(factory)
    get_class_info = _GET_CLASS_INFO(slots[_SLOT_GET_CLASS_INFO])
    found: list[ProbedClass] = []
    for index in range(count):
        info = PClassInfo()
        if get_class_info(factory, index, ctypes.byref(info)) != _K_RESULT_OK:
            continue
        found.append(
            ProbedClass(
                index=index,
                cid_raw_hex=bytes(info.cid).hex().upper(),
                cardinality=info.cardinality,
                category=info.category.decode("utf-8", "replace"),
                name=info.name.decode("utf-8", "replace"),
            )
        )
    return found


def probe(module: pathlib.Path) -> ProbeResult:
    """Load ``module`` and read its factory. Any failure is a failed result, not a raise."""
    # A plugin's own DLLs sit beside it, or one level up in the bundle; without
    # these on the search path the load fails with a bare "module not found".
    for directory in {module.parent, module.parent.parent}:
        if directory.is_dir():
            os.add_dll_directory(str(directory))

    try:
        library = ctypes.WinDLL(str(module))
    except OSError as error:
        return ProbeResult(module=str(module), ok=False, error=f"load failed: {error}")

    try:
        get_factory = _GET_PLUGIN_FACTORY(("GetPluginFactory", library))
    except AttributeError:
        return ProbeResult(module=str(module), ok=False, error="no GetPluginFactory export")

    # InitDll is optional, and a module that has it may hand back a null factory
    # until it has run.
    if hasattr(library, "InitDll") and not _INIT_DLL(("InitDll", library))():
        return ProbeResult(module=str(module), ok=False, error="InitDll returned false")

    try:
        factory = get_factory()
        if not factory:
            return ProbeResult(module=str(module), ok=False, error="GetPluginFactory returned null")
        slots = _vtable_slots(factory)
        factory_info = PFactoryInfo()
        _GET_FACTORY_INFO(slots[_SLOT_GET_FACTORY_INFO])(factory, ctypes.byref(factory_info))
        classes = _read_classes(factory, slots)
    except (OSError, ValueError, ctypes.ArgumentError) as error:
        return ProbeResult(module=str(module), ok=False, error=f"factory walk failed: {type(error).__name__}: {error}")

    return ProbeResult(
        module=str(module),
        ok=True,
        vendor=factory_info.vendor.decode("utf-8", "replace"),
        url=factory_info.url.decode("utf-8", "replace"),
        email=factory_info.email.decode("utf-8", "replace"),
        classes=classes,
    )


def main() -> int:
    result = probe(pathlib.Path(sys.argv[1]))
    sys.stdout.flush()
    print(BEGIN)
    print(json.dumps(dataclasses.asdict(result)))
    print(END)
    sys.stdout.flush()
    # ExitDll is skipped on purpose. Several vendors crash or hang in it, and the
    # process is about to end anyway -- Windows reclaims everything.
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
