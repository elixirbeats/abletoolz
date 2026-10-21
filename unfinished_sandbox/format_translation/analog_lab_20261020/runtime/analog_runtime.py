from __future__ import annotations

import argparse
import ctypes
import dataclasses
import hashlib
import importlib
import json
import struct
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Protocol, cast, override

import numpy as np
from numpy.typing import NDArray

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'legacy_archive_20261020'))

from host_process import attach_worker, run_worker  # noqa: E402
from load_probe import isolate_desktop  # noqa: E402
from vst3_cid_probe_child import probe  # noqa: E402


class Plugin(Protocol):
    def get_plugin_parameter_size(self) -> int: ...
    def get_parameter_name(self, index: int) -> str: ...
    def get_parameter(self, index: int) -> float: ...
    def get_parameter_text(self, index: int) -> str: ...
    def set_parameter(self, index: int, value: float) -> bool: ...
    def load_state(self, path: str) -> None: ...
    def add_midi_note(self, note: int, velocity: int, start_time: float, duration: float) -> bool: ...
    @override
    def __getstate__(self) -> dict[str, object]: ...


class Engine(Protocol):
    def make_plugin_processor(self, name: str, path: str) -> Plugin: ...
    def load_graph(self, graph: list[tuple[Plugin, list[str]]]) -> bool: ...
    def render(self, seconds: float) -> bool: ...
    def get_audio(self) -> NDArray[np.float32]: ...


class Factory(Protocol):
    def __call__(self, rate: int, block: int) -> Engine: ...


def vst2_entry(module: Path) -> dict[str, object]:
    callback_type = ctypes.CFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32,
                                    ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_float)
    calls: list[int] = []

    def host(effect: int, opcode: int, index: int, value: int, pointer: int, opt: float) -> int:
        calls.append(opcode)
        if opcode == 1:
            return 2400
        if opcode == 16:
            return 44100
        if opcode == 17:
            return 512
        return 0

    callback = callback_type(host)
    library = ctypes.WinDLL(str(module), use_last_error=True)
    entry = library.VSTPluginMain
    entry.argtypes = [callback_type]
    entry.restype = ctypes.c_void_p
    effect = entry(callback)
    result: dict[str, object] = {'module': str(module), 'entry_returned_nonnull': bool(effect),
                                 'host_callback_opcodes': sorted(set(calls))}
    if effect:
        result['magic'] = hex(ctypes.c_uint32.from_address(effect).value)
    return result


