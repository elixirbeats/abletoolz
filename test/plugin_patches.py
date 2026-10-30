"""Synthesized patches for the plugins whose patch names files, and the file paths their maps read.

Each builder lays a patch out the way its plugin's module documents it, short enough to read here and
belonging to nobody. ``test_file_paths`` holds the contract every one of these plugins keeps, as tables
over these builders; each vendor's own test file holds what is particular to that plugin.
"""

from __future__ import annotations

import json
import pathlib
import struct
from collections.abc import Iterable
from typing import Any

import cbor2

from abletoolz.formats import fastlz
from abletoolz.formats.fxb import FxbChunkBank
from abletoolz.plugins.identity.ids import StoredId, Vst2Id
from abletoolz.plugins.state import ni_container
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.native_instruments import CONTAINER_MAGIC
from abletoolz.plugins.state.plugin_map import FilePaths
from abletoolz.plugins.state.xfadelooper import LAYOUT_VERSION, MAGIC, PATH_FIELD_SIZE
from abletoolz.plugins.state.xfer import SERUM_2_VST3, XferJson

# The VST2 UniqueId each plugin stores.
SERATO = 1399681132
XFADE = 1163098214
MASCHINE = 1315523890
MASCHINE_FX = 1315523900
BATTERY = 1312973108
KONTAKT = 1315524405
KONTAKT_16OUT = 1315524407
TEXTURE = 1484419439
REAKTOR = 1315525174
CMI_V = 1129138550
IRIS = 1767524691
MPC = 1299211077
# Serum 2 is a VST3 only, so it is found by its class id.
SERUM_2 = SERUM_2_VST3


def file_paths(stored: int | StoredId) -> FilePaths:
    """The file paths of the plugin that stores ``stored``, found the way a set's device finds them.

    An int is a VST2 UniqueId; a plugin with no VST2 is named by its own id.
    """
    plugin_map = CATALOG.find(Vst2Id(stored) if isinstance(stored, int) else stored)
    assert plugin_map is not None and plugin_map.file_paths is not None
    return plugin_map.file_paths


# -- Serato Sample: a JSON project, maybe with bytes around it --------------------


def serato(song: dict[str, Any] | None, *, prefix: bytes = b"", suffix: bytes = b"") -> bytes:
    project: dict[str, Any] = {"Version": 35}
    if song is not None:
        project["sourceSong"] = song
    return prefix + json.dumps({"project": project}).encode() + suffix


# -- xfadelooper: a flat struct with a fixed 256 byte path field ------------------

XFADE_PARAMETERS = struct.pack("<88f", *(float(i) / 4.0 for i in range(88)))


def xfade(sample_path: str, *, residue: bytes = b"", version: bytes = LAYOUT_VERSION) -> bytes:
    """A state whose path field holds ``sample_path``, then the tail of an older, longer one."""
    encoded = sample_path.encode("latin-1")
    field = bytearray(PATH_FIELD_SIZE)
    field[: len(encoded)] = encoded
    field[len(encoded) + 1 : len(encoded) + 1 + len(residue)] = residue
    return MAGIC + version + b"Defaults".ljust(32, b"\x00") + bytes(field) + XFADE_PARAMETERS


# -- Native Instruments: the item container ---------------------------------------


def utf16_string(text: str) -> bytes:
    """NI's string framing: a u32 character count, then UTF-16LE, no terminator."""
    return struct.pack("<I", len(text)) + text.encode("utf-16-le")


def maschine_strings(strings: Iterable[str], *, magic: bytes = CONTAINER_MAGIC) -> bytes:
    """Framed strings behind the container magic, and no item container around them."""
    body = b"".join(utf16_string(text) for text in strings)
    return struct.pack("<QI", 16 + len(body), 1) + magic + body


def frame(domain: bytes, kind: int, data: bytes, base: bytes | None = None) -> bytes:
    inner = struct.pack("<Q4sII", 24, b"DSIN", 1, 1) + struct.pack("<I", 1) if base is None else base
    return struct.pack("<Q4sII", 20 + len(inner) + len(data), domain, kind, 1) + inner + data


def item(data_frame: bytes, children: tuple[bytes, ...] = ()) -> bytes:
    head = struct.pack("<I", 1) + b"hsin" + struct.pack("<Q", 1) + bytes(range(16))
    body = data_frame + struct.pack("<II", 1, len(children))
    for child in children:
        body += struct.pack("<I4sI", 0, b"DSIN", 0x75) + child
    return struct.pack("<Q", 8 + len(head) + len(body)) + head + body


