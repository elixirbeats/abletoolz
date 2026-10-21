"""Live's document container, shared by sets and clips."""

from __future__ import annotations

import gzip
import pathlib
from xml.etree import ElementTree as ET

import pytest

from abletoolz.formats import FormatError, live_document

FIXTURES = pathlib.Path(__file__).parent
LIVE_WRITTEN = [
    FIXTURES / "version_fixtures" / "skeletons" / "12.4.5b.als",
    FIXTURES / "version_fixtures" / "skeletons" / "9.0.1.als",
    FIXTURES / "alc_fixtures" / "audeka_live_saved.alc",
]
# No flags, no timestamp, best compression, OS unknown: what Live wrote in every file above.
LIVE_GZIP_HEADER = bytes.fromhex("1f8b08000000000002ff")


@pytest.mark.parametrize("path", LIVE_WRITTEN, ids=lambda path: path.name)
def test_live_writes_the_header_this_module_writes(path: pathlib.Path) -> None:
    assert path.read_bytes()[:10] == LIVE_GZIP_HEADER
    assert live_document.encode(live_document.read(path))[:10] == LIVE_GZIP_HEADER


@pytest.mark.parametrize("path", LIVE_WRITTEN, ids=lambda path: path.name)
def test_a_document_round_trips_to_live_s_own_xml(path: pathlib.Path) -> None:
    """The same XML comes back, whitespace included, compared canonically.

    Two textual differences are Live's and not this module's, which is why the
    comparison is canonical. Line ends: Live writes CRLF in some documents and
    LF in others (both occur in the corpus; this Windows-saved clip is CRLF),
    parsing reads both as LF, and LF goes back out. Quoting: Live puts an
    attribute holding a double quote in single quotes, ElementTree escapes it
    inside double ones. Live opens the result: the grid-fixed clip it verified
    was written this way.
    """
    original = gzip.decompress(path.read_bytes()).decode("utf-8")
    written = gzip.decompress(live_document.encode(live_document.read(path))).decode("utf-8")
    assert ET.canonicalize(written) == ET.canonicalize(original)


def test_plain_xml_is_a_document_too(tmp_path: pathlib.Path) -> None:
    """A set unzipped by hand; Live opens those."""
    path = tmp_path / "unzipped.als"
    path.write_bytes(gzip.decompress(LIVE_WRITTEN[0].read_bytes()))
    assert live_document.read(path).tag == "Ableton"


def test_a_pre_8_2_document_is_refused_by_name() -> None:
    with pytest.raises(live_document.Pre82DocumentError):
        live_document.parse(b"\xab\x1e" + bytes(30))


@pytest.mark.parametrize("data", [b"not a live document", gzip.compress(b"")])
def test_bytes_that_are_not_a_document_say_so(data: bytes) -> None:
    with pytest.raises(FormatError):
        live_document.parse(data)


@pytest.mark.parametrize("data", [b"\x1f\x8b\x08\0", gzip.compress(b"<Ableton/>")[:-5]])
def test_truncated_gzip_uses_the_document_error_contract(data: bytes) -> None:
    with pytest.raises(FormatError, match="corrupt or truncated gzip"):
        live_document.parse(data)


def test_a_read_error_names_the_file(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "broken.alc"
    path.write_bytes(b"nope")
    with pytest.raises(FormatError, match="broken.alc"):
        live_document.read(path)


def test_a_write_leaves_no_temp_file_behind(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "out.als"
    live_document.write(path, live_document.encode(live_document.read(LIVE_WRITTEN[0])))
    assert [entry.name for entry in tmp_path.iterdir()] == ["out.als"]
