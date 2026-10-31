"""A VST3 plugin's own list of its parameters, asked of the plugin itself.

Just enough of a VST3 host to read that list: load the module, take its
factory, create the audio class with the class id a set stores, get its edit
controller, and ask ``IEditController`` for every parameter's id, title, step
count, flags and range in the plugin's own units. Windows only, over ctypes.

It runs as its own process -- ``python -m abletoolz.plugins.identity.vst3_host
MODULE F0 F1 F2 F3`` prints the list as JSON -- because a third-party binary
that crashes or hangs while loading must cost that one plugin, never the run.
:mod:`abletoolz.plugins.identity.vst3_parameters` is what calls it.

Measured 2026-10-24 on FabFilter Pro-C 2, Pro-L 2 and Pro-G: the ranges read
here are the ones Live writes into a device's parameter slots (Pro-C 2's Style
0 to 7, Threshold -60 to 0, Output Level -1 to 1).
"""

from __future__ import annotations

import ctypes
import json
import pathlib
import struct
import sys
import uuid
from collections.abc import Callable
from ctypes import POINTER, byref, c_char, c_double, c_int32, c_ubyte, c_uint32, c_void_p, c_wchar
from typing import Any

_RESULT = c_int32  # tresult: 0 is kResultOk
_TUID = c_char * 16
# Elsewhere only so the module imports: on 64-bit the two calling conventions are one.
_FUNCTYPE = ctypes.WINFUNCTYPE if sys.platform == "win32" else ctypes.CFUNCTYPE


def _iid(text: str) -> bytes:
    """An interface id as Windows lays it out: the GUID's little-endian bytes."""
    return uuid.UUID(text).bytes_le


_IID_I_COMPONENT = _iid("E831FF31-F2D5-4301-928E-BBEE25697802")
_IID_I_EDIT_CONTROLLER = _iid("DCD7BBE3-7742-448D-A874-AACC979C759E")
_IID_I_CONNECTION_POINT = _iid("70A4156F-6E6E-4026-9891-48BFAA60D8D1")
_IID_I_HOST_APPLICATION = _iid("58E595CC-DB2D-4969-8B6A-AF8C36A664E5")
_IID_F_UNKNOWN = _iid("00000000-0000-0000-C000-000000000046")

_AUDIO_MODULE_CLASS = b"Audio Module Class"

FLAG_NAMES = {
    0x1: "can_automate",
    0x2: "read_only",
    0x4: "wrap_around",
    0x8: "list",
    0x10: "hidden",
    0x8000: "program_change",
    0x10000: "bypass",
}
"""``ParameterInfo::ParameterFlags``, by the names this module reports them under."""


class _ClassInfo(ctypes.Structure):
    # Raw bytes, not c_char: a c_char array reads only up to its first NUL, and class ids hold them.
    _fields_ = [("cid", c_ubyte * 16), ("cardinality", c_int32), ("category", c_char * 32), ("name", c_char * 64)]


class _ParameterInfo(ctypes.Structure):
    _fields_ = [
        ("id", c_uint32),
        ("title", c_wchar * 128),
        ("short_title", c_wchar * 128),
        ("units", c_wchar * 128),
        ("step_count", c_int32),
        ("default_normalized", c_double),
        ("unit_id", c_int32),
        ("flags", c_int32),
    ]


def _method(obj: c_void_p, index: int, restype: Any, *argtypes: Any) -> Callable[..., Any]:
    """The ``index``th virtual method of a COM-style object, bound to it."""
    vtable = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0]
    function = _FUNCTYPE(restype, c_void_p, *argtypes)(vtable[index])
    return lambda *args: function(obj, *args)


# A host context for plugins that refuse to initialize without one: an
# IHostApplication that names itself and creates nothing.
_QUERY = _FUNCTYPE(_RESULT, c_void_p, _TUID, POINTER(c_void_p))
_COUNT = _FUNCTYPE(c_uint32, c_void_p)
_NAME = _FUNCTYPE(_RESULT, c_void_p, POINTER(c_wchar * 128))
_CREATE = _FUNCTYPE(_RESULT, c_void_p, _TUID, _TUID, POINTER(c_void_p))


class _HostTable(ctypes.Structure):
    _fields_ = [("query", _QUERY), ("add_ref", _COUNT), ("release", _COUNT), ("name", _NAME), ("create", _CREATE)]


class _Host(ctypes.Structure):
    _fields_ = [("table", POINTER(_HostTable))]


def _query(this: int, iid: Any, out: Any) -> int:
    if bytes(iid) in (_IID_I_HOST_APPLICATION, _IID_F_UNKNOWN):
        out[0] = this
        return 0
    out[0] = None
    return -1


def _host_name(this: int, out: Any) -> int:
    out.contents.value = "abletoolz"
    return 0


_HOST_TABLE = _HostTable(
    _QUERY(_query), _COUNT(lambda this: 1), _COUNT(lambda this: 1), _NAME(_host_name), _CREATE(lambda *args: -1)
)
_HOST = _Host(ctypes.pointer(_HOST_TABLE))


