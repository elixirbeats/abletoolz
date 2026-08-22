"""The bank a VST2 host stores for a plugin that exposes no chunk of its own.

Nothing about this container is per vendor. The VST2 spec gives a plugin two
ways to be saved: hand the host an opaque chunk, or let the host write every
parameter down. The second is an FXB bank -- a 28 byte NUL-padded program name
and one float32 per parameter, each normalized 0 to 1 -- and it is what an
``.als`` holds for every plugin that never implemented the first, FabFilter's
older products among many others.

Two constraints the layout carries:

* **The units are the host's.** 0.5 is the middle of whatever range the plugin
  declares and only the plugin knows what that is, which is the whole reason a
  re-encode off one of these needs a measured transfer curve (see
  :mod:`abletoolz.plugin_parsers.state.derived`).
* **The byte order is Ableton's.** FXB is big-endian by the VST2 spec; Live
  writes the same program payload with the floats flipped to little-endian, so
  what an ``.als`` Buffer holds is read little-endian here. A host handed these
  bytes wants the FXB framing and the flip put back, which is Ableton's
  omission rather than the host's addition.
"""

from __future__ import annotations

import dataclasses
import struct

from abletoolz.plugin_parsers.state import StateTransformError

# Live pads the preset name to a fixed field and the parameters follow it.
LEGACY_NAME_BYTES = 28

# Every chunk magic a product measured so far is known to open with -- both of
# them FabFilter's, because those are the products this library re-encodes. A
# bank has none: it starts in the middle of a preset name.
_CHUNK_MAGICS = (b"FFBS", b"FabF")


@dataclasses.dataclass(frozen=True, slots=True)
class LegacyBank:
    """What an ``.als`` stores for a plugin that exposes no chunk of its own.

    Live's stored-parameter bank: the preset name in a fixed 28 byte field, then
    one float32 per parameter, each normalized 0 to 1 the way a VST2 host sees
    it. Nothing here is in the plugin's units -- 0.5 is the middle of whatever
    range the plugin declares, and only the plugin knows what that is.
    """

    preset_name: str
    parameters: tuple[float, ...]

    @classmethod
    def parse(cls, payload: bytes) -> LegacyBank:
        """Read a bank, refusing anything carrying a chunk magic instead.

        The refusal matters more than the read. A newer FabFilter's chunk would
        decode as a bank of plausible-looking floats -- its magic and version
        read as four characters of preset name -- and the result would be a
        patch nobody chose rather than an error.
        """
        for magic in _CHUNK_MAGICS:
            if payload.startswith(magic):
                raise StateTransformError(
                    f"A {magic.decode('ascii')} chunk is not a Live stored-parameter bank. "
                    "The product that wrote this exposes its own state and needs no re-encoding."
                )
        remainder = len(payload) - LEGACY_NAME_BYTES
        if remainder < 0 or remainder % 4:
            raise StateTransformError(
                f"{len(payload)} bytes is not a stored-parameter bank: a {LEGACY_NAME_BYTES} byte "
                "name field and a whole number of float32 do not account for it."
            )
        count = remainder // 4
        return cls(
            preset_name=payload[:LEGACY_NAME_BYTES].split(b"\x00", 1)[0].decode("latin1"),
            parameters=struct.unpack_from(f"<{count}f", payload, LEGACY_NAME_BYTES),
        )

    def encode(self) -> bytes:
        """The bytes an ``.als`` holds for this bank."""
        name = self.preset_name.encode("latin1").ljust(LEGACY_NAME_BYTES, b"\x00")
        return name + struct.pack(f"<{len(self.parameters)}f", *self.parameters)
