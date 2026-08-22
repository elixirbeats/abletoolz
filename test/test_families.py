"""Naming a buffer by its bytes, and the reframes the families share.

Hermetic, and deliberately so: every buffer here is synthesized from the layout
:mod:`abletoolz.plugin_parsers.state.families` documents, short enough to read
in the source, and belongs to nobody. A corpus blob would test the corpus.

The detection tests are as much about the *order* of the rules as the rules
themselves -- an ``FFBS`` tag is four printable characters, a zip's content may
be text, and an empty buffer is vacuously all of them -- so each of those is its
own case rather than a line in the table.
"""

from __future__ import annotations

import struct

import pytest

from abletoolz.plugin_parsers.state import StateTransform, state_bytes
from abletoolz.plugin_parsers.state.families import (
    FXB_CHUNK_BANK,
    FXB_MAGIC,
    JUCE_PRIVATE_DATA_BYTES,
    JUCE_PRIVATE_DATA_TAG,
    VSTW_MAGIC,
    Family,
    detect,
    izotope_unwrap,
    izotope_wrap,
    juce_private_data_strip,
    kilohearts_unwrap,
    kilohearts_wrap,
    length_prefix_add,
    length_prefix_strip,
    printable_fraction,
    vstw_chunk,
)

# Where the survey found a VST2 chunk inside a VstW envelope. Nothing in the
# library holds that number; it falls out of the two headers, and this is what
# says so.
MEASURED_CHUNK_OFFSET = 176


def fxb_chunk_bank(chunk: bytes, *, form: bytes = FXB_CHUNK_BANK, declared: int | None = None) -> bytes:
    """An FXB bank of the chunk form, laid out the way the VST2 spec fixes it.

    Magic, the byte count everything after it, the form tag, a version, the
    plugin's fxID and fxVersion, a program count, 128 reserved bytes, the chunk
    size, the chunk.
    """
    return b"".join(
        (
            FXB_MAGIC,
            struct.pack(">I", 152 + len(chunk)),
            form,
            struct.pack(">4I", 2, 0x54455354, 1, 0),
            bytes(128),
            struct.pack(">I", len(chunk) if declared is None else declared),
            chunk,
        )
    )


def vstw_envelope(bank: bytes) -> bytes:
    """The 16 byte header a JUCE host puts in front of an FXB bank."""
    return VSTW_MAGIC + struct.pack(">3I", 8, 1, 0) + bank


def juce_state(chunk: bytes) -> bytes:
    """A VST2 chunk with the 60 byte private-data block a JUCE VST3 appends."""
    return chunk + bytes(JUCE_PRIVATE_DATA_BYTES - len(JUCE_PRIVATE_DATA_TAG)) + JUCE_PRIVATE_DATA_TAG


# -- what a buffer is -------------------------------------------------------

# One minimal example per family, which is also the list a new family has to
# join before the completeness test below passes.
FAMILY_EXAMPLES: dict[Family, bytes] = {
    Family.EMPTY: b"",
    Family.ZIP: b"PK\x03\x04\x14\x00\x00\x00\x08\x00" + bytes(20),
    Family.GZIP: b"\x1f\x8b\x08\x00" + bytes(16),
    Family.ZLIB: b"\x78\x9c" + bytes(20),
    Family.VSTW: vstw_envelope(fxb_chunk_bank(b"chunk")),
    Family.FXB: fxb_chunk_bank(b"chunk"),
    Family.JUCE: b"VC2!" + struct.pack("<I", 6) + b"<x/>\x00\x00",
    Family.FABFILTER: b"FFBS" + struct.pack("<II", 1, 1) + struct.pack("<f", 0.5),
    Family.XML: b'<?xml version="1.0"?><Preset><Cutoff>0.5</Cutoff></Preset>',
    Family.JSON: b'{"gain": 0.5, "mix": 1.0}',
    Family.TEXT: b"WIDGET = 3\nMIX = 0.5\n",
    # A short binary header in front of the vendor's own preset text.
    Family.MOSTLY_TEXT: bytes(8) + b"cutoff=0.5 resonance=0.25 drive=0.75 mix=1",
    # Zero words, then a vendor 4CC, then a compressed body.
    Family.ZERO_PREFIXED: bytes(16) + b"abnk" + bytes(60),
    Family.OPAQUE: bytes(range(128, 200)),
}