def _connect(component: c_void_p, controller: c_void_p) -> None:
    """Join a component and its separate controller the way a host does, through IConnectionPoint.

    A JUCE plugin's controller learns its parameters over this connection and
    lists none without it (measured on ValhallaShimmer and Ghz Tupe 3).
    """
    points = []
    for unknown in (component, controller):
        point = c_void_p()
        query = _method(unknown, 0, _RESULT, _TUID, POINTER(c_void_p))
        if query(_TUID.from_buffer_copy(_IID_I_CONNECTION_POINT), byref(point)) != 0:
            return
        points.append(point)
    _method(points[0], 3, _RESULT, c_void_p)(points[1])
    _method(points[1], 3, _RESULT, c_void_p)(points[0])


def class_id(uid_fields: tuple[int, int, int, int]) -> bytes:
    """The 16 byte class id a factory names, from the four signed fields a set stores.

    Live writes the class GUID as four big-endian 32 bit words; Windows lays the
    same GUID out little-endian. Measured on Pro-C 2: the factory's
    ``e315f479e7c80748...`` is the set's ``(2046039523, -924366841, ...)``.
    """
    return uuid.UUID(bytes=struct.pack(">iiii", *uid_fields)).bytes_le


def _module_file(path: pathlib.Path) -> pathlib.Path:
    """The binary inside a ``.vst3``: the file itself, or the bundle's x86_64-win binary."""
    if path.is_file():
        return path
    binaries = path / "Contents" / "x86_64-win"
    inner = binaries / path.name
    return inner if inner.exists() else next(binaries.glob("*.vst3"))


def read_parameters(path: pathlib.Path, uid_fields: tuple[int, int, int, int]) -> list[dict[str, object]]:
    """Every parameter of the class ``uid_fields`` names in the module at ``path``, in the plugin's order."""
    if sys.platform != "win32":
        raise OSError("reading a VST3's parameters is only implemented on Windows")
    library = ctypes.WinDLL(str(_module_file(path)))
    if hasattr(library, "InitDll"):
        library.InitDll()
    get_factory = library.GetPluginFactory
    get_factory.restype = c_void_p
    factory = c_void_p(get_factory())
    count_classes = _method(factory, 4, c_int32)
    class_info = _method(factory, 5, _RESULT, c_int32, POINTER(_ClassInfo))
    create = _method(factory, 6, _RESULT, _TUID, _TUID, POINTER(c_void_p))

    wanted = class_id(uid_fields)
    for index in range(count_classes()):
        info = _ClassInfo()
        class_info(index, byref(info))
        if info.category == _AUDIO_MODULE_CLASS and bytes(info.cid) == wanted:
            break
    else:
        raise LookupError(f"{path.name} has no audio class {wanted.hex()}")

    component = c_void_p()
    if create(_TUID.from_buffer_copy(wanted), _TUID.from_buffer_copy(_IID_I_COMPONENT), byref(component)) != 0:
        raise RuntimeError(f"{path.name} would not create its component")
    _method(component, 3, _RESULT, c_void_p)(ctypes.addressof(_HOST))  # IPluginBase::initialize

    controller = c_void_p()
    # A single-component plugin is its own controller; the rest name a controller class.
    query = _method(component, 0, _RESULT, _TUID, POINTER(c_void_p))
    if query(_TUID.from_buffer_copy(_IID_I_EDIT_CONTROLLER), byref(controller)) != 0:
        controller_class = _TUID()
        _method(component, 5, _RESULT, _TUID)(controller_class)  # IComponent::getControllerClassId
        if create(controller_class, _TUID.from_buffer_copy(_IID_I_EDIT_CONTROLLER), byref(controller)) != 0:
            raise RuntimeError(f"{path.name} would not create its edit controller")
        _method(controller, 3, _RESULT, c_void_p)(ctypes.addressof(_HOST))
        _connect(component, controller)

    count = _method(controller, 8, c_int32)
    parameter_info = _method(controller, 9, _RESULT, c_int32, POINTER(_ParameterInfo))
    to_plain = _method(controller, 12, c_double, c_uint32, c_double)  # normalizedParamToPlain
    parameters: list[dict[str, object]] = []
    for index in range(count()):
        parameter = _ParameterInfo()
        parameter_info(index, byref(parameter))
        parameters.append(
            {
                "id": parameter.id,
                "title": parameter.title,
                "steps": parameter.step_count,
                "flags": [name for bit, name in FLAG_NAMES.items() if parameter.flags & bit],
                "minimum": to_plain(parameter.id, 0.0),
                "maximum": to_plain(parameter.id, 1.0),
                "default": to_plain(parameter.id, parameter.default_normalized),
            }
        )
    return parameters


if __name__ == "__main__":
    fields = (int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5]))
    print(json.dumps(read_parameters(pathlib.Path(sys.argv[1]), fields)), flush=True)
    # Some plugins (Prophet-5 V) hang in their DLL detach, which even os._exit runs; the answer is out, so skip it.
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = c_void_p
    kernel32.TerminateProcess(c_void_p(kernel32.GetCurrentProcess()), 0)
