"""What a state buffer is, read off its own bytes, and the moves families share.

**Name a buffer.** :func:`detect` classifies a blob into the container families
the survey measured over 23,786 device instances in 811 sets. It asks the bytes
and never the plugin name, which is the rule a vendor forced: the corpus holds
Kilohearts states from before that vendor moved to a zip container, as flat
binary, under the same name as the zip ones.

**Reframe a buffer.** The 2026-08-15 readback survey found the same handful of
shapes across unrelated vendors -- wrap in a header, strip a trailer, prefix a
length (Diva's VST3 does that to the preset text its VST2 stores bare) -- so the
operations here are per *family*, not per vendor. Each is measured on named
products; a shape nobody has seen twice is not here.

The unwrap directions answer ``None`` rather than raising, because a primitive
is asked two questions at once -- is this that shape, and if so what is inside
it -- and the second only means something when the first is yes. What to say
about a no belongs to the caller.

The JUCE shapes go both ways. The trailer a JUCE VST3 appends and the
``VstW`` envelope its VST2-compatible builds wrap the chunk in were read on
2026-10-22 by a headless host off four installed plugins in all three formats,
and every byte of both is accounted for. See :func:`juce_private_data_add` and
:func:`vstw_wrap`.
"""

from __future__ import annotations

import dataclasses
import enum
import struct

from abletoolz.formats import FormatError
from abletoolz.formats.fxb import FXB_MAGIC, FxbChunkBank
from abletoolz.plugins.state.errors import StateTransformError

# Little-endian words everywhere below. Ableton writes little-endian and so does
# every framing measured here; the one big-endian thing is the FXB bank, on its
# own or inside a VstW envelope, which is big-endian by the VST2 spec.
_U32 = struct.Struct("<I")


# -- what a buffer is -------------------------------------------------------


class Family(enum.StrEnum):
    """The container families measured across the corpus, cheapest to read first.

    A family says how far a buffer can be read without the plugin, which is what
    decides whether a conversion is a header job or a re-encode. The largest is
    :data:`OPAQUE` at 37% of devices measured, and the text-like ones plus
    FabFilter's decoded formats are about half.
    """

    EMPTY = "empty"
    ZIP = "zip archive"
    GZIP = "gzip"
    ZLIB = "zlib"
    VSTW = "VstW wrapper"
    FXB = "VST2 bank"
    JUCE = "JUCE VC2!"
    FABFILTER = "FabFilter framed"
    XML = "XML"
    JSON = "JSON"
    TEXT = "plain text"
    MOSTLY_TEXT = "mostly-text framed"
    ZERO_PREFIXED = "zero-prefixed"
    OPAQUE = "opaque binary"


ZIP_MAGIC = b"PK\x03\x04"
GZIP_MAGIC = b"\x1f\x8b"

# A zlib stream opens with a compression-method byte and a check byte, and the
# three second bytes below are the ones real buffers carry: no compression
# preset, default, and best. Deflate with no zlib header is not detectable at
# all, which is why Serum's raw stream is not a family here.
ZLIB_FIRST = 0x78
ZLIB_SECOND = frozenset({0x01, 0x9C, 0xDA})

VSTW_MAGIC = b"VstW"

# JUCE's AudioProcessor::copyXmlToBinary: magic 0x21324356 little-endian, a
# uint32 length, then the XML. Measured on soothe2, Rift and ValhallaShimmer.
JUCE_MAGIC = b"VC2!"

# The tags a FabFilter state opens with, both generations. The survey also counted
# a leading "Default " -- that is a Live bank's preset name rather than a
# vendor tag, so it is left to fxbk.LegacyBank, which reads the whole name field
# instead of guessing from eight bytes of it.
FABFILTER_TAGS = (b"FFBS", b"FFed", b"FabF")

# Xfer's JSON opens with its own keyword rather than a brace. Serum 2 writes it
# at offset 0 in all 474 distinct buffers the corpus holds for it: a JSON
# header and then a zstd body, read by xfer.XferJson.
JSON_KEYWORD = b"XferJson"

