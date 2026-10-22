"""FabFilter's own chunk, its editor state, and the re-encodes between them.

The fourth state rung: nobody migrates the patch for you, so someone has to
parse the source format and write the target's. This is that parser and that
serializer, for Pro-Q 1 and 2 to Pro-Q 3 and Pro-C 2 between host formats.

Two chunk generations
---------------------
The older products -- Pro-Q 1 and 2, Pro-C 1 and 2, Pro-L 1, Pro-R, Pro-G, Pro-MB,
Micro, Saturn 1, Timeless 2, Volcano 2 -- expose no chunk of their own. What an
``.als`` stores for them is therefore the host's own stored-parameter bank,
which is nothing to do with FabFilter and lives in
:mod:`abletoolz.plugins.state.fxbk`; what their VST3 writes is the
``FabF`` container, which only a derived table writes and so lives in
:mod:`abletoolz.plugins.state.derived`.

The current products -- Pro-Q 3, Saturn 2, Timeless 3, Volcano 3 -- expose a real
chunk instead: ``FFBS``, a version, a count, that many float32 in the plugin's
own units, and a tail. That is :class:`FfbsState`. Inside a VST2 chunk the tail
is the editor's state; inside a VST3 ``ProcessorState`` it is a twelve byte
``FFpr`` trailer, which is what ``state.fabfilter`` reframes between.

Why Pro-Q 1 to Pro-Q 3 needs both
---------------------------------
The host rig asked Pro-Q 3 whether it understands a Pro-Q 1 patch on 2026-08-13
and it does not: 6 patches, 0 of 387 parameters moved, no render change. The
formats have nothing in common -- 177 normalized host parameters against 358
plugin-unit floats, seven fields per band against thirteen -- so the only way
across is to read what the Pro-Q 1 bank means and write what Pro-Q 3 stores.

Every number in the remap below was cracked from real instances and then
confirmed in Live 12 on 2026-08-09: a converted set opened, and the bands were
where the Pro-Q 1 had them. Two independent readings agree on the arithmetic --
Pro-Q 1's default band frequency is 0.57519 and Pro-Q 3's is 9.965784, and both
decode to 1 kHz through the formulas here.

What does not survive
---------------------
* **Per-band placement.** Pro-Q 1 stores something other than 1.0 for bands that
  are not stereo and the encoding is still unresolved. Conversion refuses
  those bands; supported stereo bands retain their placement.
* **Automation.** A clip's envelopes point at plugin parameter indices, and the
  two versions do not number their parameters alike.

The preset name used to be on that list, on the strength of a statement that was
measured false: Pro-Q 3 does keep its name in the editor state, and an ``.als``
does carry the editor state -- as the ``ControllerState`` beside the processor
one. Every Pro-Q 3, Saturn 2 and Timeless 3 device in the corpus writes it, name
and all. :class:`FfbsControllerState` builds it, so a converted patch keeps the
name the Pro-Q 1 bank gave it.

"""

from __future__ import annotations

import dataclasses
import enum
import math
import struct
from collections.abc import Sequence

from abletoolz.plugins.state import (
    ConstantControllerState,
    StateTransform,
    StateTransformError,
    register_built_in_state,
    register_controller_state,
    register_custom_state,
)
from abletoolz.plugins.state.derived import DERIVED_TABLES, read_derived_table
from abletoolz.plugins.state.fxbk import LegacyBank

# Length-prefixed fields run through every FabFilter format: a little-endian
# word, then that many bytes.
_U32 = struct.Struct("<I")
_I32 = struct.Struct("<i")


# -- cutting a VST2 chunk in half -------------------------------------------

# The 4CC each FFBS-generation FabFilter product's editor state begins with, read
# off the plugins themselves on 2026-08-13. Inside a VST2 chunk the same magic is
# where the processor's half ends.
_FABFILTER_EDITOR_MAGIC = (b"FV3l", b"FQ3p", b"FQ4p", b"F3Ts", b"FS2a")

# What a FabFilter VST3 puts after its processor state and a VST2 chunk does not.
# Written rather than moved by a re-encode, which has no chunk to cut it off.
FABFILTER_PROCESSOR_TRAILER = b"FFpr\x01\x00\x00\x00\x00\x00\x00\x00"

