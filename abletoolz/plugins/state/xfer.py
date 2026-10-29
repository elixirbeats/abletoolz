"""Xfer Records' plugins.

Serum's VST2 and VST3 save the same compressed patch, and the VST3 writes no
controller half. Heard in Live 12.4.5 on 2026-08-10, and Serum's own
``moduleinfo.json`` declares that its VST3 accepts VST2 state.

Serum's Audio Unit keeps that same patch as a VST2 program under ``vstdata``
(:class:`~abletoolz.plugins.state.audio_unit.VstDataClassInfo`): in a Live
11.0.12 set saved on a Mac, its chunk inflates to the 172,736 bytes every Serum
VST2 chunk does. Pushed into the installed Serum VST3, such a patch comes back
75 bytes from what was pushed and as far from the default patch as it went in:
Serum restamps a patch's record versions and turns the Mac's ``/`` into
``\\``. It does the same to its own: VST2 and VST3 patches from Windows sets
come back 2 to 176 bytes changed. So a Mac set's Serum comes across to the
VST2 or VST3. The component's registered flags are not recorded, so the map
reads the AU and never writes one.

Serum 2 is another plugin, with its own class id. It ships as a VST3 and, on a
Mac, an AU; there is no VST2. Its AU is built from the VST3 by the VST3 SDK's
wrapper and keeps the VST3's processor state as it is
(:class:`~abletoolz.plugins.state.audio_unit.Vst3ClassInfo`). Measured on a Mac
with Serum2.component 2.1.5, headless in pedalboard: the smallest preset
abletoolz writes, identity and processor state, is restored -- the unit hands
back a patch naming the same sample, says it cannot open that file, and renders
away from its init patch (the init renders identically twice). So a Serum 2
device crosses between the VST3 and the AU either way. A written AU device has
not been opened in Live.

Both halves of its VST3 preset are one container, :class:`XferJson`:

    "XferJson" and a NUL
    u64  the length of a JSON header
         the header: component ("processor" or "controller"), hash, product,
         productVersion and version, plus the preset's name, author and
         description in the controller's
    u32  the body's decoded size
    u32  2, the codec: a zstd frame
         the body, a zstd frame whose MD5 is the header's ``hash``

The decoded body is CBOR: the patch, module by module (oscillators, envelopes,
LFOs, FX racks, clips), in the processor; editor-only settings in the
controller. Serum 2 saves .SerumPreset files in the same container under a
``fileType`` key.

What the host rig measured, Serum 2 2.1.4 in pedalboard, one process per load:

* **Real Serum 2 patches load.** A processor state pushed with its own
  controller state renders differently from the init patch unless it is the
  init patch. Patches saved by 2.0.16, 2.0.19, 2.0.24 and 2.1.4 all load, and
  the plugin rewrites each older one as its own version 10. A 2.1.4 state comes
  back with the same decoded size and differs in the last bits of a few float64
  envelope times, so not even Serum 2's own patch reads back byte for byte;
  that is the most a readback can say here.
* **The controller half carries no sound.** A patch pushed again with an empty
  controller state renders identically and leaves all 2,621 parameters where
  the controller-carrying push left them. What is lost is the preset's name,
  author and description, and editor settings: the plugin answers
  " - Init - " for the name. So the map writes the element empty.

**Serum 1 into Serum 2: refused as state, so re-encoded.** Serum 2 imports a
Serum 1 ``.fxp`` from its own browser, but not through the state a host hands
it; :mod:`abletoolz.plugins.converters.serum` rebuilds the patch instead.
Pushed into the installed VST3, each of these leaves the plugin on its init
patch, the readback the init state byte for byte and the render unmoved: a
Serum 1 VST2 chunk (zlib streams), the same chunk inflated to its 172,736
bytes, wrapped as an FXP program (both spellings of the size word), wrapped as
an FXB bank, an ``.fxp`` file Serum 1 wrote, a Serum 1 VST3 processor state,
and a chunk in the controller slot beside Serum 2's own processor state.

**The files a patch plays.** A Serum 2 oscillator names its file one of two
ways. Wavetables, noises, impulse responses and factory samples are relative to
Serum 2's own library (``relativePathToWT``, ``samplePathRelative`` and three
more keys), and they move with the library rather than with a set. A file the
user dragged in is ``samplePath``, absolute and spelled the way the saving
machine spells it, so it can point at a file this machine does not have, on
another computer's drive or in a project folder since moved. Those are what
:class:`Serum2SamplePaths` checks and relinks. A rewrite replaces that one CBOR string in place
(:mod:`abletoolz.formats.cbor`) and compresses the body again, with the size and
hash to match.

In the installed Serum 2, a patch whose sample is gone renders near silence
(RMS 0.014). Relinked to where the file is now, it renders exactly as a patch
already pointing there does (RMS 0.2115), and the plugin hands the new path
back. Pointed at a missing file, a working patch falls to the same silence, so
the path is what the plugin loads from, not a copy of the audio. Recompressed
with the path unchanged, a patch renders exactly as the plugin's own frame
does.

Serum 2 FX (class ``56534558667351736572756D20322066``) is a separate
plugin, and is not mapped.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import pathlib
import struct
import zlib
from typing import Any, cast

import zstandard

from abletoolz.formats import cbor
from abletoolz.formats.errors import FormatError
from abletoolz.plugins.container.controller import NO_CONTROLLER_STATE
from abletoolz.plugins.identity.ids import AuId, AuRegistration, Vst2Id, Vst3Id
from abletoolz.plugins.state.audio_unit import Vst3ClassInfo, VstDataClassInfo
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.framings import JSON_KEYWORD, VERBATIM
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import (
    AUDIO_SUFFIXES,
    FileRef,
    Layout,
    PluginFormat,
    PluginMap,
)

# Serum 1's chunk, the same bytes in its VST2 and VST3: a zlib stream holding the patch, a
# second zlib stream holding the wavetable frames it embeds (none when its
# tables are in Serum's library), then the first stream's compressed length as
# a little-endian u32. Three patch sizes share one front layout -- 172,736 bytes
# from current builds, 33,872 and 28,232 from older ones; 20,704 is an earlier
# format with another layout, and is refused.
SERUM_PATCH_SIZES = frozenset({172736, 33872, 28232})
_STREAM_LENGTH = struct.Struct("<I")


@dataclasses.dataclass(frozen=True, slots=True)
class SerumChunk:
    """A Serum 1 chunk: its patch and the wavetable frames it embeds."""

    patch: bytes
    tables: bytes

    @classmethod
    def parse(cls, chunk: bytes) -> SerumChunk:
        try:
            first = zlib.decompressobj()
            patch = first.decompress(chunk)
            rest = first.unused_data
            if not first.eof:
                raise StateTransformError("This Serum chunk ends inside its patch stream")
            tables = b""
            if len(rest) > _STREAM_LENGTH.size:
                second = zlib.decompressobj()
                tables = second.decompress(rest)
                if not second.eof:
                    raise StateTransformError("This Serum chunk ends inside its wavetable stream")
                rest = second.unused_data
        except zlib.error as error:
            raise StateTransformError(f"This Serum chunk is not the zlib streams Serum writes: {error}") from None
        if len(rest) != _STREAM_LENGTH.size or _STREAM_LENGTH.unpack(rest)[0] != len(chunk) - len(first.unused_data):
            raise StateTransformError("This Serum chunk does not end with its patch stream's length")
        if len(patch) not in SERUM_PATCH_SIZES:
            raise StateTransformError(f"A {len(patch)} byte Serum patch is a layout abletoolz has not measured")
        return cls(patch, tables)


SERUM_FX_ID = b"XfsX"
SERUM_FX_ID_VALUE = int.from_bytes(SERUM_FX_ID, "big")
# The parameter count a Serum AU program carries.
SERUM_PROGRAM_PARAMETERS = 315


@register
def serum() -> PluginMap:
    measured = MeasuredState(
        StateRung.VERBATIM, (StateEvidence.EAR, StateEvidence.DECLARED), datetime.date(2026, 8, 10)
    )
    in_vstdata = MeasuredState(StateRung.REFRAME, (StateEvidence.HOSTED,), datetime.date(2026, 10, 26))
    au = AuId.of("aumu", "XfsX", "XFER")
    return PluginMap(
        id="xfer.serum",
        name="Serum",
        version="1",
        scope="VST2 and VST3 save the same patch; the AU keeps it as a VST2 program, read only",
        vendor="Xfer Records",
        vst2=PluginFormat(Vst2Id(SERUM_FX_ID_VALUE), "Serum", VERBATIM, measured, plan_name="serum_vst2"),
        vst3=PluginFormat(
            Vst3Id((1448301656, 1718835315, 1701999981, 0)),
            "Serum",
            VERBATIM,
            measured,
            NO_CONTROLLER_STATE,
            plan_name="serum_vst3",
        ),
        # No registration is recorded for the AU, so a device is read from it and never written as one.
        au=PluginFormat(
            au,
            "Serum",
            VstDataClassInfo(au.identifier, SERUM_FX_ID, 1, SERUM_PROGRAM_PARAMETERS),
            in_vstdata,
            plan_name="serum_au",
        ),
    )


# -- Serum 2 -------------------------------------------------------------------

XFER_JSON_MAGIC = JSON_KEYWORD + b"\x00"
_HEADER_LENGTH = struct.Struct("<Q")
_BODY_HEADER = struct.Struct("<II")
# The one codec measured: a zstd frame, which opens with its magic.
ZSTD_CODEC = 2
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

# Serum 2's VST3 class id, ``56534558667350736572756D20320000``: the same four fields in its
# moduleinfo.json, in saved devices and in what the installed module's factory creates.
SERUM_2_VST3 = Vst3Id((1448297816, 1718833267, 1701999981, 540147712))
# Its AU, as Serum2.component 2.1.5 registers it on a Mac. The registration
# declares no sandboxSafe, so its flags are 0 (Surge XT declares it and stores 2).
SERUM_2_AU = AuId.of("aumu", "Xf2X", "XFER")
# The build the host rig measured: Serum2.vst3 2.1.4, x86_64-win.
SERUM_2_SHA256 = "74fac99ee5f9b6ee2668c8d7fc7f383d48db1d7f068b2e441f2caef7baa8400a"


@dataclasses.dataclass(frozen=True, slots=True)
class XferJson:
    """One Serum 2 state as its container holds it, the body still compressed.

    ``header`` is the JSON exactly as written, so :meth:`encode` gives back the
    same bytes it was read from.
    """

    header: bytes
    size: int
    codec: int
    body: bytes

    @property
    def document(self) -> dict[str, Any]:
        """The header, parsed."""
        return cast(dict[str, Any], json.loads(self.header))

    @property
    def component(self) -> str | None:
        """Which half of a VST3 preset this is, ``processor`` or ``controller``; None for a preset file."""
        value = self.document.get("component")
        return value if isinstance(value, str) else None

    @property
    def product_version(self) -> str | None:
        """The Serum 2 build that wrote this state."""
        value = self.document.get("productVersion")
        return value if isinstance(value, str) else None

    def decoded(self) -> bytes:
        """The body decompressed: the patch as CBOR."""
        try:
            body = zstandard.ZstdDecompressor().decompress(self.body, max_output_size=self.size)
        except zstandard.ZstdError as error:
            raise StateTransformError(f"This Serum 2 state's body is not a zstd frame that decodes: {error}") from None
        if len(body) != self.size:
            raise StateTransformError(f"This Serum 2 state's body decodes to {len(body)} bytes, not {self.size}")
        return body

    def with_body(self, decoded: bytes) -> XferJson:
        """This state holding ``decoded`` instead, compressed, with the size and hash to match.

        The header is kept as written except for the hash, which is the same
        length either way.
        """
        body = zstandard.ZstdCompressor().compress(decoded)
        old = str(self.document.get("hash")).encode("ascii")
        if self.header.count(old) != 1:
            raise StateTransformError("This Serum 2 state's header does not hold its hash exactly once")
        new = hashlib.md5(body, usedforsecurity=False).hexdigest().encode("ascii")
        return XferJson(self.header.replace(old, new), len(decoded), self.codec, body)

    def encode(self) -> bytes:
        return (
            XFER_JSON_MAGIC
            + _HEADER_LENGTH.pack(len(self.header))
            + self.header
            + _BODY_HEADER.pack(self.size, self.codec)
            + self.body
        )

    @classmethod
    def parse(cls, buffer: bytes) -> XferJson:
        """Read one container, refusing anything whose lengths or hash do not account for it."""
        if not buffer.startswith(XFER_JSON_MAGIC):
            raise StateTransformError("A Serum 2 state opens with XferJson and a NUL, and this one does not")
        at = len(XFER_JSON_MAGIC)
        if len(buffer) < at + _HEADER_LENGTH.size:
            raise StateTransformError("This Serum 2 state ends before its header length")
        (length,) = _HEADER_LENGTH.unpack_from(buffer, at)
        at += _HEADER_LENGTH.size
        if length > len(buffer) - at - _BODY_HEADER.size:
            raise StateTransformError("This Serum 2 state's header runs past its end")
        header = buffer[at : at + length]
        try:
            document = json.loads(header)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise StateTransformError("This Serum 2 state's header is not JSON") from None
        if not isinstance(document, dict):
            raise StateTransformError("This Serum 2 state's header is not a JSON object")
        at += length
        size, codec = _BODY_HEADER.unpack_from(buffer, at)
        body = buffer[at + _BODY_HEADER.size :]
        if codec != ZSTD_CODEC or not body.startswith(ZSTD_MAGIC):
            raise StateTransformError(f"This Serum 2 state's body is codec {codec}; only a zstd frame is measured")
        if hashlib.md5(body, usedforsecurity=False).hexdigest() != document.get("hash"):
            raise StateTransformError("This Serum 2 state's body does not match the hash its header gives")
        return cls(header, size, codec, body)


SAMPLE_PATH = "samplePath"
# What a Serum 2 oscillator loads from outside its library: audio, and an image
# for the spectral oscillator.
SERUM_2_SUFFIXES = AUDIO_SUFFIXES | {".png"}


@dataclasses.dataclass(frozen=True, slots=True)
class Serum2SamplePaths:
    """The files a Serum 2 patch loads by absolute path, and their relinking."""

    rewrites: bool = True
    when_empty: str | None = None
    suffixes: frozenset[str] = SERUM_2_SUFFIXES

    def _paths(self, patch: bytes) -> tuple[XferJson, bytes, tuple[cbor.TextItem, ...]]:
        state = XferJson.parse(patch)
        body = state.decoded()
        try:
            return state, body, cbor.text_values(body, SAMPLE_PATH)
        except FormatError as error:
            raise StateTransformError(f"This Serum 2 patch is not CBOR that reads: {error}") from None

    def read(self, patch: bytes) -> tuple[FileRef, ...]:
        return tuple(FileRef(item.value) for item in self._paths(patch)[2])

    def rewrite(self, patch: bytes, ref: FileRef, path: pathlib.Path) -> bytes:
        state, body, items = self._paths(patch)
        found = [item for item in items if item.value == ref.path]
        if not found:
            raise StateTransformError(f"This Serum 2 patch does not point at {ref.path}")
        # Back to front, so each replacement leaves the earlier offsets where they were.
        for item in reversed(found):
            body = cbor.replace_text(body, item, str(path))
        return state.with_body(body).encode()


def _processor_state(patch: bytes) -> bool:
    try:
        return XferJson.parse(patch).component == "processor"
    except StateTransformError:
        return False


@register
def serum_2() -> PluginMap:
    measured = MeasuredState(StateRung.VERBATIM, (StateEvidence.HOSTED,), datetime.date(2026, 10, 26))
    in_class_info = MeasuredState(StateRung.REFRAME, (StateEvidence.HOSTED,), datetime.date(2026, 10, 26))
    return PluginMap(
        id="xfer.serum-2",
        name="Serum 2",
        version="2",
        scope="VST3 2.1.4 and AU 2.1.5; reads back processor states written by 2.0.16, 2.0.19, 2.0.24 and 2.1.4",
        vendor="Xfer Records",
        vst3=PluginFormat(SERUM_2_VST3, "Serum 2", VERBATIM, measured, NO_CONTROLLER_STATE, plan_name="serum_2_vst3"),
        au=PluginFormat(
            SERUM_2_AU,
            "Serum 2",
            Vst3ClassInfo(SERUM_2_AU.identifier),
            in_class_info,
            registration=AuRegistration("Xfer Records", 0),
            plan_name="serum_2_au",
        ),
        # Every build's processor state loads in 2.1.4; anything else is not a Serum 2 patch.
        layouts=(Layout("XferJson processor state", _processor_state),),
        binary_sha256=(SERUM_2_SHA256,),
        file_paths=Serum2SamplePaths(),
    )
