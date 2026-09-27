"""Live's in-XML hex text, and the pre-11 path blob stored in it."""

from __future__ import annotations

import gzip
import pathlib
from xml.etree import ElementTree as ET

import pytest

from abletoolz.formats import FormatError, hex_text, path_data
from abletoolz.misc import SetOperatingSystem

SKELETONS = sorted((pathlib.Path(__file__).parent / "version_fixtures" / "skeletons").glob("*.als"))
HEX_TAGS = frozenset({"Buffer", "ProcessorState", "ControllerState", "Data"})


def hex_texts(path: pathlib.Path) -> list[str]:
    root = ET.fromstring(gzip.decompress(path.read_bytes()))
    texts = (element.text for element in root.iter() if element.tag in HEX_TAGS)
    return [text for text in texts if text is not None and text.strip()]


@pytest.mark.parametrize("path", SKELETONS, ids=lambda path: path.stem)
def test_live_s_own_hex_text_re_encodes_to_itself(path: pathlib.Path) -> None:
    """The wrapping is Live's, character for character, at the depth it was read."""
    for text in hex_texts(path):
        assert hex_text.encode(hex_text.decode(text), hex_text.depth(text)) == text


def test_hex_text_is_wrapped_at_eighty_characters_behind_the_indent() -> None:
    text = hex_text.encode(bytes(range(50)), 3)
    lines = text.split("\n")
    assert lines[0] == ""
    assert lines[1] == "\t\t\t" + bytes(range(40)).hex().upper()
    assert lines[2] == "\t\t\t" + bytes(range(40, 50)).hex().upper()
    assert lines[3] == "\t\t"


@pytest.mark.parametrize("text", [None, "", "\n\t\t\n\t"])
def test_an_empty_element_holds_no_bytes(text: str | None) -> None:
    assert hex_text.decode(text) == b""


def test_text_that_is_not_hex_says_so() -> None:
    with pytest.raises(FormatError):
        hex_text.decode("\n\t\tNOT HEX\n\t")


def test_a_one_line_text_has_no_depth_to_read() -> None:
    """A third-party generator writes the hex on the tag's own line."""
    with pytest.raises(FormatError):
        hex_text.depth("50006800")


def test_a_windows_path_round_trips() -> None:
    path = r"C:\Samples\kick.wav"
    decoded = path_data.decode(path_data.encode(path))
    assert decoded == path_data.PathData(SetOperatingSystem.WINDOWS_OS, path)


def test_a_path_outside_ascii_is_written_as_utf16() -> None:
    """The old encoder wrote one UTF-8 byte and a zero per byte, which is UTF-16 only for ASCII."""
    path = r"C:\Samples\Björk – café.wav"
    assert path_data.encode(path) == path.encode("utf-16-le") + b"\x00\x00"
    assert path_data.decode(path_data.encode(path)).path == path


def test_an_empty_data_element_holds_no_path() -> None:
    assert path_data.read("") is None