# The same idea on the controller side: "FFed", a zero word and a float32 1.0.
# Measured across 200 sets -- every Pro-C 2, Pro-L 2, Pro-MB and Pro-R device
# (357 of them) writes exactly these twelve bytes as its whole ControllerState,
# with no variation at all, and every Pro-Q 3, Saturn 2 and Timeless 3 device
# ends its longer one with them. The two fields are carried, not understood.
FABFILTER_CONTROLLER_TRAILER = b"FFed\x00\x00\x00\x00\x00\x00\x80\x3f"


def _fabfilter_state(payload: bytes) -> bytes:
    """Cut an FFBS-generation FabFilter VST2 chunk down to its VST3 processor state.

    A VST2 chunk is the processor's state, then the editor's, and Ableton's
    ``ProcessorState`` is the first half plus a twelve byte trailer. Measured
    2026-08-13: a Volcano 3 VST2 buffer cut here and given the trailer is byte
    for byte a corpus Volcano 3 ``ProcessorState`` of the same patch, and the
    plugin hands those bytes straight back when asked.

    Copying the chunk whole is the trap this replaces. The processor half still
    reaches the DSP, so the device sounds right and passes a listen -- while the
    edit controller never sees the patch and every parameter reads as default.
    """
    for magic in _FABFILTER_EDITOR_MAGIC:
        found = payload.find(magic)
        if found > 0:
            return payload[:found] + FABFILTER_PROCESSOR_TRAILER
    known = ", ".join(magic.decode("ascii") for magic in _FABFILTER_EDITOR_MAGIC)
    raise StateTransformError(
        f"No FabFilter editor section in a {len(payload)} byte chunk (looked for {known}). "
        "The older FabF-generation products -- Pro-C 2, Pro-L 2, Pro-R, Pro-MB, Pro-DS, Pro-G, "
        "Micro and their 1.x predecessors -- write a different chunk that nothing here can convert yet."
    )


register_built_in_state(StateTransform.FABFILTER, _fabfilter_state)


# -- the FFBS chunk ---------------------------------------------------------

FFBS_MAGIC = b"FFBS"
_FFBS_HEADER = struct.Struct("<4sII")


@dataclasses.dataclass(frozen=True, slots=True)
class FfbsState:
    """An FFBS-generation FabFilter state: a version, a float array, a tail.

    The floats are in the plugin's own units -- log2 Hz for a frequency, dB for a
    gain, an integer for an enum -- and are positional, which is exactly why a
    re-encode has to be right rather than plausible. Measured 2026-08-13: a
    FabFilter VST3 handed a correctly headed blob with a random body reads it
    positionally and never asks who wrote it.

    ``tail`` is what follows the floats, and which it is says which side of the
    format boundary the state came from: a VST2 chunk carries the editor's own
    state there, and a VST3 ``ProcessorState`` carries
    :data:`FABFILTER_PROCESSOR_TRAILER`.
    """

    version: int
    parameters: tuple[float, ...]
    tail: bytes

    @classmethod
    def parse(cls, payload: bytes) -> FfbsState:
        """Read an FFBS chunk from either side of the format boundary."""
        if len(payload) < _FFBS_HEADER.size:
            raise StateTransformError(f"only {len(payload)} bytes, too short for an FFBS header")
        magic, version, count = _FFBS_HEADER.unpack_from(payload)
        if magic != FFBS_MAGIC:
            raise StateTransformError(f"a chunk opening {magic!r} is not FFBS")
        end = _FFBS_HEADER.size + count * 4
        if end > len(payload):
            raise StateTransformError(f"an FFBS header claiming {count} floats overruns {len(payload)} bytes")
        return cls(
            version=version,
            parameters=struct.unpack_from(f"<{count}f", payload, _FFBS_HEADER.size),
            tail=payload[end:],
        )

    def encode(self) -> bytes:
        """The bytes this state is stored as."""
        head = _FFBS_HEADER.pack(FFBS_MAGIC, self.version, len(self.parameters))
        return head + struct.pack(f"<{len(self.parameters)}f", *self.parameters) + self.tail


