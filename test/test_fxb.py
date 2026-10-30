"""The VST2 bank file, read and written.

Hermetic like the family tests: the one real thing here is the header of a bank
Live 12 saved, because its layout is the claim. The chunk behind it is a patch
and belongs to somebody, so it is zeros.
"""

from __future__ import annotations

import struct

import pytest

from abletoolz.formats import FormatError
from abletoolz.formats.fxb import FXB_HEADER_BYTES, FXP_HEADER_BYTES, FxbChunkBank, FxpChunkProgram

# The first 160 bytes of a Massive bank Live 12 saved from a VST2 device's
# header (2026-09-26), whose chunk is 8,576 bytes.
LIVE_MASSIVE_BANK_HEADER = (
    b"CcnK"
    + struct.pack(">I", 0x2218)
    + b"FBCh"
    + struct.pack(">I", 1)
    + b"NiMa"
    + struct.pack(">II", 1, 128)
    + bytes(128)
    + struct.pack(">I", 0x2180)
)

A_BANK = FxbChunkBank(fx_id=b"TEST", fx_version=3, program_count=1, chunk=b"the vst2 chunk")


def with_bytes(buffer: bytes, at: int, replacement: bytes) -> bytes:
    """``buffer`` with ``replacement`` written over it at ``at``."""
    return buffer[:at] + replacement + buffer[at + len(replacement) :]


def test_a_bank_is_written_the_way_live_writes_it() -> None:
    bank = FxbChunkBank(fx_id=b"NiMa", fx_version=1, program_count=128, chunk=bytes(0x2180))
    assert bank.encode()[:FXB_HEADER_BYTES] == LIVE_MASSIVE_BANK_HEADER


def test_a_live_bank_reads_back_its_fields() -> None:
    bank = FxbChunkBank.parse(LIVE_MASSIVE_BANK_HEADER + bytes(0x2180))
    assert (bank.fx_id_text, bank.fx_version, bank.program_count, bank.version) == ("NiMa", 1, 128, 1)
    # What the set survey found in VstPluginInfo/UniqueId for Massive.
    assert bank.unique_id == 1315523937
    assert len(bank.chunk) == 0x2180


@pytest.mark.parametrize("chunk", [b"", b"the vst2 chunk", bytes(range(256)) * 4])
def test_a_bank_round_trips(chunk: bytes) -> None:
    bank = FxbChunkBank(fx_id=b"TEST", fx_version=3, program_count=1, chunk=chunk)
    assert FxbChunkBank.parse(bank.encode()) == bank


def test_a_bank_whose_byte_count_is_zero_still_reads() -> None:
    """JUCE's save_state leaves the count at 0; the chunk size is what is read."""
    bank = FxbChunkBank.parse(with_bytes(A_BANK.encode(), 4, bytes(4)))
    assert bank == A_BANK


def test_bytes_after_the_chunk_are_not_the_bank_s() -> None:
    assert FxbChunkBank.parse(A_BANK.encode() + b"a trailer") == A_BANK


@pytest.mark.parametrize(
    "buffer",
    [
        b"",
        b"CcnK",
        # The program-list form holds parameters, not a chunk.
        with_bytes(A_BANK.encode(), 8, b"FxBk"),
        # A chunk size that runs past what is there.
        with_bytes(A_BANK.encode(), FXB_HEADER_BYTES - 4, struct.pack(">I", 4096)),
        # Not at the start: a VstW envelope has to be opened first.
        b"VstW" + struct.pack(">3I", 8, 1, 0) + A_BANK.encode(),
    ],
)
def test_a_buffer_that_is_not_a_chunk_bank_says_so(buffer: bytes) -> None:
    with pytest.raises(FormatError):
        FxbChunkBank.parse(buffer)


A_PROGRAM = FxpChunkProgram(b"XfsX", 1, 315, b" - Init - ", b"the plugin's own chunk")


def test_a_chunk_program_round_trips_and_keeps_its_name() -> None:
    encoded = A_PROGRAM.encode()
    assert len(encoded) == FXP_HEADER_BYTES + len(A_PROGRAM.chunk)
    assert FxpChunkProgram.parse(encoded) == A_PROGRAM


@pytest.mark.parametrize(
    ("buffer", "message"),
    [
        (A_PROGRAM.encode()[:20], "shorter than a program header"),
        (with_bytes(A_PROGRAM.encode(), 0, b"VstW"), "not a VST2 program"),
        (with_bytes(A_PROGRAM.encode(), 8, b"FxCk"), "holds parameters"),
        (A_PROGRAM.encode() + b"!", "declares a 22 byte chunk and holds 23"),
    ],
)
def test_a_buffer_that_is_not_one_chunk_program_is_refused(buffer: bytes, message: str) -> None:
    with pytest.raises(FormatError, match=message):
        FxpChunkProgram.parse(buffer)


def test_a_program_name_longer_than_its_field_is_refused() -> None:
    with pytest.raises(FormatError, match="28 bytes, not 29"):
        FxpChunkProgram(b"XfsX", 1, 0, b"n" * 29, b"").encode()
