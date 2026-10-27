"""Naming a buffer by its bytes, and the reframes the families share.

Hermetic, and deliberately so: every buffer here is synthesized from the layout
:mod:`abletoolz.plugins.state.framings` documents, short enough to read
in the source, and belongs to nobody. A corpus blob would test the corpus.

The detection tests are as much about the *order* of the rules as the rules
themselves -- an ``FFBS`` tag is four printable characters, a zip's content may
be text, and an empty buffer is vacuously all of them -- so each of those is its
own case rather than a line in the table.
"""

from __future__ import annotations

import struct

import pytest

from abletoolz.formats.fxb import FXB_CHUNK_BANK, FXB_MAGIC
from abletoolz.plugins.state.framings import (
    JUCE_PRIVATE_DATA_TAG,
    VSTW_MAGIC,
    Family,
    detect,
    izotope_unwrap,
    juce_private_data_add,
    juce_private_data_strip,
    kilohearts_unwrap,
    printable_fraction,
    vstw_chunk,
    vstw_wrap,
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
    return juce_private_data_add(chunk, own_bypass=False)


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


def test_every_family_has_an_example() -> None:
    """A family nothing exercises is a rule nobody has read."""
    assert set(FAMILY_EXAMPLES) == set(Family)


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


def test_the_izotope_nesting_gives_up_the_processor_state() -> None:
    """A length, the state, and the preset name as a length-prefixed string."""
    chunk = struct.pack("<I", 15) + b"processor state" + struct.pack("<I", 7) + b"Default"
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


def test_the_vstw_envelope_gives_up_the_chunk_the_bank_declares_where_the_survey_found_it() -> None:
    """The survey read the chunk at 176; nothing here writes that number down."""
    state = vstw_envelope(fxb_chunk_bank(b"the vst2 chunk"))
    assert vstw_chunk(state) == b"the vst2 chunk"
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


@pytest.mark.parametrize(
    "state",
    [
        b"",
        b"the vst2 chunk",
        # The tag, and nothing in front of it to keep.
        JUCE_PRIVATE_DATA_TAG,
        # Sixty trailing bytes that are not the trailer.
        b"the vst2 chunk" + bytes(60),
    ],
)
def test_a_state_without_the_trailer_is_left_alone(state: bytes) -> None:
    assert juce_private_data_strip(state) is None


def test_the_juce_trailer_is_sixty_bytes_with_a_bypass_flag_and_thirty_one_without() -> None:
    """The flag is stored only for a plugin with no bypass parameter of its own."""
    assert len(juce_private_data_add(b"", own_bypass=False)) == 60
    assert juce_private_data_add(b"", own_bypass=True) == bytes(16) + JUCE_PRIVATE_DATA_TAG


@pytest.mark.parametrize("own_bypass", [False, True])
def test_either_trailer_comes_off_a_vst3_state_whole(own_bypass: bool) -> None:
    """soothe2 and Rift: the VST3 state is the whole VST2 chunk plus the trailer."""
    state = juce_private_data_add(b"the vst2 chunk", own_bypass=own_bypass)
    assert juce_private_data_strip(state) == b"the vst2 chunk"


def test_a_trailer_whose_size_runs_past_the_state_is_refused() -> None:
    state = bytearray(juce_state(b"chunk"))
    size_at = len(state) - len(JUCE_PRIVATE_DATA_TAG) - 8
    state[size_at : size_at + 8] = struct.pack("<q", 4096)
    assert juce_private_data_strip(bytes(state)) is None


def test_a_vstw_envelope_carries_the_juce_trailer_inside_its_bank() -> None:
    """Measured on Valhalla: the bank's chunk size covers the patch and the trailer."""
    state = vstw_wrap(juce_state(b"the vst2 chunk"), 0x54455354, 3)
    inner = vstw_chunk(state)
    assert inner == juce_state(b"the vst2 chunk")
    assert juce_private_data_strip(inner) == b"the vst2 chunk"
    assert state.index(b"the vst2 chunk") == MEASURED_CHUNK_OFFSET


def test_the_vstw_bank_carries_the_plugins_id_and_version() -> None:
    state = vstw_wrap(b"chunk", 1934451059, 327680)
    assert state[:16] == VSTW_MAGIC + struct.pack(">3I", 8, 1, 0)
    assert struct.unpack(">4i", state[28:44]) == (2, 1934451059, 327680, 0)