# -- the editor state on the other side of the chunk ------------------------

# The 4CC each FFBS-generation product's editor state opens with. The same
# magics :func:`_fabfilter_state` looks for when it cuts a VST2 chunk in half,
# which is the point: the half it cuts off is this.
PRO_Q3_EDITOR_MAGIC = b"FQ3p"
SATURN_2_EDITOR_MAGIC = b"FS2a"
TIMELESS_3_EDITOR_MAGIC = b"F3Ts"

# The version every editor state measured declares. Nothing to do with the FFBS
# processor version, which is 1.
EDITOR_STATE_VERSION = 3

# The int32 after the preset name. It reads -1 in half the devices measured and
# 0, 1, 2, 3 or 4 in the rest, each non-negative value at most once per product
# in a set -- an index over the instances of that plugin, on the face of it. What
# it indexes is not known, so a device written here says it has none.
NO_INSTANCE = -1

# Introduces the named-controller list: how many pairs follow, then that many
# (name, value) length-prefixed strings. A Timeless 3 device carries its two XY
# pads, two LFOs and two envelope followers here.
_EDITOR_CONTROLLER_MAGIC = b"CuSV"

# Three words -- one before the label, one before the controller magic and one
# after it -- that read 1 in every device measured. Carried rather than
# understood, the way FabF's leading and trailing words are.
_EDITOR_CARRIED: tuple[int, int, int] = (1, 1, 1)


# 4CC, version, and the length of the preset name: the shortest prefix a parse
# can read anything out of.
_EDITOR_HEAD_BYTES = 12
# The instance index, a carried word, and the label's length.
_EDITOR_MIDDLE_BYTES = 12
# A carried word, the controller magic, another carried word, and the count.
_EDITOR_CONTROLLER_HEAD_BYTES = 16


def _need(payload: bytes, cursor: int, count: int, what: str) -> None:
    """Refuse a payload that ends before ``count`` more bytes of ``what``."""
    if cursor + count > len(payload):
        raise StateTransformError(f"{len(payload)} bytes end before an editor state's {what}")


def _read_string(payload: bytes, cursor: int) -> tuple[str, int]:
    """One length-prefixed ASCII string, and where it ends."""
    _need(payload, cursor, _U32.size, "string length")
    (length,) = _U32.unpack_from(payload, cursor)
    cursor += _U32.size
    _need(payload, cursor, length, f"{length} byte string")
    return payload[cursor : cursor + length].decode("latin1"), cursor + length


def _write_string(value: str) -> bytes:
    """The same, the other way."""
    encoded = value.encode("latin1")
    return _U32.pack(len(encoded)) + encoded