# How many bytes of leading whitespace a text format may have before its first
# real character. Text buffers in the corpus have none; the window is what keeps
# the check off the whole blob, which can be 21 MB.
_LEADING_WINDOW = 64

# What the survey rig counted as printable, tabs and newlines included.
_PRINTABLE = bytes(byte for byte in range(256) if 0x20 <= byte < 0x7F or byte in (0x09, 0x0A, 0x0D))

# Where a framed text format stops looking like a binary one. The mostly-text
# family is a short binary header in front of the vendor's own
# preset text, and three quarters printable is where the corpus separates.
MOSTLY_TEXT_FRACTION = 0.75


def printable_fraction(buffer: bytes) -> float:
    """How much of ``buffer`` is text, by the survey's own definition.

    Zero for an empty buffer: nothing is not text.
    """
    if not buffer:
        return 0.0
    return 1.0 - len(buffer.translate(None, delete=_PRINTABLE)) / len(buffer)


def detect(buffer: bytes) -> Family:
    """Which family ``buffer`` belongs to, by its bytes alone.

    The order is the answer as much as the rules are. A compressed body is
    tested before anything textual, because a zip's local header is not text and
    its content might be; a vendor tag is tested before the text rules, because
    ``FFBS`` and ``abnk`` are printable; and the fully-printable test comes
    before the mostly-printable one, since every plain text buffer would pass
    both. Empty is first for the same reason the other way round -- no bytes are
    vacuously all printable, and an empty buffer is a real answer in the corpus
    (7.6% of devices: a default patch stores nothing).
    """
    if not buffer:
        return Family.EMPTY
    if buffer.startswith(ZIP_MAGIC):
        return Family.ZIP
    if buffer.startswith(GZIP_MAGIC):
        return Family.GZIP
    if len(buffer) >= 2 and buffer[0] == ZLIB_FIRST and buffer[1] in ZLIB_SECOND:
        return Family.ZLIB
    if buffer.startswith(VSTW_MAGIC):
        return Family.VSTW
    if buffer.startswith(FXB_MAGIC):
        return Family.FXB
    if buffer.startswith(JUCE_MAGIC):
        return Family.JUCE
    if buffer.startswith(FABFILTER_TAGS):
        return Family.FABFILTER
    leading = buffer[:_LEADING_WINDOW].lstrip()
    if leading.startswith(b"<"):
        return Family.XML
    if leading.startswith((b"{", b"[", JSON_KEYWORD)):
        return Family.JSON
    fraction = printable_fraction(buffer)
    if fraction == 1.0:
        return Family.TEXT
    if fraction >= MOSTLY_TEXT_FRACTION:
        return Family.MOSTLY_TEXT
    if buffer.startswith(bytes(_U32.size)):
        return Family.ZERO_PREFIXED
    return Family.OPAQUE


# -- the reframes the families share ----------------------------------------

# Kilohearts' VST3 header: two little-endian words, the first of which is 1 in
# every instance measured. Read off three kHs VST3 devices in a Live 12 set and
# reproduced by the survey straight out of the plugin.
KILOHEARTS_HEADER = struct.Struct("<II")
KILOHEARTS_VERSION = 1


def kilohearts_wrap(payload: bytes) -> bytes:
    """Wrap a kHs VST2 payload the way its VST3 expects it.

    The zip the VST2 stores raw is preceded by ``(1, payload length)``.
    Sound-validated on kHs Distortion and kHs Filter, and what every
    Kilohearts plugin map's VST3 framing does.
    """
    return KILOHEARTS_HEADER.pack(KILOHEARTS_VERSION, len(payload)) + payload


def kilohearts_unwrap(state: bytes) -> bytes | None:
    """The payload inside a kHs VST3 state, or None if the two words do not fit it.

    Both words are checked. A blob whose first word happens to be 1 is not this
    shape unless the second accounts for everything after the header.
    """
    if len(state) < KILOHEARTS_HEADER.size:
        return None
    version, length = KILOHEARTS_HEADER.unpack_from(state)
    if version != KILOHEARTS_VERSION or length != len(state) - KILOHEARTS_HEADER.size:
        return None
    return state[KILOHEARTS_HEADER.size :]


