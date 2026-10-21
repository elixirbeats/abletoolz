from __future__ import annotations

import hashlib
import json
import shutil
import struct
from pathlib import Path

ARCHIVE = Path('N:/Electronics, Computers/Desktop 2070/VSTs')
OUT = Path(__file__).parent


def inspect(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    pe = struct.unpack_from('<I', raw, 60)[0]
    machine = struct.unpack_from('<H', raw, pe + 4)[0]
    return {'source': str(path), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
            'machine': hex(machine), 'architecture': {0x14c: 'x86', 0x8664: 'x64'}.get(machine, 'unknown')}


def main() -> None:
    names = ['FabFilter Micro.dll', 'FabFilter Pro-Q 3.dll', 'FabFilter Volcano 2.dll',
             'Analog Lab 4.dll', 'Mini V2.dll', 'CamelCrusher.dll', 'ValhallaRoom_x64.dll']
    paths = [ARCHIVE / name for name in names]
    paths.extend([ARCHIVE / '32/FabFilter Pro-Q.64.dll', ARCHIVE / '32/Mini V2.64.dll',
                  ARCHIVE / '32/FabFilter/FabFilter Saturn x64.dll',
                  ARCHIVE / '32/FabFilter/FabFilter Pro-Q 2 x64.dll'])
    records = []
    for path in paths:
        record = inspect(path)
        local = OUT / 'plugins' / path.relative_to(ARCHIVE)
        local.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, local)
        record['local'] = str(local)
        config = path.with_suffix('.txt')
        if config.exists():
            shutil.copy2(config, local.with_suffix('.txt'))
            record['wrapper_config'] = config.read_text()
        records.append(record)
        print(path.name, record['architecture'], record['bytes'], flush=True)
    (OUT / 'inventory.json').write_text(json.dumps(records, indent=2))
    wrappers = [{'path': str(p), 'config': p.read_text()} for p in (ARCHIVE / '32').glob('*.64.txt')]
    (OUT / 'wrapper_configs.json').write_text(json.dumps(wrappers, indent=2))


if __name__ == '__main__':
    main()