@pytest.mark.parametrize("family", list(Family))
def test_every_family_has_an_example(family: Family) -> None:
    """A family nothing exercises is a rule nobody has read."""
    assert family in FAMILY_EXAMPLES


@pytest.mark.parametrize(("family", "buffer"), sorted(FAMILY_EXAMPLES.items()))
def test_a_buffer_is_named_by_its_own_bytes(family: Family, buffer: bytes) -> None:
    assert detect(buffer) is family


def test_an_empty_buffer_is_empty_rather_than_text() -> None:
    """7.6% of devices, and no bytes would otherwise pass every printable test."""
    assert detect(b"") is Family.EMPTY
    assert printable_fraction(b"") == 0.0


def test_a_vendor_tag_wins_over_the_text_it_is_made_of() -> None:
    """FFBS, FabF and CcnK are all four printable characters."""
    assert detect(b"FFBS" + b"0123456789") is Family.FABFILTER
    assert detect(b"CcnK" + b"0123456789") is Family.FXB


def test_a_compressed_body_is_named_before_whatever_is_inside_it() -> None:
    """The magic is a header; what it inflates to is a separate question."""
    assert detect(b"PK\x03\x04" + b"state.json is in here") is Family.ZIP


def test_xml_and_json_are_told_apart_by_their_first_character() -> None:
    assert detect(b"<Preset/>") is Family.XML
    assert detect(b'[{"band": 1}]') is Family.JSON


def test_xfer_s_keyword_counts_as_json() -> None:
    """Serum 2 writes it at offset 0 in every distinct buffer the corpus holds."""
    assert detect(b'XferJson{"osc": 1}') is Family.JSON


def test_fully_printable_beats_mostly_printable() -> None:
    """Otherwise plain text would answer to the framed family it is a special case of."""
    assert detect(b"MIX = 1.0") is Family.TEXT
    assert detect(b"\x00\x01MIX = 1.0") is Family.MOSTLY_TEXT


def test_a_binary_buffer_with_no_landmark_is_opaque() -> None:
    """37% of the library, and saying so is the honest answer."""
    assert detect(bytes([0x93, 0x27, 0xA1, 0x04, 0xFF, 0xB2, 0x11, 0xC8])) is Family.OPAQUE


# -- the reframes the families share ----------------------------------------


def test_the_kilohearts_header_is_a_version_and_a_length() -> None:
    """The byte-for-byte fixture the transform has always been held to."""
    assert kilohearts_wrap(b"payload") == b"\x01\x00\x00\x00\x07\x00\x00\x00payload"


def test_the_kilohearts_transform_is_the_primitive_itself() -> None:
    """``state: kilohearts`` names this reframe rather than a copy of it."""
    assert state_bytes(StateTransform.KILOHEARTS) is kilohearts_wrap


@pytest.mark.parametrize("payload", [b"", b"PK\x03\x04zip", bytes(300)])
def test_the_kilohearts_framing_round_trips(payload: bytes) -> None:
    assert kilohearts_unwrap(kilohearts_wrap(payload)) == payload


@pytest.mark.parametrize(
    "state",
    [
        b"",
        b"\x01\x00\x00",
        # The right version, a length that does not account for the rest.
        struct.pack("<II", 1, 99) + b"payload",
        # The right length behind a version nothing has ever written.
        struct.pack("<II", 2, 7) + b"payload",
    ],
)
def test_a_buffer_that_is_not_kilohearts_framed_says_so(state: bytes) -> None:
    assert kilohearts_unwrap(state) is None


def test_the_izotope_nesting_unwraps_to_the_processor_state() -> None:
    """The other byte-for-byte fixture: a length, the state, and the preset name."""
    chunk = struct.pack("<I", 5) + b"patch" + struct.pack("<I", 7) + b"Default"
    assert izotope_unwrap(chunk) == b"patch"