@dataclasses.dataclass(frozen=True, slots=True)
class EditorState:
    """An FFBS-generation FabFilter's editor state, which is where the preset name is.

    An ``.als`` holds this as a VST3 device's ``ControllerState`` and a VST2
    chunk holds it after the processor half, and the two are the same layout:

        the product's 4CC, a version, a length-prefixed preset name, an int32
        instance index, a word, a length-prefixed label, a word,
        :data:`_EDITOR_CONTROLLER_MAGIC`, a word, a count, and that many pairs
        of length-prefixed strings.

    The VST3 form is that followed by
    :data:`FABFILTER_CONTROLLER_TRAILER`, which
    is also the whole of what a FabF-generation product writes -- it has no
    editor state to put in front of it.

    ``label`` is a display string Live hands the plugin. It reads as the track
    name in most devices measured and as the whole device chain before the
    plugin in one, and it is empty in others, so a device written here carries
    none rather than inventing one.
    """

    magic: bytes
    version: int
    preset_name: str
    instance_index: int
    label: str
    controllers: tuple[tuple[str, str], ...]
    carried: tuple[int, int, int] = _EDITOR_CARRIED

    @classmethod
    def parse(cls, payload: bytes) -> EditorState:
        """Read an editor state from either side of the format boundary.

        The controller magic is checked rather than skipped: it is the one fixed
        landmark past the two variable-length strings, so a payload that does
        not have it there was misread at the first of them and every string
        after would be nonsense.

        Reading stops at the end of the controller list. A VST2 chunk ends
        there; a VST3 ``ControllerState`` has the trailer after it, and what
        that is belongs to the caller.
        """
        _need(payload, 0, _EDITOR_HEAD_BYTES, "4CC, version and preset name")
        (version,) = _U32.unpack_from(payload, 4)
        preset_name, cursor = _read_string(payload, 8)
        _need(payload, cursor, _EDITOR_MIDDLE_BYTES, "instance index and label")
        (instance_index,) = _I32.unpack_from(payload, cursor)
        (first,) = _U32.unpack_from(payload, cursor + 4)
        label, cursor = _read_string(payload, cursor + 8)
        _need(payload, cursor, _EDITOR_CONTROLLER_HEAD_BYTES, "controller list")
        (second,) = _U32.unpack_from(payload, cursor)
        magic = payload[cursor + 4 : cursor + 8]
        if magic != _EDITOR_CONTROLLER_MAGIC:
            raise StateTransformError(
                f"{magic!r} where {_EDITOR_CONTROLLER_MAGIC!r} should be: this is not an editor state"
            )
        (third,) = _U32.unpack_from(payload, cursor + 8)
        (count,) = _U32.unpack_from(payload, cursor + 12)
        cursor += _EDITOR_CONTROLLER_HEAD_BYTES
        controllers: list[tuple[str, str]] = []
        for _ in range(count):
            name, cursor = _read_string(payload, cursor)
            value, cursor = _read_string(payload, cursor)
            controllers.append((name, value))
        return cls(
            magic=payload[:4],
            version=version,
            preset_name=preset_name,
            instance_index=instance_index,
            label=label,
            controllers=tuple(controllers),
            carried=(first, second, third),
        )

    def encode(self) -> bytes:
        """The bytes this state is stored as, trailer not included."""
        first, second, third = self.carried
        return b"".join(
            (
                self.magic,
                _U32.pack(self.version),
                _write_string(self.preset_name),
                _I32.pack(self.instance_index),
                _U32.pack(first),
                _write_string(self.label),
                _U32.pack(second),
                _EDITOR_CONTROLLER_MAGIC,
                _U32.pack(third),
                _U32.pack(len(self.controllers)),
                *(_write_string(name) + _write_string(value) for name, value in self.controllers),
            )
        )


@dataclasses.dataclass(frozen=True, slots=True)
class FfbsControllerState:
    """The ``ControllerState`` an FFBS-generation FabFilter VST3 wants.

    A :class:`~abletoolz.plugins.state.ControllerState`, so a
    ``ConversionRule`` naming one of these products can declare it. What
    arrives is whatever that device's VST2 saved, and there are two shapes of
    it, told apart by the product's own editor magic rather than by guessing:

    * the same product's VST2 chunk, whose second half *is* this state. It goes
      across whole with the trailer appended -- the mirror of the cut
      ``state: fabfilter`` makes on the first half -- so the preset name, the
      label and every named controller cross unchanged.
    * a legacy bank, which is what an ``.als`` holds for the older products a
      re-encode converts from. There is no editor state in one, so a default one
      is built around the bank's own preset name.
    """

    magic: bytes

    def build(self, source: bytes) -> bytes:
        """The controller bytes for a device whose VST2 saved ``source``."""
        found = source.find(self.magic)
        if found > 0:
            return source[found:] + FABFILTER_CONTROLLER_TRAILER
        editor = EditorState(
            magic=self.magic,
            version=EDITOR_STATE_VERSION,
            preset_name=LegacyBank.parse(source).preset_name,
            instance_index=NO_INSTANCE,
            label="",
            controllers=(),
        )
        return editor.encode() + FABFILTER_CONTROLLER_TRAILER


PRO_Q3_CONTROLLER = FfbsControllerState(PRO_Q3_EDITOR_MAGIC)
SATURN_2_CONTROLLER = FfbsControllerState(SATURN_2_EDITOR_MAGIC)
TIMELESS_3_CONTROLLER = FfbsControllerState(TIMELESS_3_EDITOR_MAGIC)

