"""The VST2 bank file: a plugin's opaque chunk behind a 160 byte header.

An ``.fxb`` is what a VST2 host writes when asked to save a bank, and for a
plugin that exposes a chunk it is a small wrapper and nothing more: magic, a
byte count, the form tag ``FBCh``, a version, the plugin's fxID and fxVersion, a
program count, 128 reserved bytes, the chunk size, the chunk. Big-endian
throughout, by the VST2 spec.

Measured 2026-09-26 on a Massive bank saved from Live 12's device header:
version 1, fxID ``NiMa``, fxVersion 1, 128 programs, and a chunk that is byte
for byte the ``Buffer`` an ``.als`` stores for the same device. So for a chunk
plugin the file and the set differ by this header and nothing else, and a bank
is how a patch leaves a set, or enters one, through Live's own
"Load Preset or Bank".

The other form, ``FxBk``, holds a list of programs rather than a chunk and is
refused here rather than misread. What Live stores for a plugin that has no
chunk is not a bank file at all; see :mod:`abletoolz.plugins.state.fxbk`.
"""

from __future__ import annotations

import dataclasses
import struct

from abletoolz.formats.errors import FormatError

FXB_MAGIC = b"CcnK"
FXB_CHUNK_BANK = b"FBCh"
FXB_HEADER_BYTES = 160

_HEADER = struct.Struct(">4sI4sI4sII128xI")
# The byte count covers everything after itself: the rest of the header and the chunk.
_COUNTED_HEADER_BYTES = FXB_HEADER_BYTES - 8


@dataclasses.dataclass(frozen=True, slots=True)
class FxbChunkBank:
    """A VST2 bank of the chunk form.

    The byte count after the magic is not kept. Live writes the true count and
    JUCE's ``save_state`` writes 0, so it says nothing about the chunk; the
    chunk size does, and :meth:`encode` writes the count Live writes.
    """

    fx_id: bytes
    fx_version: int
    program_count: int
    chunk: bytes
    version: int = 1

    @classmethod
    def parse(cls, buffer: bytes) -> FxbChunkBank:
        """The bank at the start of ``buffer``.

        A chunk size that runs past the buffer is refused. Bytes after the chunk
        are not the bank's and are left to the caller, which is what lets a
        ``VstW`` state carry a trailer.
        """
        if len(buffer) < FXB_HEADER_BYTES:
            raise FormatError(f"{len(buffer)} bytes is shorter than a bank header")
        magic, _, form, version, fx_id, fx_version, programs, size = _HEADER.unpack_from(buffer)
        if magic != FXB_MAGIC:
            raise FormatError(f"not a VST2 bank: it opens with {magic!r}")
        if form != FXB_CHUNK_BANK:
            raise FormatError(f"a {form!r} bank holds programs, not a chunk")
        if FXB_HEADER_BYTES + size > len(buffer):
            raise FormatError(f"the bank declares a {size} byte chunk and holds {len(buffer) - FXB_HEADER_BYTES}")
        return cls(
            fx_id=fx_id,
            fx_version=fx_version,
            program_count=programs,
            chunk=buffer[FXB_HEADER_BYTES : FXB_HEADER_BYTES + size],
            version=version,
        )

    @property
    def fx_id_text(self) -> str:
        """The fxID as the four characters vendors choose it as (``NiMa``)."""
        return self.fx_id.decode("latin1")

    @property
    def unique_id(self) -> int:
        """The fxID as the integer an ``.als`` stores in ``VstPluginInfo/UniqueId``."""
        return int.from_bytes(self.fx_id, "big", signed=True)

    def encode(self) -> bytes:
        """The bank file, laid out as Live writes it."""
        return (
            _HEADER.pack(
                FXB_MAGIC,
                _COUNTED_HEADER_BYTES + len(self.chunk),
                FXB_CHUNK_BANK,
                self.version,
                self.fx_id,
                self.fx_version,
                self.program_count,
                len(self.chunk),
            )
            + self.chunk
        )
