"""Texture's user sample path, in the bare JUCE form and inside the VST3's VstW bank.

Reading it, relinking it and rewriting it back are in ``test_file_paths``; this is what a rewrite has
to recompute around it.
"""

from __future__ import annotations

import pathlib
import struct
from collections.abc import Callable

import pytest
from plugin_patches import TEXTURE, TEXTURE_SNARE, file_paths, texture_bare, texture_wrapped

from abletoolz.formats.fxb import FxbChunkBank
from abletoolz.plugins.state.plugin_map import FileRef


@pytest.mark.parametrize("form", [texture_bare, texture_wrapped], ids=["bare", "VstW bank"])
def test_a_texture_rewrite_recomputes_every_count(form: Callable[[str | None], bytes]) -> None:
    moved = pathlib.Path("D:\\A Much Longer Folder\\Moved Snare.wav")
    written = file_paths(TEXTURE).rewrite(form(TEXTURE_SNARE), FileRef(TEXTURE_SNARE), moved)

    assert written.count(b"VC2!") == 1
    start = written.index(b"VC2!")
    size = struct.unpack_from("<I", written, start + 4)[0]
    assert written[start + 8 + size : start + 8 + size + 1] == b"\x00"
    if form is texture_wrapped:
        bank = FxbChunkBank.parse(written[16:])
        assert written[16 : 16 + len(bank.encode())] == bank.encode()
