"""What a state buffer is, read off its own bytes, and the moves families share.

Two things the library had no way to do, and they are the same thing twice.

**Name a buffer.** :func:`detect` classifies a blob into the container families
MODEL.md's "What a buffer is" table measured over 23,786 device instances in 811
sets. It asks the bytes and never the plugin name, which is the rule a vendor
forced: the corpus holds Kilohearts states from before that vendor moved to a
zip container, as flat binary, under the same name as the zip ones.

**Reframe a buffer.** The 2026-08-15 readback survey found the same handful of
shapes across unrelated vendors -- wrap in a header, strip a trailer, prefix a
length -- so the operations here are per *family*, not per vendor. Each is
measured on named products, and none of them is speculative: a shape nobody has
seen twice is not here.

The unwrap directions answer ``None`` rather than raising, because a primitive
is asked two questions at once -- is this that shape, and if so what is inside
it -- and the second only means something when the first is yes. What to say
about a no belongs to the caller, which is why the messages a ``state:`` entry
prints still live beside the policy it names.

One asymmetry, and it is deliberate: :func:`juce_private_data_strip` has no
partner. See its docstring.
"""

from __future__ import annotations

import enum
import struct

# Little-endian words everywhere below. Ableton writes little-endian and so does
# every framing measured here; the one big-endian thing is the FXB bank inside a
# VstW envelope, which is big-endian by the VST2 spec.
_U32 = struct.Struct("<I")
_U32_BE = struct.Struct(">I")


# -- what a buffer is -------------------------------------------------------


