"""Names an older Windows plugin saved in the code page of whichever machine saved them."""

from __future__ import annotations


def decode_ansi(raw: bytes) -> str:
    """``raw`` as the text it most likely was, for display; the bytes do not say which code page wrote them.

    UTF-8 when it is valid, Shift-JIS when that reads as Japanese kana, Windows-1252, Shift-JIS again (kanji
    alone), and Latin-1 last, which reads any byte.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        japanese = raw.decode("cp932")
    except UnicodeDecodeError:
        japanese = None
    if japanese is not None and any("぀" <= character <= "ヿ" for character in japanese):
        return japanese
    try:
        return raw.decode("cp1252")
    except UnicodeDecodeError:
        return japanese if japanese is not None else raw.decode("latin-1")
