"""Native Instruments' plugins.

Massive 1.7's VST2 chunk and VST3 processor state are the same NI item
container, byte for byte: one state loaded into the VST2 and the VST3 reads back
with only its per-save ids rewritten and every sound parameter identical,
measured in DawDreamer on 2026-09-26. Its VST3 controller half is not measured,
so the VST3 is not written from here yet.

Maschine 2 saves its whole project inside the plugin state -- a quarter of a
megabyte is normal -- and buries the references to the kits and samples it was
built from somewhere in the middle. When the drive those came from goes away,
nothing in a set tells you: the device still loads, it just cannot find its
sources any more. :class:`MaschinePaths` finds those references.

Battery 4 and Kontakt keep theirs inside a compressed subtree, as segment
paths in a patch frame; :class:`SegmentPaths` reads them and, for Battery,
rewrites them. The container, the patch frame and both path forms are
:mod:`abletoolz.plugins.state.ni_container`'s.

Reaktor 6 keeps a snapshot and names the ensemble file it belongs to;
:class:`EnsemblePaths` reads and relinks that one path.

Rewriting Battery 4
-------------------
Measured 2026-10-23 in DawDreamer with the installed Battery 4 VST2, on a
corpus kit of 237 files: a patch with a sample moved to a longer path, a
shorter one, a renamed file or a path of the same length plays exactly as the
original, and Battery saves it back with the new path in place. Pointing all
five velocity layers of the kick at a different kick changes the render at
that cell's note and nowhere else, so the path is what Battery reads. A path
to a file that does not exist makes Battery refuse the whole patch -- it plays
nothing and hands the bytes back as they came -- and every later load in the
same process with it, so a rewrite only ever names a file that was found.

Kontakt keeps the same patch frame and is rewritten the same way. Measured
2026-10-23 with the installed Kontakt 5 VST2 on a corpus set using an unlocked
library (Cassette Keys 01), copied to a new folder and relinked by ``--fix``:
the relinked device loads and plays, and the same device before relinking
never finishes loading. Swapping one sample's content, the test that proved
Battery reads its paths, has not been run on Kontakt.

Reading them
------------
The state is NI's item container (see
:mod:`~abletoolz.plugins.state.ni_container`), and Maschine's paths sit in it
uncompressed. Inside, strings are framed as a little-endian u32 *character*
count followed by that many UTF-16LE code units, with no terminator::

    5b 00 00 00  43 00 3a 00 5c 00 ...   -> 91 chars, "C:\\Sample Libraries\\..."

That framing is what makes detection exact rather than a text search: a
candidate only counts when the length prefix lands the string's end precisely on
a known suffix. Kit references (``.mxgrp``) and loose audio (``.wav``) both show
up this way.

Rewriting them
--------------
Maschine keeps its project in the same patch frame type as Battery, laid out
differently: after the head, one u32-counted Boost binary archive instead of
records. A sample's path is in it twice.

* **What Maschine loads**: each sampler names its sample as two
  portable-archive strings (a byte count, then that many bytes, UTF-8): the
  path relative to a root, then -- sometimes after a couple of zero bytes --
  the root, a folder ending in ``/``. ``Joey Waronker Drums Samples/Kick_01_
  JoeyWaronker.wav`` under ``D:/Loop Loft/``. A rewrite makes the new file's
  folder the root and its name the relative path, and recomputes the frame's
  u64 and the archive's u32.
* **What it only remembers**: a UTF-16 copy in a **record blob**: a
  portable-archive length, a u32 10, a u64 counting the bytes in use, the
  fields, and leftover bytes up to the length, which is fixed (1032 in every
  blob measured). Changing this copy's length without the count and the
  leftover kept in step crashes Maschine on load; the rewrite moves the count
  and trims or pads the leftover, the way Maschine's own save does, and
  refuses a path that does not fit. Kits (``.mxgrp``) are named only here.

Measured 2026-10-23 with the installed Maschine 2 VST2. In Live, a project
whose UTF-16 copies alone pointed at new files still asked for the old ones by
the root-and-relative pair. In DawDreamer, a custom kit (Loop Loft) relinked
through the pairs -- one sample to its real file, three to generated sine
tones -- loads, saves the new paths back, and its own pattern now plays the
tones: 1320, 1760 and 2200 Hz come out 14 to 43 times stronger than from the
same project unrelinked. Factory samples play even with every stored path
gone, so Maschine finds its own library content without them; custom kits are
what need relinking.
"""

