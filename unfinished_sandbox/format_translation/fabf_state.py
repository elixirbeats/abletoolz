"""The two shapes a FabFilter patch takes on disk, and how to read and write both.

A FabFilter effect saved into an ``.als`` looks completely different depending on
which plugin format the user dropped on the track, and the difference is not
cosmetic. The VST2 device stores what the VST2 API asks for: a flat array of
normalised floats, 0..1, in parameter-index order, behind a fixed-width preset
name. The VST3 device stores what the plugin itself would write to a preset
file: a little ``FabF`` container whose floats are in the units the plugin
thinks in -- decibels, log2 hertz, enum indices, seconds.

So converting one to the other is not a reframing of bytes. The container has to
be rebuilt *and* every number has to be pushed through the plugin's own
normalised-to-native curve, which nobody outside FabFilter has written down.
This module only does the container half; :mod:`transfer` does the numbers, and
it learns them by asking the plugin rather than guessing.

Layouts, measured on Pro-C 2 on 2026-08-13 and cross-checked against six real
sets:

VST2 ``<Buffer>`` -- 212 bytes for Pro-C 2::

    offset 0    28 bytes    preset name, NUL-padded, not length-prefixed
    offset 28   4*n bytes   float32 little-endian, all in 0..1

That is byte-for-byte the tail of a VST2 ``FxCk`` program chunk, which is why
DawDreamer's ``save_state`` bank can be sliced straight into this shape and back.

VST3 ``<ProcessorState>`` -- 227 bytes for Pro-C 2::

    offset 0    4 bytes     b"FabF"
    offset 4    4 bytes     uint32 version, 2 on every Pro-C 2 state seen
    offset 8    4 bytes     uint32 length of the preset name
    offset 12   n bytes     the preset name, no terminator
    offset 12+n 4 bytes     uint32, 0 on every state seen
    offset 16+n 4 bytes     uint32 parameter count, 46 for Pro-C 2
    offset 20+n 4*c bytes   float32 little-endian, plugin-native units
    then        8 bytes     two uint32, both 1 on every state seen

The two trailing words and the zero word before the count are carried through
rather than invented: the rig reads them off the installed plugin's own default
state and writes back what it found, so a state it builds differs from one the
plugin wrote only where a parameter actually differs.
"""

from __future__ import annotations

import dataclasses
import struct

FABF_MAGIC = b"FabF"
_U32 = struct.Struct("<I")


class StateError(ValueError):
    """A blob that is not the FabFilter container it was claimed to be."""