def loaded_arturia_modules() -> list[str]:
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32]
    psapi.EnumProcessModules.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                                       ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
    modules = (ctypes.c_void_p * 2048)()
    needed = ctypes.c_uint32()
    if not psapi.EnumProcessModules(kernel.GetCurrentProcess(), modules, ctypes.sizeof(modules), ctypes.byref(needed)):
        raise ctypes.WinError(ctypes.get_last_error())
    if needed.value > ctypes.sizeof(modules):
        raise ValueError('Process module inventory exceeded the bounded buffer')
    result = []
    for index in range(needed.value // ctypes.sizeof(ctypes.c_void_p)):
        name = ctypes.create_unicode_buffer(32768)
        if not kernel.GetModuleFileNameW(modules[index], name, len(name)):
            raise ctypes.WinError(ctypes.get_last_error())
        if 'arturia' in name.value.lower():
            result.append(name.value)
    return sorted(result)


def pump(seconds: float) -> None:
    message = wintypes.MSG()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        while time.monotonic() < end and ctypes.windll.user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
            ctypes.windll.user32.TranslateMessage(ctypes.byref(message))
            ctypes.windll.user32.DispatchMessageW(ctypes.byref(message))
        time.sleep(0.002)


def capture(plugin: Plugin) -> bytes:
    state = plugin.__getstate__()['plugin_state']
    if not isinstance(state, bytes):
        raise TypeError('Expected bytes from plugin serialization')
    return state


def worker(mode: str, module: Path, output: Path, source: Path | None, source_kind: str,
           touch_master: bool) -> None:
    attach_worker()
    isolate_desktop()
    print('attached_and_isolated', flush=True)
    if mode == 'loader':
        library = ctypes.WinDLL(str(module), use_last_error=True)
        result: object = {'windows_loader_succeeded': True, 'handle': library._handle}
    elif mode == 'factory':
        result = dataclasses.asdict(probe(module))
    elif mode == 'vst2entry':
        result = vst2_entry(module)
    elif mode == 'dawdreamer':
        daw = importlib.import_module('dawdreamer')
        factory = cast(Factory, daw.RenderEngine)
        engine = factory(44100, 512)
        print('before_plugin_creation', flush=True)
        plugin = engine.make_plugin_processor('analog_lab_probe', str(module))
        count = plugin.get_plugin_parameter_size()
        print('parameter_count', count, flush=True)
        engine.load_graph([(plugin, [])])
        engine.render(0.05)
        pump(0.15)
        baseline = capture(plugin)
        output.with_suffix('.baseline.state').write_bytes(baseline)
        source_payload: dict[str, object] = {}
        if source is not None:
            if baseline[:4] != b'CcnK' or baseline[8:12] != b'FBCh':
                raise ValueError('Only native FBCh templates are supported for source pushes')
            bare = source.read_bytes()
            source_payload = {
                'path': str(source), 'bytes': len(bare), 'sha256': hashlib.sha256(bare).hexdigest(),
                'experiment': source_kind + ' raw payload import using current native FBCh product ID',
                'target_product_id_hex': baseline[16:20].hex(),
                'payload_modified': False, 'host_byte_size_updated': True,
            }
            push = bytearray(baseline[:156] + struct.pack('>I', len(bare)) + bare)
            struct.pack_into('>I', push, 4, len(push) - 8)
            push_file = output.with_suffix('.push.fxb')
            push_file.write_bytes(push)
            plugin.load_state(str(push_file))
            pump(0.2)
            engine.render(0.05)
            pump(0.15)
            print('source_pushed', source, flush=True)
        touch_info: dict[str, object] = {}
        if touch_master:
            if source is None:
                raise ValueError('The knob-edit probe requires a source payload')
            original = plugin.get_parameter(0)
            touch_info['original_normalized'] = original
            touch_info['original_display'] = plugin.get_parameter_text(0)
            output.with_suffix('.pre_edit.state').write_bytes(capture(plugin))
            touch_info['edit_return'] = plugin.set_parameter(0, 0.5)
            pump(0.15)
            engine.render(0.02)
            touch_info['edited_display'] = plugin.get_parameter_text(0)
            output.with_suffix('.edited.state').write_bytes(capture(plugin))
            touch_info['restore_return'] = plugin.set_parameter(0, original)
            pump(0.15)
            engine.render(0.02)
            touch_info['restored_display'] = plugin.get_parameter_text(0)
        result = {'module': str(module), 'count': count,
                  'loaded_arturia_modules': loaded_arturia_modules(),
                  'parameters': [{'index': i, 'name': plugin.get_parameter_name(i),
                                  'normalized': plugin.get_parameter(i), 'display': plugin.get_parameter_text(i)}
                                 for i in range(count)]}
        if source_payload:
            result['source_payload'] = source_payload
        if touch_info:
            result['master_edit_and_restore'] = touch_info
        output.with_suffix('.inventory.json').write_text(json.dumps(result, indent=2))
        state = capture(plugin)
        output.with_suffix('.state').write_bytes(state)
        if source is not None:
            source_payload['raw_readback_equals_source'] = state[160:] == source.read_bytes()
        if state[:4] == b'CcnK' and state[8:12] == b'FBCh':
            output.with_suffix('.readback.bare').write_bytes(state[160:])
        plugin.add_midi_note(60, 100, 0.1, 0.5)
        engine.render(1.2)
        audio = engine.get_audio()
        np.save(output.with_suffix('.audio.npy'), audio)
        output.with_suffix('.audio.json').write_text(json.dumps({
            'midi_note': 60, 'velocity': 100, 'start_seconds': 0.1, 'duration_seconds': 0.5,
            'render_seconds': 1.2, 'sample_rate': 44100,
            'peak': float(np.max(np.abs(audio))),
            'rms': float(np.sqrt(np.mean(np.square(audio, dtype=np.float64)))),
        }, indent=2))
        print('saved', len(state), flush=True)
    else:
        raise ValueError(mode)
    output.write_text(json.dumps(result, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['loader', 'factory', 'vst2entry', 'dawdreamer'])
    parser.add_argument('--plugin', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--child', action='store_true')
    parser.add_argument('--source', type=Path)
    parser.add_argument('--source-kind', choices=['cross-generation', 'normalized-reload'], default='cross-generation')
    parser.add_argument('--touch-master', action='store_true')
    args = parser.parse_args()
    if args.child:
        worker(args.mode, args.plugin, args.out, args.source, args.source_kind, args.touch_master)
        return
    args.out.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(Path(__file__)), args.mode, '--plugin', str(args.plugin),
               '--out', str(args.out), '--child']
    if args.source is not None:
        command.extend(['--source', str(args.source)])
    command.extend(['--source-kind', args.source_kind])
    if args.touch_master:
        command.append('--touch-master')
    run_worker(command, args.out.parent, args.out.stem, 30)


if __name__ == '__main__':
    main()