from __future__ import annotations

import dataclasses
import datetime
import pathlib
import re
import urllib.parse

from abletoolz.plugins.identity.ids import Vst2Id, Vst3Id
from abletoolz.plugins.state import ni_container
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.framings import VERBATIM
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import AUDIO_SUFFIXES, FileRef, PluginFormat, PluginMap


@register
def massive() -> PluginMap:
    measured = MeasuredState(StateRung.VERBATIM, (StateEvidence.HOSTED,), datetime.date(2026, 9, 26))
    return PluginMap(
        id="native-instruments.massive",
        name="Massive",
        version="1",
        scope="VST2 and VST3 hold the same NI item container",
        vendor="Native Instruments",
        vst2=PluginFormat(Vst2Id(1315523937), "Massive", VERBATIM, measured),
        vst3=PluginFormat(Vst3Id((1448301646, 1766678893, 1634956137, 1986330624)), "Massive", VERBATIM, measured),
    )


# -- Maschine 2 ------------------------------------------------------------------

CONTAINER_MAGIC = b"hsin"
"""Magic of the outermost chunk, twelve bytes into the state."""

CONTAINER_MAGIC_OFFSET = 12

KIT_SUFFIXES = frozenset({".mxgrp", ".mxsnd"})
"""Maschine's own group and sound files."""

REFERENCE_SUFFIXES = KIT_SUFFIXES | AUDIO_SUFFIXES
"""Kits, and loose audio a user dropped onto a pad."""

MAX_PATH_CHARS = 4096
"""Sanity bound on the length prefix, well past any real Windows path."""

# Where a path can start, in UTF-16LE: a drive letter ("C", NUL, ":", NUL, "\",
# NUL), which is every path the corpus holds, or a leading slash for a set saved
# on macOS. The length prefix is what decides a candidate either way, so the
# second anchor costs nothing and stops a Mac set reading as empty.
_PATH_START = re.compile(rb"[A-Za-z]\x00:\x00\\\x00|/\x00")


def _decode_prefixed_string(raw: bytes, start: int) -> str | None:
    """Decode the UTF-16LE string whose length prefix sits just before ``start``.

    Answers None unless the prefix is present, in range, and describes text that
    decodes cleanly and holds no control characters.
    """
    if start < 4:
        return None
    count = int.from_bytes(raw[start - 4 : start], "little")
    if not 4 <= count <= MAX_PATH_CHARS:
        return None
    end = start + count * 2
    if end > len(raw):
        return None
    try:
        text = raw[start:end].decode("utf-16-le")
    except UnicodeDecodeError:
        return None
    if any(char < " " for char in text):
        return None
    return text