def izotope_unwrap(chunk: bytes) -> bytes | None:
    """The VST3 processor state inside an iZotope VST2 chunk, or None.

    Measured 2026-08-13 on Trash 2 and Ozone 9 devices a set stores both ways:
    the chunk is a little-endian uint32 length, that many bytes -- exactly what
    Ableton writes as ``ProcessorState`` -- and then the preset name as a
    length-prefixed string. Both lengths have to agree before this is believed,
    because a blob that merely opens with a plausible number is not this shape.
    """
    if len(chunk) >= 12:
        (size,) = _U32.unpack_from(chunk)
        if 0 < size <= len(chunk) - 8:
            trailer = chunk[4 + size :]
            (name_length,) = _U32.unpack_from(trailer)
            if name_length == len(trailer) - 4:
                return chunk[4 : 4 + size]
    return None


# A VstW envelope is a 16-byte header -- the magic and three big-endian words,
# reading 8, 1 and 0 in every buffer measured -- and then a whole FXB bank.
VSTW_HEADER_BYTES = 16


def vstw_chunk(buffer: bytes) -> bytes | None:
    """The VST2 chunk inside a ``VstW`` envelope, or None if it is not one.

    Valhalla, Eventide and Soundtoys VST3 devices carry their VST2 chunk here
    unchanged, which the survey found at byte 176 of the state: this envelope's
    16 bytes, the FXB header the spec fixes at 156 bytes, and a size word. What
    the bank declares is read rather than assumed, so a state carrying something
    after the chunk -- a JUCE trailer, say -- gives up the chunk and not the
    remainder, and a size that runs past the buffer is refused.

    For a JUCE plugin the bank's chunk is not the VST2 chunk yet: the JUCE
    trailer sits inside it, covered by the bank's size (ValhallaFreqEcho: a 344
    byte chunk, 284 of patch and 60 of trailer). The VST2 chunk is
    :func:`juce_private_data_strip` of what this returns.
    """
    if not buffer.startswith(VSTW_MAGIC):
        return None
    try:
        return FxbChunkBank.parse(buffer[VSTW_HEADER_BYTES:]).chunk
    except FormatError:
        return None


def vstw_wrap(chunk: bytes, fx_id: int, fx_version: int) -> bytes:
    """Wrap ``chunk`` in the ``VstW`` envelope a JUCE VST3 writes, for one plugin.

    Measured 2026-10-22 on ValhallaFreqEcho 1.2.8 and ValhallaSupermassive
    5.0.0: the 16 byte header reads 8, 1 and 0 (the last is the bypass flag),
    and the bank is version 2, holds no programs, and carries the plugin's VST2
    UniqueId as its fxID and its version code as its fxVersion -- the same
    number its Audio Unit declares as its component version. Built from those
    two numbers and the chunk, the envelope is byte for byte the one the plugin
    wrote, and a patch written this way reads back unchanged.

    The caller brings the two numbers: they belong to the target plugin, not to
    the chunk.
    """
    bank = FxbChunkBank(
        fx_id=fx_id.to_bytes(4, "big", signed=True),
        fx_version=fx_version,
        program_count=0,
        chunk=chunk,
        version=_VSTW_BANK_VERSION,
    )
    return VSTW_MAGIC + _VSTW_HEADER.pack(*_VSTW_HEADER_WORDS) + bank.encode()


_VSTW_HEADER = struct.Struct(">3I")
# Header size, version and bypass, in every envelope measured.
_VSTW_HEADER_WORDS = (8, 1, 0)
_VSTW_BANK_VERSION = 2


