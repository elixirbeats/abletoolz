"""FastLZ, the byte-oriented LZ77 compression Native Instruments' plugins save their patches with.

A stream is a run of tokens. A control byte below 32 is a literal run of that
many bytes plus one, copied as they are; above it, a back-reference: the top
three bits are the match length less two (seven meaning a length byte
follows), and the low five bits with the next byte are the distance back, less
one. The top three bits of the very first byte, always a literal run, name the
level: 0 for level 1, 1 for level 2, which differs only in coding long lengths
and distances.

Measured 2026-10-23 over the NI states in the user's sets: Battery 4 writes
level 2 and an unlocked Kontakt 5 library writes level 1, and every stream
decodes to exactly the size its container states. :func:`compress` writes
level 1, which every FastLZ decoder reads.
"""

from __future__ import annotations

from abletoolz.formats.errors import FormatError

_MAX_LITERALS = 32
_MAX_DISTANCE = 8191
_MAX_MATCH = 264
_LONG_DISTANCE = 31 << 8
_FAR_BIAS = 8191


def decompress(src: bytes, size: int) -> bytes:
    """The ``size`` bytes the FastLZ stream ``src`` holds; a stream that decodes to any other length is refused."""
    if not src:
        raise FormatError("An empty FastLZ stream holds nothing")
    level = (src[0] >> 5) + 1
    if level not in (1, 2):
        raise FormatError(f"FastLZ level {level} does not exist")
    out = bytearray()
    ip = 1
    ctrl = src[0] & 31
    try:
        while True:
            if ctrl >= 32:
                length = (ctrl >> 5) - 1
                offset = (ctrl & 31) << 8
                ref = len(out) - offset - 1
                if level == 1:
                    if length == 6:
                        length += src[ip]
                        ip += 1
                    ref -= src[ip]
                    ip += 1
                else:
                    if length == 6:
                        while True:
                            code = src[ip]
                            ip += 1
                            length += code
                            if code != 255:
                                break
                    code = src[ip]
                    ip += 1
                    ref -= code
                    if code == 255 and offset == _LONG_DISTANCE:
                        offset = (src[ip] << 8) | src[ip + 1]
                        ip += 2
                        ref = len(out) - offset - _FAR_BIAS - 1
                length += 3
                if ref < 0:
                    raise FormatError(f"FastLZ back-reference before the start of the output at byte {ip}")
                for k in range(length):
                    out.append(out[ref + k])
            else:
                ctrl += 1
                if ip + ctrl > len(src):
                    raise FormatError("FastLZ literal run past the end of the stream")
                out += src[ip : ip + ctrl]
                ip += ctrl
            if len(out) > size:
                raise FormatError(f"FastLZ stream decodes past its stated {size} bytes")
            if ip >= len(src):
                break
            ctrl = src[ip]
            ip += 1
    except IndexError as error:
        raise FormatError("FastLZ stream ends inside a token") from error
    if len(out) != size:
        raise FormatError(f"FastLZ stream decodes to {len(out)} bytes, not the stated {size}")
    return bytes(out)


def compress(data: bytes) -> bytes:
    """``data`` as a level 1 FastLZ stream: greedy three-byte matches within 8 KiB."""
    if not data:
        raise FormatError("FastLZ cannot hold zero bytes: a stream opens with a literal")
    out = bytearray()
    literals = bytearray()
    last: dict[bytes, int] = {}
    n = len(data)
    i = 0

    def flush() -> None:
        while literals:
            run = literals[:_MAX_LITERALS]
            out.append(len(run) - 1)
            out.extend(run)
            del literals[:_MAX_LITERALS]

    while i < n:
        if i + 3 <= n:
            key = data[i : i + 3]
            ref = last.get(key)
            last[key] = i
            # The first token must be a literal run: it carries the level.
            if ref is not None and i - ref - 1 <= _MAX_DISTANCE and (out or literals):
                length = 3
                limit = min(_MAX_MATCH, n - i)
                while length < limit and data[ref + length] == data[i + length]:
                    length += 1
                flush()
                distance = i - ref - 1
                if length <= 8:
                    out.append(((length - 2) << 5) | (distance >> 8))
                else:
                    out.append((7 << 5) | (distance >> 8))
                    out.append(length - 9)
                out.append(distance & 0xFF)
                for j in range(i + 1, min(i + length, n - 2)):
                    last[data[j : j + 3]] = j
                i += length
                continue
        literals.append(data[i])
        if len(literals) == _MAX_LITERALS:
            flush()
        i += 1
    flush()
    return bytes(out)