# What the FabF-generation products -- Pro-C 2, Pro-L 2, Pro-MB, Pro-R and their
# siblings -- write. Their VST2 exposes no chunk, so there is no editor half to
# carry and the trailer is the whole of it.
FABFILTER_CONSTANT_CONTROLLER = ConstantControllerState(FABFILTER_CONTROLLER_TRAILER)

# Reachable from a config entry as ``controller: <name>``. One per product for
# the FFBS generation, because the magic is the product's and the magic is what
# tells a carried editor state from a bank that never had one; one shared name
# for the FabF generation, which has nothing to tell apart.
FABFILTER_FABF_CONTROLLER = "fabfilter-fabf"

register_controller_state(FABFILTER_FABF_CONTROLLER, FABFILTER_CONSTANT_CONTROLLER)
register_controller_state("fabfilter-pro-q-3", PRO_Q3_CONTROLLER)
register_controller_state("fabfilter-saturn-2", SATURN_2_CONTROLLER)
register_controller_state("fabfilter-timeless-3", TIMELESS_3_CONTROLLER)


# -- what a Pro-Q 1 band says -----------------------------------------------


class Q1BandField(enum.IntEnum):
    """The seven slots Pro-Q 1 gives each band, all normalized 0 to 1."""

    FREQUENCY = 0
    GAIN = 1
    Q = 2
    SHAPE = 3
    SLOPE = 4
    PLACEMENT = 5
    ENABLED = 6


PRO_Q1_BANDS = 24
PRO_Q1_BAND_SLOTS = len(Q1BandField)
# One used-band count, then the bands, then eight globals this conversion drops:
# Pro-Q 3 has no counterpart for most of them and defaults the rest.
PRO_Q1_GLOBALS = 8
PRO_Q1_PARAMETERS = 1 + PRO_Q1_BANDS * PRO_Q1_BAND_SLOTS + PRO_Q1_GLOBALS

# Pro-Q 1's frequency knob is logarithmic over its full 10 Hz to 30 kHz range, so
# a normalized 0.57519 is 10 * 3000 ** 0.57519 Hz. Cross-checked against Pro-Q 3,
# which stores the same default as log2 999.99.
_Q1_FREQUENCY_FLOOR_HZ = 10.0
_Q1_FREQUENCY_DECADES = 3000.0

# Gain is linear and centred: 0.5 is 0 dB and the ends are -30 and +30.
_Q1_GAIN_SPAN_DB = 60.0
_Q1_GAIN_CENTRE = 0.5


# -- what a Pro-Q 3 band says -----------------------------------------------


class Q3BandField(enum.IntEnum):
    """The thirteen slots Pro-Q 3 gives each band, in the plugin's own units.

    Five have no Pro-Q 1 counterpart and keep Pro-Q 3's default: the dynamic
    range and the two flags around it are Pro-Q 3's dynamic EQ, which Pro-Q 1
    does not have, and the last two are its per-band phase and solo state.
    """

    USED = 0
    ENABLED = 1
    FREQUENCY = 2
    GAIN = 3
    DYNAMIC_RANGE = 4
    DYNAMIC_AUTO = 5
    DYNAMIC_ENABLED = 6
    Q = 7
    SHAPE = 8
    SLOPE = 9
    PLACEMENT = 10
    PHASE = 11
    SOLO = 12


PRO_Q3_BANDS = 24
PRO_Q3_BAND_SLOTS = len(Q3BandField)
PRO_Q3_GLOBALS = 46
PRO_Q3_PARAMETERS = PRO_Q3_BANDS * PRO_Q3_BAND_SLOTS + PRO_Q3_GLOBALS

# The version an FFBS Pro-Q 3 state declares, in both a VST2 chunk and a VST3
# ProcessorState.
PRO_Q3_STATE_VERSION = 1

# One unused Pro-Q 3 band, read off the installed VST3's own default state on
# 2026-08-13 rather than out of anybody's set: a disabled Bell at 1 kHz, 0 dB,
# Q 0.5, 12 dB/oct, Stereo.
PRO_Q3_DEFAULT_BAND: tuple[float, ...] = (
    0.0,  # USED
    1.0,  # ENABLED
    math.log2(1000.0),  # FREQUENCY
    0.0,  # GAIN
    0.0,  # DYNAMIC_RANGE
    1.0,  # DYNAMIC_AUTO
    1.0,  # DYNAMIC_ENABLED
    0.5,  # Q
    0.0,  # SHAPE
    1.0,  # SLOPE
    2.0,  # PLACEMENT
    1.0,  # PHASE
    0.0,  # SOLO
)