def test_the_izotope_nesting_round_trips() -> None:
    chunk = izotope_wrap(b"processor state", b"Default")
    assert chunk == struct.pack("<I", 15) + b"processor state" + struct.pack("<I", 7) + b"Default"
    assert izotope_unwrap(chunk) == b"processor state"


@pytest.mark.parametrize(
    "chunk",
    [
        b"",
        # A leading length that is plausible and a trailing name that is not.
        struct.pack("<I", 5) + b"patch" + struct.pack("<I", 99) + b"Default",
        # A leading length longer than the chunk.
        struct.pack("<I", 4096) + b"patch" + struct.pack("<I", 7) + b"Default",
        # No length at all.
        bytes(4) + b"patch" + struct.pack("<I", 7) + b"Default",
    ],
)
def test_a_chunk_whose_two_lengths_disagree_is_not_the_nesting(chunk: bytes) -> None:
    assert izotope_unwrap(chunk) is None


def test_a_length_prefix_round_trips() -> None:
    """Diva's VST3 does this to the preset text its VST2 stores bare."""
    assert length_prefix_add(b"#pgm=Bass") == struct.pack("<I", 9) + b"#pgm=Bass"
    assert length_prefix_strip(length_prefix_add(b"#pgm=Bass")) == b"#pgm=Bass"


@pytest.mark.parametrize("buffer", [b"", b"\x02\x00", struct.pack("<I", 3) + b"#pgm=Bass"])
def test_a_length_that_does_not_account_for_the_rest_is_refused(buffer: bytes) -> None:
    assert length_prefix_strip(buffer) is None


def test_the_vstw_envelope_gives_up_the_chunk_the_bank_declares() -> None:
    state = vstw_envelope(fxb_chunk_bank(b"the vst2 chunk"))
    assert vstw_chunk(state) == b"the vst2 chunk"


def test_the_measured_offset_falls_out_of_the_two_headers() -> None:
    """The survey read the chunk at 176; nothing here writes that number down."""
    state = vstw_envelope(fxb_chunk_bank(b"the vst2 chunk"))
    assert state.index(b"the vst2 chunk") == MEASURED_CHUNK_OFFSET


def test_a_declared_size_is_read_rather_than_the_rest_of_the_buffer() -> None:
    """A state with a trailer after the chunk gives up the chunk, not both."""
    state = juce_state(vstw_envelope(fxb_chunk_bank(b"the vst2 chunk")))
    assert vstw_chunk(state) == b"the vst2 chunk"


@pytest.mark.parametrize(
    "state",
    [
        b"",
        b"VstW",
        # A program bank rather than a chunk bank: there is no chunk in one.
        vstw_envelope(fxb_chunk_bank(b"the vst2 chunk", form=b"FxBk")),
        # A chunk size that runs past what is there.
        vstw_envelope(fxb_chunk_bank(b"the vst2 chunk", declared=4096)),
        # The bank without the envelope in front of it.
        fxb_chunk_bank(b"the vst2 chunk"),
    ],
)
def test_a_buffer_that_is_not_a_vstw_envelope_says_so(state: bytes) -> None:
    assert vstw_chunk(state) is None


def test_the_juce_trailer_comes_off_a_vst3_state() -> None:
    """soothe2 and Rift: the VST3 state is the whole VST2 chunk plus 60 bytes."""
    assert juce_private_data_strip(juce_state(b"the vst2 chunk")) == b"the vst2 chunk"


@pytest.mark.parametrize(
    "state",
    [
        b"",
        b"the vst2 chunk",
        # The tag, and nothing in front of it to keep.
        JUCE_PRIVATE_DATA_TAG,
        # Sixty trailing bytes that are not the trailer.
        b"the vst2 chunk" + bytes(JUCE_PRIVATE_DATA_BYTES),
    ],
)
def test_a_state_without_the_trailer_is_left_alone(state: bytes) -> None:
    assert juce_private_data_strip(state) is None
