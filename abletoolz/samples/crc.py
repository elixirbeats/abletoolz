"""The checksum Live stores beside every file it references (``OriginalCrc``).

Pre-11 sets store it as ``Crc``, with ``MaxCrcSize`` 16384 beside it. It is the
same checksum in every version from Live 9 to Live 12.

It is CRC-16/UMTS (also called CRC-16/BUYPASS): polynomial 0x8005, MSB first,
init 0, no reflection, no final xor. It covers the first ``MaxCrcSize`` bytes of
the file, or the whole file when it is shorter: raw bytes, not decoded audio,
which is why ``.amxd`` references carry one too.

Live's exe holds the matching lookup table, as ``uint32[256]``. Every
measurement below counts distinct files, each one found on disk at the exact
size the set recorded, and only sets Live saved: copies abletoolz has
written are left out.

| Live | Schema | Sets | Files | Match |
| --- | --- | ---: | ---: | ---: |
| 11, 12 | | | 163 | 163 |
| 10.1 | 10.0_377 | 70 | 559 | 559 |
| 10.0 | 10.0_370 | 61 | 409 | 409 |
| 9.0.1 to 9.6 | 9.0_305, 9.5_326 | 18 | 137 | 137 |

Live 11 and 12 were measured 2026-10-23 (wav, aif, amxd; 14,984 bytes to several
MB), plus a 48-byte wav. Live 9 and 10 were measured 2026-10-23 (wav, aif, mp3; 458
bytes to 147 MB, including files under 16384 bytes). The pre-11 files were found by
name and size in the sample database, since those sets point at drives that have
moved. Six of Ableton's Samplification factory-pack sets (Live 9.2) miss on all
179 of their files. Each of those name-and-size matches is a copy under a
library folder rather than the file the pack shipped, so they are read as
different files that share a name and size, not as a different checksum. flac
and ogg are not in any of these samples.
"""

import pathlib

from fastcrc.crc16 import umts

MAX_CRC_SIZE = 16384
"""Bytes of the file the checksum covers, the value pre-11 sets write as ``MaxCrcSize``."""


def file_crc(path: pathlib.Path) -> int:
    """The ``OriginalCrc`` Live would store for the file at ``path``."""
    with path.open("rb") as f:
        return umts(f.read(MAX_CRC_SIZE))