@dataclasses.dataclass(frozen=True, slots=True)
class LegacyBank:
    """A FabFilter VST2 device's ``<Buffer>``: a name field and normalised floats."""

    name: str
    name_field: int
    values: tuple[float, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class FabfState:
    """A FabFilter VST3 device's ``<ProcessorState>``, taken apart.

    ``leading`` and ``trailing`` are the words the rig does not claim to
    understand. They are kept so a rebuilt state can reproduce them exactly.
    """

    version: int
    name: str
    leading: int
    values: tuple[float, ...]
    trailing: tuple[int, ...]


def parse_legacy(blob: bytes, name_field: int = 28) -> LegacyBank:
    """Read a VST2 ``<Buffer>``. The float count follows from the length."""
    if len(blob) <= name_field:
        raise StateError(f"{len(blob)} bytes is not longer than the {name_field}-byte name field")
    body = blob[name_field:]
    if len(body) % 4:
        raise StateError(f"{len(body)} bytes after the name field is not a whole number of floats")
    count = len(body) // 4
    return LegacyBank(
        name=blob[:name_field].split(b"\x00", 1)[0].decode("latin-1"),
        name_field=name_field,
        values=struct.unpack(f"<{count}f", body),
    )


def build_legacy(bank: LegacyBank) -> bytes:
    """Write a VST2 ``<Buffer>`` back out."""
    name = bank.name.encode("latin-1")
    if len(name) > bank.name_field:
        raise StateError(f"preset name {bank.name!r} does not fit the {bank.name_field}-byte field")
    return name.ljust(bank.name_field, b"\x00") + struct.pack(f"<{len(bank.values)}f", *bank.values)


def parse_fabf(blob: bytes) -> FabfState:
    """Read a VST3 ``ProcessorState``.

    The parameter count is checked against what is actually there rather than
    trusted, because a count that disagrees with the byte length means the
    layout is not the one documented above and every downstream number would be
    read out of the wrong place.
    """
    if len(blob) < 20 or blob[:4] != FABF_MAGIC:
        raise StateError(f"does not start with {FABF_MAGIC!r}: {blob[:8].hex()}")
    (version,) = _U32.unpack_from(blob, 4)
    (name_length,) = _U32.unpack_from(blob, 8)
    if 12 + name_length + 8 > len(blob):
        raise StateError(f"name length {name_length} runs past the end of {len(blob)} bytes")
    name = blob[12 : 12 + name_length].decode("latin-1")
    cursor = 12 + name_length
    (leading,) = _U32.unpack_from(blob, cursor)
    (count,) = _U32.unpack_from(blob, cursor + 4)
    cursor += 8
    if count * 4 > len(blob) - cursor:
        raise StateError(f"declared {count} floats but only {len(blob) - cursor} bytes remain")
    values = struct.unpack_from(f"<{count}f", blob, cursor)
    rest = blob[cursor + count * 4 :]
    if len(rest) % 4:
        raise StateError(f"{len(rest)} trailing bytes is not a whole number of words")
    return FabfState(
        version=version,
        name=name,
        leading=leading,
        values=values,
        trailing=struct.unpack(f"<{len(rest) // 4}I", rest),
    )


def build_fabf(state: FabfState) -> bytes:
    """Write a VST3 ``ProcessorState`` back out."""
    name = state.name.encode("latin-1")
    return b"".join(
        (
            FABF_MAGIC,
            _U32.pack(state.version),
            _U32.pack(len(name)),
            name,
            _U32.pack(state.leading),
            _U32.pack(len(state.values)),
            struct.pack(f"<{len(state.values)}f", *state.values),
            struct.pack(f"<{len(state.trailing)}I", *state.trailing),
        )
    )


# --- the VST2 bank file DawDreamer reads and writes -------------------------
#
# ``save_state``/``load_state`` speak the VST2 FXB format, not the ``.als``
# Buffer, so the rig has to walk one to get at the other. The point of doing so
# is deliverable 1: the floats inside the FXB's single program are the same
# floats an ``.als`` stores, and DawDreamer will say what parameter index each
# one belongs to.

_CHUNK = struct.Struct(">4si4sii")  # CcnK, size, form, version, fxID


@dataclasses.dataclass(frozen=True, slots=True)
class FxbProgram:
    """The one program inside a single-program FXB bank."""

    name: str
    values: tuple[float, ...]
    offset: int


def fxb_form(bank: bytes) -> str:
    """Which of the two VST2 bank shapes this is.

    ``FxBk`` is a bank of parameter floats -- the shape everything downstream
    assumes, and the shape an ``.als`` ``<Buffer>`` mirrors. ``FBCh`` is the
    other legal answer: an opaque chunk the plugin serialises itself, which the
    host cannot index and the derivation cannot bridge, because there are no
    per-parameter floats in it to bridge. Telling them apart is the difference
    between a table that can be derived and one that cannot.
    """
    if len(bank) < 12 or bank[:4] != b"CcnK":
        raise StateError(f"not a VST2 bank: opens with {bank[:8].hex()}")
    return bank[8:12].decode("latin-1")


def fxb_program(bank: bytes) -> FxbProgram:
    """Find the ``FxCk`` program inside an FXB and read its name and floats.

    The bank header is 156 bytes (a 28-byte header and 128 reserved bytes) and
    the program header is 28 more, but the search is done by locating the
    ``FxCk`` form rather than by adding those constants, so a host that pads
    differently is noticed instead of silently misread.
    """
    found = bank.find(b"FxCk")
    if found < 8:
        raise StateError("no FxCk program chunk in this bank")
    start = found - 8
    _, _, form, _, _ = _CHUNK.unpack_from(bank, start)
    if form != b"FxCk":
        raise StateError(f"chunk at {start} is {form!r}, not FxCk")
    (count,) = struct.unpack_from(">i", bank, start + 24)
    body = start + 28
    if body + 28 + count * 4 > len(bank):
        raise StateError(f"program declares {count} parameters but the bank is only {len(bank)} bytes")
    return FxbProgram(
        name=bank[body : body + 28].split(b"\x00", 1)[0].decode("latin-1"),
        values=struct.unpack_from(f">{count}f", bank, body + 28),
        offset=body,
    )


def fxb_with_buffer(bank: bytes, buffer: bytes) -> bytes:
    """The same FXB with an ``.als`` ``<Buffer>`` dropped into its program.

    The two differ only in float byte order -- FXB is big-endian by the VST2
    spec, the ``.als`` Buffer is little-endian as Ableton wrote it -- so this is
    the whole of what it takes to play a set's patch back through DawDreamer.
    """
    program = fxb_program(bank)
    name, values = buffer[:28], buffer[28:]
    if len(values) // 4 != len(program.values):
        raise StateError(f"buffer holds {len(values) // 4} floats, the bank's program holds {len(program.values)}")
    swapped = struct.pack(f">{len(program.values)}f", *struct.unpack(f"<{len(program.values)}f", values))
    return bank[: program.offset] + name + swapped + bank[program.offset + 28 + len(values) :]
