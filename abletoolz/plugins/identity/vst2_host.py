"""Read an installed VST2's own record of itself, run as a child process.

Live writes a VST2 device's host record -- inputs, outputs, parameter and
program counts, flags, version, category -- from what the plugin reports when it
loads, so a device written for another plugin takes them from that plugin.
They are all in the ``AEffect`` its entry point returns, plus two dispatcher
answers: its category and the VST version it speaks. Loading third-party code
can crash or hang the process, which is why
:mod:`abletoolz.plugins.identity.vst2_records` runs this in a child with a
timeout; it prints one JSON object.
"""

from __future__ import annotations

import ctypes
import json
import pathlib
import sys
from ctypes import CFUNCTYPE, POINTER, Structure, c_char, c_float, c_int32, c_ssize_t, c_void_p
from typing import Any

_MAGIC = int.from_bytes(b"VstP", "big")
_AUDIO_MASTER_VERSION = 1
_HOST_VST_VERSION = 2400
_EFF_OPEN = 0
_EFF_GET_PLUG_CATEGORY = 35
_EFF_GET_VST_VERSION = 58

_HostCallback = CFUNCTYPE(c_ssize_t, c_void_p, c_int32, c_int32, c_ssize_t, c_void_p, c_float)


class _AEffect(Structure):
    _fields_ = [
        ("magic", c_int32),
        ("dispatcher", c_void_p),
        ("process", c_void_p),
        ("set_parameter", c_void_p),
        ("get_parameter", c_void_p),
        ("num_programs", c_int32),
        ("num_params", c_int32),
        ("num_inputs", c_int32),
        ("num_outputs", c_int32),
        ("flags", c_int32),
        ("reserved1", c_ssize_t),
        ("reserved2", c_ssize_t),
        ("initial_delay", c_int32),
        ("real_qualities", c_int32),
        ("off_qualities", c_int32),
        ("io_ratio", c_float),
        ("object", c_void_p),
        ("user", c_void_p),
        ("unique_id", c_int32),
        ("version", c_int32),
        ("process_replacing", c_void_p),
        ("process_double_replacing", c_void_p),
        ("future", c_char * 56),
    ]


_Dispatcher = CFUNCTYPE(c_ssize_t, POINTER(_AEffect), c_int32, c_int32, c_ssize_t, c_void_p, c_float)


def _answer(effect: Any, opcode: int, index: int, value: int, ptr: Any, opt: float) -> int:
    return _HOST_VST_VERSION if opcode == _AUDIO_MASTER_VERSION else 0


_host = _HostCallback(_answer)


def read_record(path: pathlib.Path) -> dict[str, int]:
    """What the VST2 at ``path`` reports about itself, by the field names Live writes."""
    if sys.platform != "win32":
        raise OSError("reading a VST2's own record is only implemented on Windows")
    library = ctypes.WinDLL(str(path))
    entry = getattr(library, "VSTPluginMain", None) or getattr(library, "main", None)
    if entry is None:
        raise LookupError(f"{path.name} exports no VST2 entry point")
    entry.restype = POINTER(_AEffect)
    entry.argtypes = [_HostCallback]
    pointer = entry(_host)
    if not pointer:
        raise RuntimeError(f"{path.name} returned no plugin")
    effect = pointer.contents
    if effect.magic != _MAGIC:
        raise RuntimeError(f"{path.name} returned something that is not a VST2 plugin")
    dispatch = _Dispatcher(effect.dispatcher)
    dispatch(pointer, _EFF_OPEN, 0, 0, None, 0.0)
    return {
        "UniqueId": effect.unique_id,
        "Inputs": effect.num_inputs,
        "Outputs": effect.num_outputs,
        "NumberOfParameters": effect.num_params,
        "NumberOfPrograms": effect.num_programs,
        "Flags": effect.flags,
        "Version": effect.version,
        "VstVersion": dispatch(pointer, _EFF_GET_VST_VERSION, 0, 0, None, 0.0),
        "Category": dispatch(pointer, _EFF_GET_PLUG_CATEGORY, 0, 0, None, 0.0),
    }


if __name__ == "__main__":
    print(json.dumps(read_record(pathlib.Path(sys.argv[1]))), flush=True)
    # A plugin can hang in its DLL detach, which even os._exit runs; the answer is out, so skip it.
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = c_void_p
    kernel32.TerminateProcess(c_void_p(kernel32.GetCurrentProcess()), 0)