# Pro-Q 3's 46 global slots at their defaults, from the same reading. Pro-Q 1 has
# no counterpart for any of them, so a converted patch keeps these untouched.
PRO_Q3_DEFAULT_GLOBALS: tuple[float, ...] = (
    0.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0,
    1.0, -1.0, 1.0, 2.0, 2.0, 3.0, 0.0, 1.0, 1.0, 2.0,
    0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
    0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
    0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
)  # fmt: skip

PRO_Q3_DEFAULT_PARAMETERS: tuple[float, ...] = PRO_Q3_DEFAULT_BAND * PRO_Q3_BANDS + PRO_Q3_DEFAULT_GLOBALS


# -- and how one becomes the other ------------------------------------------

# The band shapes each version offers, in the order it numbers them. Pro-Q 3
# added four and reordered nothing, so the map is a name lookup rather than a
# table of numbers -- a reordering would fail at import instead of quietly
# turning every low cut into a notch.
_Q1_SHAPES = ("Bell", "Low Shelf", "Low Cut", "High Shelf", "High Cut")
_Q3_SHAPES = (
    "Bell",
    "Low Shelf",
    "Low Cut",
    "High Shelf",
    "High Cut",
    "Notch",
    "Band Pass",
    "Tilt Shelf",
    "Flat Tilt",
)
_Q1_TO_Q3_SHAPE: tuple[int, ...] = tuple(_Q3_SHAPES.index(shape) for shape in _Q1_SHAPES)

# The cut slopes, likewise. Pro-Q 1 offers four of the nine Pro-Q 3 offers, so
# its slope 2 (24 dB/oct) is Pro-Q 3's slope 3, not its slope 2.
_Q1_SLOPES_DB = (6, 12, 24, 48)
_Q3_SLOPES_DB = (6, 12, 18, 24, 30, 36, 48, 72, 96)
_Q1_TO_Q3_SLOPE: tuple[int, ...] = tuple(_Q3_SLOPES_DB.index(slope) for slope in _Q1_SLOPES_DB)

# Pro-Q 3 numbers its placements Left, Right, Stereo, Mid, Side.
_Q3_STEREO_PLACEMENT = 2.0

# Both versions run 10 Hz to 30 kHz and ±30 dB -- Pro-Q 3 says so itself, and its
# floor and both gain ends round-trip exactly. The ceiling is the one place they
# disagree, and only in the last two bits: log2 30000 rounded to float32 is
# 14.872674942016602, and what Pro-Q 3 stores for its own top frequency is this,
# 0.02 Hz lower. Measured by handing it three real patches with a band at the
# top of the range and watching it hand back its own number instead. Clamping
# here is what makes an exact readback mean something -- without it those three
# patches fail the acceptance test over a difference no one could hear.
PRO_Q3_MAX_FREQUENCY_LOG2 = 14.872673988342285


def _enum_index(normalized: float, choices: tuple[int, ...], field: str) -> int:
    """Read a normalized enum slot as one of ``choices``, or refuse it.

    Live stores an enum as the index over its top index, so a five-way shape is
    0, 0.25, 0.5, 0.75 or 1. Rounding recovers the index; a value that rounds
    outside the range is a bank this parser has misread, and continuing would
    write a band nobody asked for.
    """
    index = round(normalized * (len(choices) - 1))
    if not 0 <= index < len(choices):
        raise StateTransformError(f"a Pro-Q 1 {field} of {normalized} is not one of its {len(choices)} settings")
    return choices[index]