def subtree(inner: bytes, *, compressed: bool) -> bytes:
    if not compressed:
        data = struct.pack("<IB", 1, 0) + inner
    else:
        packed = fastlz.compress(inner)
        data = struct.pack("<IBII", 1, 1, len(inner), len(packed)) + packed
    return frame(b"DSIN", 0x74, b"", frame(b"DSIN", ni_container.SUBTREE_TYPE, data))


def patch_data(paths: list[str]) -> bytes:
    """A patch frame's data: a program record, then the file list of segment paths."""
    file_list = b"\x02\x00\x02\x00\x00\x00" + struct.pack("<I", len(paths))
    file_list += b"".join(ni_container.encode_segment_path(path) for path in paths)
    stream = struct.pack("<HI", 3, 7) + b"program" + struct.pack("<HI", 0x4B, len(file_list)) + file_list
    return struct.pack("<III", 1, 1, 1) + struct.pack("<Q", len(stream)) + stream + bytes(4) + b"\x0d\x62\x65\x85"


def battery(paths: list[str], *, compressed: bool = True) -> bytes:
    """A Battery or Kontakt state: the patch frame inside a (by default compressed) subtree."""
    inner = item(frame(b"DSIN", ni_container.PATCH_TYPE, patch_data(paths)))
    return item(frame(b"DSIN", 0x76, b"\x00" * 6), (item(subtree(inner, compressed=compressed)),))


KICK = "C:\\Users\\Public\\Documents\\Battery 4 Factory Library\\Samples\\Drums\\Kick\\Kick 1.wav"
KIT = "C:\\Users\\Public\\Documents\\Battery 4 Factory Library\\Kits\\Big Kit.nbkt"
KEYS = "D:\\Libraries\\Keys\\instruments\\Keys.nki"
KEYS_SAMPLE = "D:\\Libraries\\Keys\\samples\\keys_01.ncw"

RECORD_LENGTH = 1032


def record(fields: bytes) -> bytes:
    """A record blob as Maschine writes one: portable length, u32 10, u64 in use, fields, leftover."""
    body = struct.pack("<IQ", 10, len(fields)) + fields
    return b"\x02" + struct.pack("<H", RECORD_LENGTH) + body + b"\xab" * (RECORD_LENGTH - len(body))


def maschine_project(*paths_in_records: str, samplers: bytes = b"") -> bytes:
    """A Maschine project: one record per path, then the samplers' references, in one archive."""
    archive = b"\x01\x16serialization::archive" + b"\x00" * 9
    for path in paths_in_records:
        archive += record(utf16_string("Sample") + utf16_string(path) + utf16_string("Drums"))
    archive += samplers + b"\x00" * 16
    stream = struct.pack("<I", len(archive)) + archive
    data = struct.pack("<IIIQ", 1, 1, 1, len(stream)) + stream + bytes(4) + b"\x0d\x62\x65\x85"
    return item(frame(b"DSIN", ni_container.PATCH_TYPE, data))


SNARE = "C:\\Sample Libraries\\Maschine 2 Library\\Samples\\Drums\\Snare\\Snare 1.wav"


# -- Reaktor 6: the ensemble path in the state's head ------------------------------

ENSEMBLE = "D:/Native Instruments Packs/Monark Library/Monark.ens"
REAKTOR_REST = b"\x00\x00\x00\x00\x00\x09\x00\x00\x00\x01FP5R\x0d\x00\x00\x00"
REAKTOR_UTF16_COPY = "file://localhost/D:/Native%20Instruments%20Packs/Monark.ens".encode("utf-16-le")


def reaktor_url(path: str) -> bytes:
    """A head naming the ensemble by a NUL-terminated file URL."""
    url = "file:///" + path.replace(" ", "%20")
    head = b"\x014RIN\x10\x00\x00\x00\x00\x01CSAR\x05\x00\x00\x00\x00\x03\x00\x00\x00\x01\x01"
    return head + url.encode() + b"\x00" + REAKTOR_REST + REAKTOR_UTF16_COPY


def reaktor_plain(path: str) -> bytes:
    """A head naming the ensemble by a length-prefixed plain path."""
    head = b"\x014RIN\x11\x00\x00\x00\x00\x01CSAR\x06\x00\x00\x00\x00\x00\x00\x00\x00"
    return head + len(path.encode()).to_bytes(4, "little") + path.encode() + REAKTOR_REST + REAKTOR_UTF16_COPY


# -- Texture: the JUCE state, bare or in the VST3's VstW bank -----------------------