@dataclasses.dataclass(frozen=True, slots=True)
class MaschinePaths:
    """The kits and samples a Maschine 2 project names, in the order they appear.

    A sample is named once, as Maschine loads it (root and relative path
    joined); a kit, which only the UTF-16 copies name, as stored there. A
    rewrite changes the pair and every UTF-16 copy of the same file.
    """

    rewrites: bool = True
    when_empty: str | None = None
    suffixes: frozenset[str] = REFERENCE_SUFFIXES

    def read(self, patch: bytes) -> tuple[FileRef, ...]:
        magic_end = CONTAINER_MAGIC_OFFSET + len(CONTAINER_MAGIC)
        if patch[CONTAINER_MAGIC_OFFSET:magic_end] != CONTAINER_MAGIC:
            return ()
        found: dict[str, FileRef] = {}
        # What Maschine loads: the samplers' root-and-relative pairs.
        for pair in _sampler_refs(patch):
            found.setdefault(_same_file_key(pair.path), FileRef(pair.path, "sample"))
        # What it only remembers: the UTF-16 copies, which name kits nothing else does.
        for match in _PATH_START.finditer(patch):
            text = _decode_prefixed_string(patch, match.start())
            if text is None:
                continue
            suffix = pathlib.PurePath(text.replace("\\", "/")).suffix.lower()
            if suffix not in REFERENCE_SUFFIXES:
                continue
            found.setdefault(_same_file_key(text), FileRef(text, "kit" if suffix in KIT_SUFFIXES else "sample"))
        return tuple(found.values())

    def rewrite(self, patch: bytes, ref: FileRef, path: pathlib.Path) -> bytes:
        if not self.rewrites:
            raise StateTransformError("Maschine 2 references are not relinked from here. Repoint them in Maschine.")
        if not ni_container.is_container(patch):
            raise StateTransformError("This state is not an NI item container")
        target = _maschine_target(path)
        key = _same_file_key(ref.path)
        tree = ni_container.parse(patch)
        changed = 0
        for item in tree.items():
            for frame in item.frame.chain():
                if frame.subtree is not None or (frame.domain, frame.type) != (
                    ni_container.NI_DOMAIN,
                    ni_container.PATCH_TYPE,
                ):
                    continue
                data, pairs = _rewrite_sampler_refs(frame.data, key, target)
                data, copies = _rewrite_utf16_copies(data, key, str(target))
                if pairs or copies:
                    frame.data = _with_archive_lengths(frame.data, data)
                    changed += pairs + copies
        if not changed:
            raise StateTransformError(f"This Maschine project does not point at {ref.path}")
        return ni_container.serialize(tree)


def _same_file_key(path: str) -> str:
    """One spelling for a path whichever separators and case it was stored with."""
    return path.replace("\\", "/").lower()


def _maschine_target(path: pathlib.Path) -> pathlib.PurePath:
    """``path`` spelled as a stored copy the reader finds again: a drive path in backslashes, a Mac path in slashes."""
    windows = pathlib.PureWindowsPath(str(path))
    if re.fullmatch(r"[A-Za-z]:", windows.drive):
        return windows
    posix = pathlib.PurePosixPath(str(path).replace("\\", "/"))
    if posix.is_absolute() and not str(posix).startswith("//"):
        return posix
    raise StateTransformError(f"Maschine stores a path from a drive letter or from /, not {path}")


# -- the samplers' references ------------------------------------------------------

_SAMPLE_NAME = re.compile(rb"\.(?:wav|aif|aiff|flac|mp3|ogg|mp4|rx2)", re.IGNORECASE)
_MAX_GAP = 4


@dataclasses.dataclass(frozen=True, slots=True)
class _SamplerRef:
    relative_at: int
    root_at: int
    root_end: int
    relative: str
    root: str

    @property
    def path(self) -> str:
        return self.root + self.relative


def _portable_string(data: bytes, at: int) -> tuple[int, int] | None:
    """Where the string a portable-archive length at ``at`` counts starts and ends, if one does."""
    width = data[at] if at < len(data) else 0
    if not 1 <= width <= 2:
        return None
    size = int.from_bytes(data[at + 1 : at + 1 + width], "little")
    start = at + 1 + width
    return (start, start + size) if size and start + size <= len(data) else None


def _sampler_refs(data: bytes) -> list[_SamplerRef]:
    refs = []
    for match in _SAMPLE_NAME.finditer(data):
        end = match.end()
        relative_at = next(
            (
                at
                for at in range(end - 3, max(-1, end - 260), -1)
                if (span := _portable_string(data, at)) is not None and span[1] == end
            ),
            None,
        )
        if relative_at is None:
            continue
        cursor = end
        while cursor < end + _MAX_GAP and cursor < len(data) and data[cursor] == 0:
            cursor += 1
        root_span = _portable_string(data, cursor)
        if root_span is None:
            continue
        try:
            start = _portable_string(data, relative_at)
            assert start is not None
            relative = data[start[0] : end].decode("utf-8")
            root = data[root_span[0] : root_span[1]].decode("utf-8")
        except UnicodeDecodeError:
            continue
        if root.endswith("/") and (re.match(r"^[A-Za-z]:/", root) or root.startswith("/")):
            refs.append(_SamplerRef(relative_at, cursor, root_span[1], relative, root))
    return refs