def pro_q1_to_pro_q3_parameters(bank: Sequence[float]) -> tuple[float, ...]:
    """Remap Pro-Q 1's normalized parameter bank onto Pro-Q 3's plugin-unit floats.

    Slot 0 of the bank is the used-band count over 24; each band that count
    covers is read out of its seven slots and written into Pro-Q 3's thirteen.
    Bands past the count, and every Pro-Q 3 field Pro-Q 1 has no counterpart for,
    keep :data:`PRO_Q3_DEFAULT_PARAMETERS`.
    """
    if len(bank) != PRO_Q1_PARAMETERS:
        raise StateTransformError(
            f"a Pro-Q 1 bank has {PRO_Q1_PARAMETERS} parameters, not {len(bank)}. "
            "Another FabFilter's bank read as one would land on a patch nobody chose."
        )
    used = round(bank[0] * PRO_Q1_BANDS)
    if not 0 <= used <= PRO_Q1_BANDS:
        raise StateTransformError(f"a Pro-Q 1 bank claiming {used} of {PRO_Q1_BANDS} bands is not readable")

    parameters = list(PRO_Q3_DEFAULT_PARAMETERS)
    for band in range(used):
        start = 1 + band * PRO_Q1_BAND_SLOTS
        source = bank[start : start + PRO_Q1_BAND_SLOTS]
        if source[Q1BandField.PLACEMENT] != 1.0:
            raise StateTransformError("Pro-Q 1 non-stereo band placement is unmapped")
        target = band * PRO_Q3_BAND_SLOTS
        frequency = _Q1_FREQUENCY_FLOOR_HZ * _Q1_FREQUENCY_DECADES ** source[Q1BandField.FREQUENCY]
        parameters[target + Q3BandField.USED] = 1.0
        parameters[target + Q3BandField.ENABLED] = source[Q1BandField.ENABLED]
        parameters[target + Q3BandField.FREQUENCY] = min(math.log2(frequency), PRO_Q3_MAX_FREQUENCY_LOG2)
        parameters[target + Q3BandField.GAIN] = (source[Q1BandField.GAIN] - _Q1_GAIN_CENTRE) * _Q1_GAIN_SPAN_DB
        # Both versions normalize Q the same way, which is the one field that
        # needed no arithmetic and was confirmed in Live along with the rest.
        parameters[target + Q3BandField.Q] = source[Q1BandField.Q]
        parameters[target + Q3BandField.SHAPE] = _enum_index(source[Q1BandField.SHAPE], _Q1_TO_Q3_SHAPE, "shape")
        parameters[target + Q3BandField.SLOPE] = _enum_index(source[Q1BandField.SLOPE], _Q1_TO_Q3_SLOPE, "slope")
        parameters[target + Q3BandField.PLACEMENT] = _Q3_STEREO_PLACEMENT
    return tuple(parameters)


def pro_q1_to_pro_q3(payload: bytes) -> bytes:
    """Re-encode a Pro-Q 1 VST2 bank as the ``ProcessorState`` Pro-Q 3's VST3 wants.

    What a mapping entry reaches with ``state: custom:fabfilter-q1-to-q3``. In
    goes what an ``.als`` holds in a Pro-Q 1 device's ``Buffer``; out comes what
    it should hold in a Pro-Q 3 device's ``ProcessorState``.
    """
    bank = LegacyBank.parse(payload)
    return FfbsState(
        version=PRO_Q3_STATE_VERSION,
        parameters=pro_q1_to_pro_q3_parameters(bank.parameters),
        tail=FABFILTER_PROCESSOR_TRAILER,
    ).encode()


# The name a config entry writes as ``state: custom:<name>``.
PRO_Q1_TO_PRO_Q3 = "fabfilter-q1-to-q3"

register_custom_state(PRO_Q1_TO_PRO_Q3, pro_q1_to_pro_q3)


PRO_Q2_PARAMETERS = 191
PRO_Q2_BAND_SLOTS = 7


def _q2_setting(value: float, count: int) -> int:
    return math.floor(value * (count - 1) + 0.5)


