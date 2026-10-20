"""Live's document container: gzip around an ``<Ableton>`` XML document.

A set (``.als``), a clip (``.alc``), a device or rack preset (``.adv``,
``.adg``) and a groove (``.agr``) are the same file with different content: a
gzip stream holding the XML Live serializes, with its prolog and a trailing
newline. Live also opens the XML uncompressed, which is what a set exported or
unzipped by hand is, so that is read too.

The gzip header is Live's own, byte for byte: no flags, a zero timestamp,
extra flags 2 (best compression), OS 255 -- ``1f 8b 08 00 00 00 00 00 02 ff``
in every set and clip Live wrote that this library holds. ``gzip.open``
writes something else: the FNAME flag and the file name, and the current
time. Live's browser refuses to index a clip whose flag byte is not 0
(measured on ``.alc``), so nothing here uses it.

Sets older than Live 8.2 are a different container altogether and open with
``ab 1e``; they are refused by name rather than misread.
"""

from __future__ import annotations

import gzip
import os
import pathlib
from xml.etree import ElementTree as ET

from abletoolz.formats.errors import FormatError

GZIP_MAGIC = b"\x1f\x8b"
PRE_8_2_MAGIC = b"\xab\x1e"  # yes, it spells able
XML_MAGIC = b"<?"

XML_HEADER = b'<?xml version="1.0" encoding="UTF-8"?>\n'
XML_FOOTER = b"\n"

# What Live's header carries: best compression (extra flags 2), no timestamp, and
# OS 255. zlib stamps the OS it was built for (3 on Linux, 19 on macOS), so the
# OS byte is set after compressing.
_COMPRESSLEVEL = 9
_MTIME = 0
_OS_OFFSET = 9
_OS_UNKNOWN = 0xFF


class Pre82DocumentError(FormatError):
    """A document from before Live 8.2, which is not gzip and not XML."""


def to_xml_bytes(root: ET.Element) -> bytes:
    """The document as Live serializes it: prolog, XML, trailing newline."""
    xml: bytes = ET.tostring(root, encoding="utf-8")
    return XML_HEADER + xml + XML_FOOTER


def parse(data: bytes) -> ET.Element:
    """The ``<Ableton>`` root of a document, compressed or not."""
    magic = data[:2]
    if magic == PRE_8_2_MAGIC:
        raise Pre82DocumentError("a document from before Live 8.2, which is not supported")
    if magic == GZIP_MAGIC:
        xml = gzip.decompress(data)
    elif magic == XML_MAGIC:
        xml = data
    else:
        raise FormatError(f"not a Live document: it opens with {magic.hex()}, neither gzip nor XML")
    if not xml:
        raise FormatError("an empty gzip stream: the file is truncated or corrupt")
    return ET.fromstring(xml)


def encode(root: ET.Element) -> bytes:
    """The document compressed the way Live writes it."""
    data = bytearray(gzip.compress(to_xml_bytes(root), compresslevel=_COMPRESSLEVEL, mtime=_MTIME))
    data[_OS_OFFSET] = _OS_UNKNOWN
    return bytes(data)


def read(path: pathlib.Path) -> ET.Element:
    """The document at ``path``; a :class:`FormatError` names the file."""
    try:
        return parse(path.read_bytes())
    except FormatError as error:
        raise type(error)(f"{path}: {error}") from error


def write(path: pathlib.Path, document: bytes) -> None:
    """Write a document :func:`encode` made to ``path`` atomically: a temp file, then a rename.

    It takes the bytes rather than the tree so a caller that must know the tree
    serializes before touching the disk does not compress a large set twice. An
    interrupted write leaves the old file or none, never half of the new one.
    """
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_bytes(document)
    os.replace(tmp_path, path)