# What a JUCE VST3 appends to the plugin's own state, and a VST2 chunk or an
# Audio Unit's jucePluginState has not got. Read off Surge XT 1.3.4, Surge XT
# Effects 1.3.4, ValhallaFreqEcho 1.2.8 and ValhallaSupermassive 5.0.0 in all
# their formats on 2026-10-22, and found in soothe2 and Rift across the corpus:
#
#     int64 0
#     a ValueTree named JUCEPrivateData with one property, Bypass = false --
#         written only when the plugin has no bypass parameter of its own
#     int64 the ValueTree's byte count (0 when there is none)
#     the 15 bytes JUCEPrivateData, no terminator
#
# so the trailer is 60 bytes with the tree and 31 without. Surge XT has its own
# bypass parameter and writes the 31; the other three write the 60.
JUCE_PRIVATE_DATA_TAG = b"JUCEPrivateData"
_I64 = struct.Struct("<q")
# ValueTree::writeToStream for that one tree: the type name, one property, its
# name, a one-byte var holding the bool-false marker, no children.
_JUCE_BYPASS_TREE = JUCE_PRIVATE_DATA_TAG + b"\x00\x01\x01Bypass\x00\x01\x01\x03\x00"


def juce_private_data_add(chunk: bytes, *, own_bypass: bool) -> bytes:
    """``chunk`` with the trailer a JUCE VST3 writes after it, not bypassed.

    ``own_bypass`` is a fact about the plugin, not the patch: whether it exposes
    a bypass parameter of its own, which decides whether JUCE stores a bypass
    flag here at all. Only the un-bypassed trailer is written, because it is the
    only one measured; Live keeps a device's on/off switch itself.
    """
    tree = b"" if own_bypass else _JUCE_BYPASS_TREE
    return chunk + _I64.pack(0) + tree + _I64.pack(len(tree)) + JUCE_PRIVATE_DATA_TAG


def juce_private_data_strip(state: bytes) -> bytes | None:
    """The plugin's own state inside a JUCE VST3 state, or None if there is no trailer.

    The trailer says how long it is -- the size word in front of its closing
    tag -- so both lengths come off, and a buffer that merely ends with the tag
    does not, unless the eight bytes where the trailer opens are the zero JUCE
    writes there.
    """
    if not state.endswith(JUCE_PRIVATE_DATA_TAG):
        return None
    size_at = len(state) - len(JUCE_PRIVATE_DATA_TAG) - _I64.size
    if size_at < _I64.size:
        return None
    (size,) = _I64.unpack_from(state, size_at)
    start = size_at - size - _I64.size
    if size < 0 or start < 0 or state[start : start + _I64.size] != bytes(_I64.size):
        return None
    return state[:start]


# -- the framings a plugin format names -------------------------------------
# Each is a :class:`~abletoolz.plugins.state.plugin_map.Framing`.


@dataclasses.dataclass(frozen=True, slots=True)
class Verbatim:
    """The format holds the plugin's own patch unchanged."""

    def lift(self, state: bytes) -> bytes:
        return state

    def lower(self, patch: bytes) -> bytes:
        return patch


VERBATIM = Verbatim()


@dataclasses.dataclass(frozen=True, slots=True)
class JuceTrailer:
    """A JUCE VST3: the plugin's own state, then JUCE's private-data trailer."""

    own_bypass: bool

    def lift(self, state: bytes) -> bytes:
        patch = juce_private_data_strip(state)
        if patch is None:
            raise StateTransformError("A JUCE VST3 state ends in JUCE's trailer, and this one does not")
        return patch

    def lower(self, patch: bytes) -> bytes:
        return juce_private_data_add(patch, own_bypass=self.own_bypass)


@dataclasses.dataclass(frozen=True, slots=True)
class JuceVstw:
    """A JUCE VST3 built to replace its VST2: the trailer, inside a ``VstW`` bank.

    The bank carries the plugin's VST2 UniqueId and version code, which is why
    they are the framing's own fields.
    """

    fx_id: int
    fx_version: int
    own_bypass: bool = False

    def lift(self, state: bytes) -> bytes:
        inner = vstw_chunk(state)
        if inner is None:
            raise StateTransformError("This JUCE VST3 state is not inside a VstW bank")
        return JuceTrailer(self.own_bypass).lift(inner)

    def lower(self, patch: bytes) -> bytes:
        return vstw_wrap(juce_private_data_add(patch, own_bypass=self.own_bypass), self.fx_id, self.fx_version)