def pro_q2_to_pro_q3_parameters(bank: Sequence[float]) -> tuple[float, ...]:
    """Convert a measured Pro-Q 2 bank, refusing unresolved monitoring modes.

    Source enumeration and sweeps use the recovered native x64 Pro-Q 2 DLL.
    Thirteen wrapped-device buffers restored all 191 source floats unchanged.
    Left/right placement becomes mid/side when the source Channel Mode is on;
    Pro-Q 3 stores that choice per band and for the output pan. The source's
    pan text still says left/right; stereo renders confirm the coupled mode.
    Automation indices need a separate map.
    """
    if len(bank) != PRO_Q2_PARAMETERS:
        raise StateTransformError(f"a Pro-Q 2 bank has {PRO_Q2_PARAMETERS} parameters, not {len(bank)}")
    if any(not math.isfinite(value) or not 0 <= value <= 1 for value in bank):
        raise StateTransformError("Pro-Q 2 parameters must be finite normalized values between 0 and 1")
    for index, name in ((176, "Auto Gain"), (179, "Analyzer Side-chain"), (188, "Solo Band"), (190, "Ex Auto Gain")):
        if bank[index] != 0:
            raise StateTransformError(f"Pro-Q 2 {name} conversion is unresolved; refusing a changed mode")

    parameters = list(PRO_Q3_DEFAULT_PARAMETERS)
    mid_side = _q2_setting(bank[170], 2)
    for band in range(PRO_Q3_BANDS):
        source = bank[band * PRO_Q2_BAND_SLOTS : (band + 1) * PRO_Q2_BAND_SLOTS]
        target = band * PRO_Q3_BAND_SLOTS
        state = _q2_setting(source[0], 3)
        placement = _q2_setting(source[6], 3)
        parameters[target + Q3BandField.USED] = float(state != 2)
        parameters[target + Q3BandField.ENABLED] = float(state != 0)
        parameters[target + Q3BandField.FREQUENCY] = min(
            math.log2(10.0) + source[1] * math.log2(3000.0), PRO_Q3_MAX_FREQUENCY_LOG2
        )
        parameters[target + Q3BandField.GAIN] = 60.0 * source[2] - 30.0
        parameters[target + Q3BandField.Q] = source[3]
        parameters[target + Q3BandField.SHAPE] = float(_q2_setting(source[4], 8))
        parameters[target + Q3BandField.SLOPE] = float(_q2_setting(source[5], 9))
        parameters[target + Q3BandField.PLACEMENT] = float(placement + 3 if mid_side and placement < 2 else placement)

    for source_index, target_index, count in (
        (168, 312, 3),
        (169, 313, 5),
        (174, 318, 2),
        (175, 319, 2),
        (177, 321, 2),
        (178, 322, 2),
        (180, 324, 3),
        (181, 325, 4),
        (182, 326, 5),
        (183, 327, 5),
        (184, 328, 2),
        (185, 330, 2),
        (186, 331, 4),
        (187, 332, 2),
    ):
        parameters[target_index] = float(_q2_setting(bank[source_index], count))
    parameters[314] = bank[171] * 2.0
    parameters[315] = bank[172] * 2.0 - 1.0
    parameters[316] = bank[173] * 2.0 - 1.0
    parameters[317] = float(mid_side)
    parameters[333] = bank[189] * 40.0 - 20.0
    return tuple(parameters)


def pro_q2_to_pro_q3(payload: bytes) -> bytes:
    """Re-encode a Pro-Q 2 VST2 bank as a Pro-Q 3 processor state."""
    bank = LegacyBank.parse(payload)
    return FfbsState(
        version=PRO_Q3_STATE_VERSION,
        parameters=pro_q2_to_pro_q3_parameters(bank.parameters),
        tail=FABFILTER_PROCESSOR_TRAILER,
    ).encode()


PRO_Q2_TO_PRO_Q3 = "fabfilter-q2-to-q3"
register_custom_state(PRO_Q2_TO_PRO_Q3, pro_q2_to_pro_q3)


# Pro-C 2 is the product the derivation was proved on: 46 parameters, every
# state slot owned by exactly one of them, and nine real patches out of the
# user's sets that the installed VST3 read back byte for byte with all 46
# parameters agreeing with the VST2's own reading of the same bank.
PRO_C_2 = "fabfilter-pro-c-2"

PRO_C_2_TABLE = read_derived_table(DERIVED_TABLES / "pro-c-2.json")

register_custom_state(PRO_C_2, PRO_C_2_TABLE.convert)
