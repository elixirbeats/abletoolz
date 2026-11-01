"""Wolfram: its VST3 class id as moduleinfo prints it, and its VST2 chunk."""

from __future__ import annotations

import struct

import pytest

from abletoolz.plugins.identity.ids import PluginKind, Vst2Id, Vst3Id
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.plugin_map import FormatState


def test_class_ids_read_as_moduleinfo_prints_them() -> None:
    assert Vst3Id.of("11111111-92464F53-93FBE413-1C945A19").uid_fields == (  # Wolfram's own VST3
        286331153,
        -1840885933,
        -1812208621,
        479484441,
    )


def test_a_wolfram_chunk_as_live_saved_it_reads_as_its_processor_state() -> None:
    """The library's Wolfram VST2 chunks: seven u32, the size less 16, three zeros, the state's slot flag and sizes."""
    slot_b, slot_a = b"\x01\xfa\0\0slot B", b"\x01\xfa\0\0slot A preset"
    body = slot_b + slot_a
    chunk = struct.pack("<7I", 28 + len(body) - 16, 0, 0, 0, 1, len(slot_b), len(slot_a)) + body
    wolfram = CATALOG.find(Vst2Id(0x576F6C66))
    assert wolfram is not None
    assert wolfram.lift(FormatState(chunk), PluginKind.VST) == chunk[16:]
    for broken in (chunk[:20], chunk[:-1], b"\1" + chunk[1:]):
        with pytest.raises(StateTransformError):
            wolfram.lift(FormatState(broken), PluginKind.VST)