class Family(enum.StrEnum):
    """The container families measured across the corpus, cheapest to read first.

    A family says how far a buffer can be read without the plugin, which is what
    decides whether a conversion is a header job or a re-encode. The shares each
    one holds are in MODEL.md's table; the largest is :data:`OPAQUE` at 37%, and
    the text-like ones plus FabFilter's decoded formats are about half.
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
FXB_MAGIC = b"CcnK"

# JUCE's AudioProcessor::copyXmlToBinary: magic 0x21324356 little-endian, a
# uint32 length, then the XML. Measured on soothe2, Rift and ValhallaShimmer.
JUCE_MAGIC = b"VC2!"

# The tags a FabFilter state opens with, both generations. MODEL.md's table also
# counts a leading "Default " -- that is a Live bank's preset name rather than a
# vendor tag, so it is left to fxbk.LegacyBank, which reads the whole name field
# instead of guessing from eight bytes of it.
FABFILTER_TAGS = (b"FFBS", b"FFed", b"FabF")

# Xfer's JSON opens with its own keyword rather than a brace. Serum 2 writes it
# at offset 0 in all 474 distinct buffers the corpus holds for it.
JSON_KEYWORD = b"XferJson"

# How many bytes of leading whitespace a text format may have before its first
# real character. Text buffers in the corpus have none; the window is what keeps
# the check off the whole blob, which can be 21 MB.
_LEADING_WINDOW = 64

# What the survey rig counted as printable, tabs and newlines included.
_PRINTABLE = bytes(byte for byte in range(256) if 0x20 <= byte < 0x7F or byte in (0x09, 0x0A, 0x0D))

# Where a framed text format stops looking like a binary one. MODEL.md's
# mostly-text family is a short binary header in front of the vendor's own
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
    Sound-validated on kHs Distortion and kHs Filter, and what
    ``state: kilohearts`` does.
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


def izotope_wrap(state: bytes, preset_name: bytes) -> bytes:
    """Nest a VST3 processor state the way an iZotope VST2 chunk holds it.

    The mirror of :func:`izotope_unwrap`: a length, the state, and the preset
    name as a length-prefixed string. The name is asked for rather than invented
    -- an iZotope chunk always carries one, ``Default`` in the devices measured,
    and which one it is belongs to the patch.
    """
    return length_prefix_add(state) + length_prefix_add(preset_name)


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


def length_prefix_add(payload: bytes) -> bytes:
    """Put a little-endian uint32 length in front of ``payload``.

    Diva's VST3 does exactly this to the preset text its VST2 stores bare, and
    it is the field iZotope's nesting is built out of twice.
    """
    return _U32.pack(len(payload)) + payload


def length_prefix_strip(buffer: bytes) -> bytes | None:
    """What a leading uint32 length covers, or None if it does not cover the rest.

    The length has to account for everything after it exactly. A four-byte word
    that merely reads as a plausible number in front of a shorter or longer body
    is a different container.
    """
    if len(buffer) < _U32.size:
        return None
    (length,) = _U32.unpack_from(buffer)
    if length != len(buffer) - _U32.size:
        return None
    return buffer[_U32.size :]


# A VstW envelope is a 16-byte header -- the magic and three big-endian words,
# reading 8, 1 and 0 in every buffer measured -- and then a whole FXB bank.
VSTW_HEADER_BYTES = 16

# The FXB chunk-bank form, which is the one that carries an opaque chunk: magic,
# byte count, this form tag, version, fxID, fxVersion, program count, 128 future
# bytes, the chunk size, the chunk. FxBk is the other form and holds programs
# rather than a chunk, so it is refused here rather than misread.
FXB_CHUNK_BANK = b"FBCh"
_FXB_CHUNK_SIZE_OFFSET = 156
_FXB_CHUNK_OFFSET = 160


def vstw_chunk(buffer: bytes) -> bytes | None:
    """The VST2 chunk inside a ``VstW`` envelope, or None if it is not one.

    Valhalla, Eventide and Soundtoys VST3 devices carry their VST2 chunk here
    unchanged, which the survey found at byte 176 of the state. The 176 is not
    written down below and does not need to be: it is this envelope's 16 bytes
    plus the FXB header the spec fixes at 156 bytes and a size word. What the
    bank declares is read rather than assumed, so a state carrying something
    after the chunk -- a JUCE trailer, say -- gives up the chunk and not the
    remainder, and a size that runs past the buffer is refused.

    This one has no partner either, for a different reason than the JUCE
    trailer: a bank header carries the plugin's own fxID and fxVersion, which
    the chunk does not say. Writing an envelope means knowing the target plugin,
    not just the bytes in hand.
    """
    if not buffer.startswith(VSTW_MAGIC):
        return None
    bank = buffer[VSTW_HEADER_BYTES:]
    if len(bank) < _FXB_CHUNK_OFFSET or not bank.startswith(FXB_MAGIC) or bank[8:12] != FXB_CHUNK_BANK:
        return None
    (size,) = _U32_BE.unpack_from(bank, _FXB_CHUNK_SIZE_OFFSET)
    if _FXB_CHUNK_OFFSET + size > len(bank):
        return None
    return bank[_FXB_CHUNK_OFFSET : _FXB_CHUNK_OFFSET + size]


# What a JUCE host appends to a VST3 state that a VST2 chunk has not got.
# soothe2 and Rift's VST3 state is the whole VST2 chunk plus these 60 bytes.
JUCE_PRIVATE_DATA_TAG = b"JUCEPrivateData"
JUCE_PRIVATE_DATA_BYTES = 60


def juce_private_data_strip(state: bytes) -> bytes | None:
    """The VST2 chunk inside a JUCE VST3 state, or None if there is no trailer.

    One-directional on purpose. Stripping is structural -- the trailer ends with
    its own name, so a buffer either has one or does not -- but writing one
    would mean writing 60 bytes this library has never derived from anything it
    holds. They were the same 60 bytes in every soothe2 and Rift device
    measured, and constant in every sample is not the same as understood, so the
    add direction stays unwritten rather than invented.
    """
    if len(state) <= JUCE_PRIVATE_DATA_BYTES or not state.endswith(JUCE_PRIVATE_DATA_TAG):
        return None
    return state[:-JUCE_PRIVATE_DATA_BYTES]