def _portable_bytes(text: str) -> bytes:
    raw = text.encode("utf-8")
    width = 1 if len(raw) < 0x100 else 2
    return bytes([width]) + len(raw).to_bytes(width, "little") + raw


def _rewrite_sampler_refs(data: bytes, key: str, target: pathlib.PurePath) -> tuple[bytes, int]:
    """Point every sampler reference to the file ``key`` names at ``target``: its folder as the root."""
    root = target.parent.as_posix().rstrip("/") + "/"
    out = bytearray()
    cursor = 0
    count = 0
    for ref in _sampler_refs(data):
        if _same_file_key(ref.path) != key:
            continue
        span = _portable_string(data, ref.relative_at)
        assert span is not None
        # The zero bytes between the two strings stay as they were.
        out += data[cursor : ref.relative_at] + _portable_bytes(target.name) + data[span[1] : ref.root_at]
        out += _portable_bytes(root)
        cursor = ref.root_end
        count += 1
    out += data[cursor:]
    return bytes(out), count


def _rewrite_utf16_copies(data: bytes, key: str, target: str) -> tuple[bytes, int]:
    """Point the UTF-16 copies of the path at ``target``, in whichever spelling they were stored."""
    count = 0
    for match in list(_PATH_START.finditer(data)):
        text = _decode_prefixed_string(data, match.start())
        if text is not None and _same_file_key(text) == key and _maschine_string(text) in data:
            data, copies = _replace_in_records(data, _maschine_string(text), _maschine_string(target))
            count += copies
            break
    return data, count


def _with_archive_lengths(old: bytes, data: bytes) -> bytes:
    """``data``, an edit of the Maschine patch frame data ``old``, with its stream and archive lengths recomputed.

    A frame whose two lengths did not already cover it that way is refused: there they are something else.
    """
    if (int.from_bytes(old[12:20], "little"), int.from_bytes(old[20:24], "little")) != (len(old) - 28, len(old) - 32):
        raise StateTransformError("This Maschine patch frame's stream and archive lengths are not the measured layout")
    stream = len(data) - 28
    return data[:12] + stream.to_bytes(8, "little") + (stream - 4).to_bytes(4, "little") + data[24:]