TEXTURE_SNARE = "C:\\Shared\\Samples\\Production\\Drum N Bass\\Emperor\\SNARE_02 & co.wav"
JUCE_TRAILER = (
    b"\x00" * 9 + b"JUCEPrivateData\x00\x01\x01Bypass\x00\x01\x01\x03\x00\x1d" + b"\x00" * 7 + b"JUCEPrivateData"
)


def texture_xml(sample: str | None) -> bytes:
    """Texture's state XML: a user sample by its file name, or one of Texture's own sounds by id."""
    if sample is None:
        data = '<ProcessorStateString string="8024e752f294485cba8f576a158b047c" __id="sample"/>'
    else:
        escaped = sample.replace("&", "&amp;")
        data = (
            '<ProcessorStateString string="UserSample" __id="sample"><source type="sampleTrigger">'
            f'<audio><file name="{escaped}" start="0.01" end="0.35" fadeOut="0.0"/></audio></source>'
            "</ProcessorStateString>"
        )
    return f'<?xml version="1.0" encoding="UTF-8"?> <state><parameters/><data>{data}</data></state>'.encode()


def texture_bare(sample: str | None) -> bytes:
    body = texture_xml(sample)
    return b"VC2!" + struct.pack("<I", len(body)) + body + b"\x00"


def texture_wrapped(sample: str | None) -> bytes:
    chunk = texture_bare(sample) + JUCE_TRAILER
    bank = FxbChunkBank(fx_id=b"Xzqo", fx_version=0x00010808, program_count=1, chunk=chunk, version=2)
    return b"VstW" + struct.pack(">III", 8, 1, 0) + bank.encode()


# -- CMI V: a fixed 256 byte path field per voice, in both sections ------------------

LOOP = "D:\\Production\\Samples 5\\Kasra\\LOOPS\\KAS_174_F_BASS_LOOP_11.wav"
FACTORY = "Factory/MALLETS/BASSMAR1.wav"
RESOURCE = "C:\\ProgramData\\Arturia\\CMI V\\resources/other/Factory/data0.bin"
CMI_HEAD = b"\x01\x16serialization::archive\x01\x0a\x00\x00\x00"


def cmi_voice(number: int, path: str) -> bytes:
    blob = CMI_HEAD + path.encode().ljust(256, b"\x00") + b"\x00\x00\x80\x3f"
    return f" 1 Voice{number}_AudioSampleObject {len(blob)} ".encode() + blob


def cmi_section(*samples: str) -> bytes:
    voices = b"".join(cmi_voice(n, s) for n, s in enumerate(samples, 1))
    return b"22 serialization::archive 10 0 7 0 7" + voices + b" 0"


def cmi_v(*samples: str) -> bytes:
    """A patch whose voices play ``samples``, in both of its sections."""
    first, second = cmi_section(*samples), cmi_section(*samples)
    return struct.pack("<QQ", len(first), len(second)) + first + second


# -- iZotope Iris and MPC Essentials: read, not yet rewritten -------------------------

SWAMP = "C:/Users/beep/Documents/iZotope/Iris/Iris Library/Samples/Animal/Swamp birds.irss"
SINE = "C:/Users/beep/AppData/Roaming/iZotope/Iris/Sub/Sine.irss"
PROJECT = "C:\\Users\\elixer\\Documents\\MPC\\Half time beats.xpj"


def counted(text: str) -> bytes:
    data = text.encode()
    return len(data).to_bytes(4, "little") + data


def iris(*samples: str) -> bytes:
    """Each layer's sample as a counted string, between other counted strings that are not samples."""
    middle = b"\x00\xb3\xf8".join(counted(sample) for sample in samples)
    return b"\x01\x00\x00\x00" + middle + b"\x01\x00" + counted("C:/a.txt")


def mpc(project: str) -> bytes:
    return (1).to_bytes(8, "little") + project.encode() + b"\x00Main mode\x00\x00"


# -- Serum 2: a zstd-compressed CBOR patch ---------------------------------------

SERUM_2_SAMPLE = r"C:\Samples\Drum_Loop_170.wav"


def serum_2_sample() -> bytes:
    """Serum 2's init patch with oscillator A's sample playing ``SERUM_2_SAMPLE``, written as a converter writes one."""
    init_state = pathlib.Path(__file__).parent / "state_fixtures" / "serum2" / "serum2-init.vst3.state"
    init = XferJson.parse(init_state.read_bytes())
    doc = cbor2.loads(init.decoded())
    doc["Oscillator0"]["SampleOsc0"]["samplePath"] = SERUM_2_SAMPLE
    return init.with_body(cbor2.dumps(doc)).encode()
