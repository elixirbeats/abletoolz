from __future__ import annotations

import ctypes
import hashlib
import importlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Protocol, cast, override

OUT = Path(__file__).parent


class Plugin(Protocol):
    def get_plugin_parameter_size(self) -> int: ...
    def get_parameter_name(self, index: int) -> str: ...
    def get_parameter(self, index: int) -> float: ...
    def get_parameter_text(self, index: int) -> str: ...
    @override
    def __getstate__(self) -> dict[str, object]: ...


class Engine(Protocol):
    def make_plugin_processor(self, name: str, path: str) -> Plugin: ...


class Factory(Protocol):
    def __call__(self, sample_rate: int, block: int) -> Engine: ...


def isolate_desktop() -> None:
    user = ctypes.windll.user32
    user.CreateDesktopW.restype = ctypes.c_void_p
    user.CreateDesktopW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
    user.SetThreadDesktop.argtypes = [ctypes.c_void_p]
    desktop = user.CreateDesktopW('AbletoolzPluginProbe', None, None, 0, 0x01FF, None)
    if not desktop or not user.SetThreadDesktop(desktop):
        raise OSError('Cannot isolate plugin UI on a hidden desktop')
    ctypes.windll.kernel32.SetErrorMode(0x8003)


def child(path: Path, result: Path) -> None:
    isolate_desktop()
    daw = importlib.import_module('dawdreamer')
    factory = cast(Factory, daw.RenderEngine)
    engine = factory(44100, 512)
    print('before_load', flush=True)
    plugin = engine.make_plugin_processor('archive_probe', str(path))
    print('processor_returned', flush=True)
    count = plugin.get_plugin_parameter_size()
    print('parameter_count', count, flush=True)
    if count == 0:
        raise ValueError('Plugin returned no parameters; load success is unproven')
    inventory = [{'index': i, 'name': plugin.get_parameter_name(i),
                  'normalized': plugin.get_parameter(i), 'display': plugin.get_parameter_text(i)}
                 for i in range(count)]
    raw = plugin.__getstate__()['plugin_state']
    if not isinstance(raw, bytes):
        raise TypeError('Plugin state is not bytes')
    result.with_suffix('.state').write_bytes(raw)
    result.write_text(json.dumps({'loaded': True, 'count': count, 'parameters': inventory,
                                 'state_bytes': len(raw), 'state_sha256': hashlib.sha256(raw).hexdigest(),
                                 'host_format': raw[8:12].decode('ascii', errors='backslashreplace'),
                                 'serialized_chunk_bytes': len(raw) - 160 if raw[8:12] == b'FBCh' else None,
                                 'state_prefix_hex': raw[:180].hex()}, indent=2))
    print('saved', len(raw), flush=True)


def main() -> None:
    if len(sys.argv) == 4 and sys.argv[1] == '--child':
        child(Path(sys.argv[2]), Path(sys.argv[3]))
        return
    records: list[dict[str, object]] = []
    inventory = json.loads((OUT / 'inventory.json').read_text())
    for item in inventory:
        path = Path(item['local'])
        name = path.stem
        result = OUT / 'loads' / (name + '.json')
        result.parent.mkdir(exist_ok=True)
        start = time.monotonic()
        command = [sys.executable, str(Path(__file__)), '--child', str(path), str(result)]
        try:
            run = subprocess.run(command, capture_output=True, text=True, timeout=20,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            record: dict[str, object] = {'name': name, 'path': str(path), 'returncode': run.returncode,
                                        'stdout': run.stdout, 'stderr': run.stderr, 'timeout': False}
        except subprocess.TimeoutExpired as error:
            record = {'name': name, 'path': str(path), 'timeout': True,
                      'stdout': str(error.stdout), 'stderr': str(error.stderr)}
        record['seconds'] = round(time.monotonic() - start, 3)
        record['result_exists'] = result.exists()
        record['successful'] = record.get('returncode') == 0 and result.exists()
        if record['successful']:
            record['result'] = json.loads(result.read_text())
        records.append(record)
        (OUT / 'load_results.json').write_text(json.dumps(records, indent=2))
        print(name, record.get('returncode'), record['timeout'], record['result_exists'], flush=True)


if __name__ == '__main__':
    main()