def _maschine_string(text: str) -> bytes:
    """``text`` as Maschine frames a string: a u32 count of UTF-16 code units, then the code units."""
    encoded = text.encode("utf-16-le")
    return (len(encoded) // 2).to_bytes(4, "little") + encoded


_RECORD_KIND = (10).to_bytes(4, "little")
_RECORD_HEAD_BYTES = 12
_RECORD_SEARCH_BYTES = 8192


def _replace_in_records(data: bytes, old: bytes, new: bytes) -> tuple[bytes, int]:
    """Replace ``old`` with ``new`` in every record blob that holds it, each keeping its size; and how many."""
    out = bytearray()
    cursor = 0
    count = 0
    while (at := data.find(old, cursor)) >= 0:
        start, length, used = _enclosing_record(data, at, len(old))
        content = data[start + _RECORD_HEAD_BYTES : start + _RECORD_HEAD_BYTES + used]
        changed = content.replace(old, new)
        spare = length - _RECORD_HEAD_BYTES - len(changed)
        if spare < 0:
            raise StateTransformError("The new path does not fit the record Maschine keeps it in")
        leftover = data[start + _RECORD_HEAD_BYTES + used : start + length]
        delta = len(changed) - len(content)
        leftover = (leftover + bytes(max(0, -delta)))[max(0, delta) :]
        head = _RECORD_KIND + len(changed).to_bytes(8, "little")
        out += data[cursor:start] + head + changed + leftover
        cursor = start + length
        count += content.count(old)
    out += data[cursor:]
    if len(out) != len(data):
        raise StateTransformError("A Maschine record changed size")
    return bytes(out), count


def _enclosing_record(data: bytes, at: int, size: int) -> tuple[int, int, int]:
    """The record blob around ``data[at:at + size]``: where it starts, its fixed length, and the bytes in use."""
    for start in range(at - _RECORD_HEAD_BYTES, max(0, at - _RECORD_SEARCH_BYTES) - 1, -1):
        if data[start : start + 4] != _RECORD_KIND:
            continue
        used = int.from_bytes(data[start + 4 : start + _RECORD_HEAD_BYTES], "little")
        for width in range(1, 5):
            if start - width - 1 < 0 or data[start - width - 1] != width:
                continue
            length = int.from_bytes(data[start - width : start], "little")
            if at + size <= start + _RECORD_HEAD_BYTES + used <= start + length <= len(data):
                return start, length, used
    raise StateTransformError("No record Maschine keeps a path in was found around it")


_MASCHINE_SCOPE = "VST2 project container, references read only"
_MASCHINE_MEASURED = MeasuredState(StateRung.VERBATIM, (StateEvidence.STRUCTURAL,))


@register
def maschine_2() -> PluginMap:
    return PluginMap(
        id="native-instruments.maschine-2",
        name="Maschine 2",
        version="2",
        scope=_MASCHINE_SCOPE,
        vendor="Native Instruments",
        vst2=PluginFormat(Vst2Id(1315523890), "Maschine 2", VERBATIM, _MASCHINE_MEASURED),
        file_paths=MaschinePaths(),
    )


@register
def maschine_2_fx() -> PluginMap:
    # The effect build stores its own UniqueId, so it is a plugin of its own.
    return PluginMap(
        id="native-instruments.maschine-2-fx",
        name="Maschine 2 FX",
        version="2",
        scope=_MASCHINE_SCOPE,
        vendor="Native Instruments",
        vst2=PluginFormat(Vst2Id(1315523900), "Maschine 2 FX", VERBATIM, _MASCHINE_MEASURED),
        file_paths=MaschinePaths(),
    )


# -- Battery 4 and Kontakt -----------------------------------------------------------

BATTERY_SUFFIXES = frozenset({".nbkt"}) | AUDIO_SUFFIXES
"""Battery's kits, and the samples on its cells."""

KONTAKT_SUFFIXES = frozenset({".nki", ".nkm", ".nkb", ".nkr", ".nkc", ".nkx", ".ncw"}) | AUDIO_SUFFIXES
"""Kontakt's instruments, multis, banks, resources and the library files it reads samples from."""

_FILE_KINDS = {
    ".nbkt": "kit",
    ".nki": "instrument",
    ".nkm": "instrument",
    ".nkb": "instrument",
    ".nkr": "resource",
    ".nkc": "resource",
    ".nkx": "resource",
}
"""What a file is to the plugin, where it is not a sample."""


@dataclasses.dataclass(frozen=True, slots=True)
class SegmentPaths:
    """The files a Battery or Kontakt patch names as segment paths, in the order they appear.

    A Kontakt library that encrypts its patch names nothing readable here, and
    is left alone. A rewrite replaces every copy of the old path in the tree, in
    the same frame it was found in, and recompresses the subtree that held it.
    """

    suffixes: frozenset[str]
    rewrites: bool = True
    when_empty: str | None = None

    def read(self, patch: bytes) -> tuple[FileRef, ...]:
        if not ni_container.is_container(patch):
            return ()
        found: dict[str, FileRef] = {}
        for data in _frame_data(ni_container.parse(patch)):
            for stored in ni_container.segment_paths(data):
                suffix = pathlib.PureWindowsPath(stored.path).suffix.lower()
                if suffix in self.suffixes and stored.path not in found:
                    found[stored.path] = FileRef(stored.path, _FILE_KINDS.get(suffix, "sample"))
        return tuple(found.values())

    def rewrite(self, patch: bytes, ref: FileRef, path: pathlib.Path) -> bytes:
        if not ni_container.is_container(patch):
            raise StateTransformError("This state is not an NI item container")
        tree = ni_container.parse(patch)
        old, new = ni_container.encode_segment_path(ref.path), ni_container.encode_segment_path(str(path))
        if not _replace(tree, old, new):
            raise StateTransformError(f"This patch does not point at {ref.path}")
        return ni_container.serialize(tree)


def _frame_data(tree: ni_container.Item) -> list[bytes]:
    """The own data of every frame in ``tree`` that is not a subtree, through subtrees that decode."""
    return [frame.data for item in tree.items() for frame in item.frame.chain() if frame.subtree is None]


def _replace(tree: ni_container.Item, old: bytes, new: bytes) -> int:
    """Replace ``old`` with ``new`` wherever a patch frame holds it, its lengths recomputed; how many copies.

    A path anywhere else sits among lengths nobody has measured, so it is refused rather than spliced.
    """
    changed = 0
    for item in tree.items():
        for frame in item.frame.chain():
            if frame.subtree is not None or old not in frame.data:
                continue
            if (frame.domain, frame.type) != (ni_container.NI_DOMAIN, ni_container.PATCH_TYPE):
                raise StateTransformError(f"A path sits in NI frame type {frame.type:#x}, whose lengths are unmeasured")
            frame.data, count = ni_container.replace_in_patch(frame.data, old, new)
            changed += count
    return changed


_BATTERY_MEASURED = MeasuredState(StateRung.VERBATIM, (StateEvidence.STRUCTURAL,))


@register
def battery_4() -> PluginMap:
    return PluginMap(
        id="native-instruments.battery-4",
        name="Battery 4",
        version="4",
        scope="NI item container; sample and kit paths inside its compressed subtree",
        vendor="Native Instruments",
        vst2=PluginFormat(Vst2Id(1312973108), "Battery 4", VERBATIM, _BATTERY_MEASURED),
        vst3=PluginFormat(
            Vst3Id((1448301646, 1113666658, 1635021925, 1920540724)), "Battery 4", VERBATIM, _BATTERY_MEASURED
        ),
        file_paths=SegmentPaths(BATTERY_SUFFIXES),
    )


_KONTAKT_PATHS = SegmentPaths(KONTAKT_SUFFIXES)


@register
def kontakt_5() -> PluginMap:
    return PluginMap(
        id="native-instruments.kontakt-5",
        name="Kontakt 5",
        version="5",
        scope="NI item container; library paths inside its compressed subtree, unless the library encrypts it",
        vendor="Native Instruments",
        vst2=PluginFormat(Vst2Id(1315524405), "Kontakt 5", VERBATIM, _BATTERY_MEASURED),
        file_paths=_KONTAKT_PATHS,
    )


@register
def kontakt_5_16out() -> PluginMap:
    # The sixteen-output build stores its own UniqueId, so it is a plugin of its own.
    return PluginMap(
        id="native-instruments.kontakt-5-16out",
        name="Kontakt 5 16out",
        version="5",
        scope="NI item container; library paths inside its compressed subtree, unless the library encrypts it",
        vendor="Native Instruments",
        vst2=PluginFormat(Vst2Id(1315524407), "Kontakt 5 16out", VERBATIM, _BATTERY_MEASURED),
        file_paths=_KONTAKT_PATHS,
    )


# -- Reaktor 6 -------------------------------------------------------------------------
#
# A Reaktor state is its snapshot, not its ensemble: the ensemble is read from the .ens file the state names
# in its head, and a state whose .ens is gone loads and plays nothing. The head is "\x01 4RIN", a u32
# version, a zero, "\x01 CSAR", a u32 version, a zero and a u32 form, then the path: form 3 is two bytes and a
# percent-encoded "file:///" URL ending in a NUL (CSAR 5), form 0 a u32 byte count and the plain path (CSAR 6,
# and what Reaktor saves back). No length covers the head, so the path can change length. A UTF-16 copy
# further in is metadata that Reaktor rewrites when it saves. Measured 2026-10-24 over the user's sets: all
# 27 states with an ensemble have one of these heads.
#
# Measured 2026-10-24 in DawDreamer with the installed Reaktor 6 VST2: a Monark state naming a D: drive that
# is gone renders silence; with only the head's URL pointed at the Monark installed here it plays a note
# sweep, and with only the UTF-16 copy changed it stays silent. Relinked from either head form to a renamed
# copy of that ensemble in another folder, it renders the same sweep sample for sample.

ENSEMBLE_SUFFIXES = frozenset({".ens"})
"""Reaktor's ensembles."""

_REAKTOR_MAGIC = b"\x014RIN"
_CSAR_MAGIC = b"\x01CSAR"
_CSAR_AT = 10
_FORM_AT = 20
_URL_FORM = 3
_PLAIN_FORM = 0
_URL_AT = 26
_PLAIN_AT = 24
_FILE_URL = "file:///"


@dataclasses.dataclass(frozen=True, slots=True)
class _EnsembleField:
    start: int
    end: int
    path: str
    url: bool


def _ensemble_field(patch: bytes) -> _EnsembleField | None:
    """Where a Reaktor state's head names its ensemble, and the path it names; None for anything else."""
    if patch[:5] != _REAKTOR_MAGIC or patch[_CSAR_AT : _CSAR_AT + 5] != _CSAR_MAGIC or len(patch) < _URL_AT:
        return None
    form = int.from_bytes(patch[_FORM_AT : _FORM_AT + 4], "little")
    if form == _URL_FORM:
        end = patch.find(b"\x00", _URL_AT)
        url = patch[_URL_AT:end].decode("utf-8", "replace") if end > 0 else ""
        if not url.startswith(_FILE_URL):
            return None
        return _EnsembleField(_URL_AT, end, urllib.parse.unquote(url[len(_FILE_URL) :]), url=True)
    if form == _PLAIN_FORM:
        size = int.from_bytes(patch[_PLAIN_AT : _PLAIN_AT + 4], "little")
        start = _PLAIN_AT + 4
        if not 0 < size <= MAX_PATH_CHARS or start + size > len(patch):
            return None
        return _EnsembleField(start, start + size, patch[start : start + size].decode("utf-8", "replace"), url=False)
    return None


@dataclasses.dataclass(frozen=True, slots=True)
class EnsemblePaths:
    """The ensemble file a Reaktor state is built on."""

    rewrites: bool = True
    when_empty: str | None = None
    suffixes: frozenset[str] = ENSEMBLE_SUFFIXES

    def read(self, patch: bytes) -> tuple[FileRef, ...]:
        field = _ensemble_field(patch)
        if field is None or pathlib.PurePath(field.path).suffix.lower() not in self.suffixes:
            return ()
        return (FileRef(field.path, "ensemble"),)

    def rewrite(self, patch: bytes, ref: FileRef, path: pathlib.Path) -> bytes:
        field = _ensemble_field(patch)
        if field is None or _same_file_key(field.path) != _same_file_key(ref.path):
            raise StateTransformError(f"This Reaktor state does not point at {ref.path}")
        target = str(path).replace("\\", "/")
        if field.url:
            stored = (_FILE_URL + urllib.parse.quote(target, safe="/:")).encode("utf-8")
            return patch[: field.start] + stored + patch[field.end :]
        stored = target.encode("utf-8")
        return patch[:_PLAIN_AT] + len(stored).to_bytes(4, "little") + stored + patch[field.end :]


@register
def reaktor_6() -> PluginMap:
    return PluginMap(
        id="native-instruments.reaktor-6",
        name="Reaktor 6",
        version="6",
        scope="the snapshot of an ensemble, which it names by path in its head",
        vendor="Native Instruments",
        vst2=PluginFormat(Vst2Id(1315525174), "Reaktor 6", VERBATIM, _BATTERY_MEASURED),
        file_paths=EnsemblePaths(),
    )
