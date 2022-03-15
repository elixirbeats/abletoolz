
test_path = r'D:\PythonTest Project\tttg Project'
import colorama
colorama.init(autoreset=True)
from  colorama import Fore
from pathlib import Path

import sys
if sys.platform == "win32":
    ESC = ''
else:
    ESC = '\033'


C = Fore.LIGHTCYAN_EX
G = Fore.GREEN
R = Fore.RED
M = Fore.MAGENTA
Y = Fore.LIGHTYELLOW_EX
B = Fore.LIGHTBLUE_EX



print(r'[32mTESTTT')

CSI = '\033['
OSC = '\033]'
BEL = '\007'
print(f'{ESC} ]0;this is the window title {BEL}.')

for path in Path(test_path).rglob('*.cfg'):
    print(f'{Y} {path.name} in {B}{path.parent.resolve()}')
    with open(path, 'rb') as fd:
        byte_data = fd.read()
    try:
        decoded = byte_data.decode('utf-16', errors='ignore')
    except UnicodeDecodeError as exc:
        hexed = [f"x{hex(b)}" for b in byte_data]
        print(f'{R}Error decoding bytes, Exception [[{B}{exc}{R}]], ')
        # for chunk in byte_data[::32]:
        #     print(f'[[{chunk}]]')
            # print(f'data: [[{C}{byte_data}{R}]]')
    # for b in byte_data:
    #     if b != b'\x00' and b != 0:
    #         print(f'hex: {hex(b)}, byte: [{b}], chr({chr(b)})')
    null_stripped = decoded.replace('\x00', '')
    print(f'{M}{null_stripped}')
    print(f'Parsed {path.name}, last modified time {path.stat().st_mtime}')