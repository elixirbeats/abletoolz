"""The hex text Live writes for binary data inside its XML.

A plugin's ``Buffer``, a VST3 ``ProcessorState``/``ControllerState`` and the
pre-11 path ``Data`` are all bytes, and Live stores them the same way: upper
case hex, wrapped so each line is 80 characters of hex behind the element's
indentation, opening on a new line and closing on the parent's indentation::

    <Buffer>
    \t\t\t\t0A1B2C...
    \t\t\t\t...
    \t\t\t</Buffer>

The depth -- how many tabs the hex lines carry -- is kept separate from the
bytes, because rewriting a blob in place means writing it back at the depth it
was read from, and that is the only thing about the old text worth keeping.
"""

from __future__ import annotations

import textwrap

from abletoolz.formats.errors import FormatError

# Hex characters per line, not counting the indentation in front of them.
LINE_WIDTH = 80


def decode(text: str | None) -> bytes:
    """The bytes a hex text holds; nothing at all for an empty element."""
    if text is None:
        return b""
    compact = "".join(text.split())
    try:
        return bytes.fromhex(compact)
    except ValueError as error:
        raise FormatError(f"not hex text: {compact[:32]!r}") from error


def depth(text: str) -> int:
    """Read the first hex line's indentation, including inline generated data."""
    decode(text)
    for line in text.splitlines():
        if line.strip():
            indentation = line[: len(line) - len(line.lstrip())]
            return indentation.count("\t")
    return 0


def encode(payload: bytes, depth: int) -> str:
    """``payload`` as Live writes it at ``depth`` tabs."""
    indent = "\t" * depth
    lines = textwrap.wrap(
        payload.hex().upper(),
        initial_indent=indent,
        subsequent_indent=indent,
        break_long_words=True,
        tabsize=4,
        width=LINE_WIDTH + depth,
    )
    return "\n" + "\n".join(lines) + "\n" + "\t" * (depth - 1)
