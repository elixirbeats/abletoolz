from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path


def repoint(wrapper: Path, plugin: Path, output: Path) -> Path:
    config = wrapper.with_suffix('.txt')
    text = config.read_text()
    pieces = text.split('"', 2)
    if len(pieces) != 3 or pieces[0].strip():
        raise ValueError('Expected a quoted plugin path at the start of the wrapper config')
    if wrapper.name == plugin.name:
        raise ValueError('Wrapper and original plugin must have different filenames')
    output.mkdir(parents=True, exist_ok=False)
    local_plugin = output / plugin.name
    local_wrapper = output / wrapper.name
    shutil.copy2(plugin, local_plugin)
    shutil.copy2(wrapper, local_wrapper)
    shutil.copy2(config, output / (wrapper.stem + '.original.txt'))
    local_wrapper.with_suffix('.txt').write_text('"' + str(local_plugin.resolve()) + '"' + pieces[2])
    records = [{'source': str(source.resolve()), 'copy': str(destination.resolve()),
                'sha256': hashlib.sha256(destination.read_bytes()).hexdigest()}
               for source, destination in [(wrapper, local_wrapper), (plugin, local_plugin)]]
    (output / 'provenance.json').write_text(json.dumps(records, indent=2))
    return local_wrapper


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Copy a jBridge stub and original DLL, then repair only the copied link.'
    )
    parser.add_argument('--wrapper', required=True, type=Path)
    parser.add_argument('--plugin', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    print(repoint(args.wrapper, args.plugin, args.out))


if __name__ == '__main__':
    main()
