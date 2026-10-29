"""FabFilter's own chunk, its editor state, and the re-encodes between them.

The fourth state rung: nobody migrates the patch for you, so someone has to
parse the source format and write the target's. This is that parser and that
serializer, for Pro-Q 1 and 2 to Pro-Q 3, and for Pro-C 2, Pro-L 2, Pro-MB and
Pro-G between host formats.

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
``FFpr`` trailer, which is what :class:`FfbsVst3` reframes between.

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

The preset name survives: Pro-Q 3 keeps it in the editor state, which an
``.als`` carries as the ``ControllerState`` beside the processor one, and every
Pro-Q 3, Saturn 2 and Timeless 3 device in the corpus writes it. A converted
patch carries one with the name the Pro-Q 1 bank gave it, and
:class:`FfbsControllerState` writes it into the VST3's controller half.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
import math
import struct

from abletoolz.plugins.container.controller import ConstantControllerState
from abletoolz.plugins.identity.ids import Vst2Id, Vst3Id
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.derived import DERIVED_TABLES, DerivedTable, read_derived_table
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.framings import VERBATIM
from abletoolz.plugins.state.fxbk import LEGACY_NAME_BYTES
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import Layout, PluginFormat, PluginMap

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

# The same idea on the controller side: "FFed", a zero word and a float32 1.0,
# carried rather than understood. Across 200 sets every Pro-C 2, Pro-L 2, Pro-MB
# and Pro-R device (357 of them) writes exactly these twelve bytes as its whole
# ControllerState, and every Pro-Q 3, Saturn 2 and Timeless 3 device ends its
# longer one with them. Re-measured 2026-10-24: all 568 Pro-L 2, Pro-MB and
# Pro-G VST3 devices in the user's sets, Pro-G's 21 among them, write the same.
FABFILTER_CONTROLLER_TRAILER = b"FFed\x00\x00\x00\x00\x00\x00\x80\x3f"


@dataclasses.dataclass(frozen=True, slots=True)
class FfbsVst3:
    """An FFBS-generation FabFilter VST3: the VST2 chunk's processor half and a trailer.

    The plugin's own patch is its VST2 chunk: the processor's state, then the
    editor's. Ableton's ``ProcessorState`` is the first half plus a twelve byte
    trailer, and the editor half goes to the controller state. Measured
    2026-08-13: a Volcano 3 VST2 buffer cut here and given the trailer is byte
    for byte a corpus Volcano 3 ``ProcessorState`` of the same patch, and the
    plugin hands those bytes straight back when asked.

    Copying the chunk whole is a trap: the processor half still reaches the
    DSP, so the device sounds right and passes a listen, while the edit
    controller never sees the patch and every parameter reads as default.
    Only this direction is measured.
    """

    def lift(self, state: bytes) -> bytes:
        raise StateTransformError("Rebuilding a FabFilter VST2 chunk from its VST3 halves is not measured")

    def lower(self, patch: bytes) -> bytes:
        chunk = FfbsState.parse(patch)
        if not chunk.tail.startswith(_FABFILTER_EDITOR_MAGIC):
            known = ", ".join(magic.decode("ascii") for magic in _FABFILTER_EDITOR_MAGIC)
            raise StateTransformError(f"No FabFilter editor section after the floats (looked for {known})")
        return dataclasses.replace(chunk, tail=FABFILTER_PROCESSOR_TRAILER).encode()


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

# The 4CC each FFBS-generation product's editor state opens with: the magics
# :class:`FfbsVst3` cuts a VST2 chunk at, because the half it cuts off is this.
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


def _write_string(value: str) -> bytes:
    """One length-prefixed latin-1 string."""
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

    The VST3 form is that followed by :data:`FABFILTER_CONTROLLER_TRAILER`,
    which is also the whole of what a FabF-generation product writes -- it has
    no editor state to put in front of it.

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

    Built from the plugin's own patch, its VST2 chunk, whose second half *is*
    this state. It goes across whole with the trailer appended -- the mirror of
    the cut :class:`FfbsVst3` makes on the first half -- so the preset name, the
    label and every named controller cross unchanged. A converter into one of
    these products writes the editor half itself (see
    :func:`default_editor_state`), so there is always one to carry.
    """

    magic: bytes

    def build(self, source: bytes) -> bytes:
        """The controller bytes for the plugin's own patch ``source``."""
        editor = FfbsState.parse(source).tail
        if not editor.startswith(self.magic):
            raise StateTransformError(f"This chunk's editor half does not open with {self.magic!r}")
        return editor + FABFILTER_CONTROLLER_TRAILER


def default_editor_state(magic: bytes, preset_name: str) -> EditorState:
    """The editor state a converted patch carries: its preset name and nothing invented."""
    return EditorState(
        magic=magic,
        version=EDITOR_STATE_VERSION,
        preset_name=preset_name,
        instance_index=NO_INSTANCE,
        label="",
        controllers=(),
    )


PRO_Q3_CONTROLLER = FfbsControllerState(PRO_Q3_EDITOR_MAGIC)
SATURN_2_CONTROLLER = FfbsControllerState(SATURN_2_EDITOR_MAGIC)
TIMELESS_3_CONTROLLER = FfbsControllerState(TIMELESS_3_EDITOR_MAGIC)

# What the FabF-generation products -- Pro-C 2, Pro-L 2, Pro-MB, Pro-R and their
# siblings -- write. Their VST2 exposes no chunk, so there is no editor half to
# carry and the trailer is the whole of it.
FABFILTER_CONSTANT_CONTROLLER = ConstantControllerState(FABFILTER_CONTROLLER_TRAILER)


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

# One used-band count, then the bands, then eight globals. The original x86 Pro-Q 1
# (vendor version 1040, SHA256 3a449f58...) was hosted on 2026-10-28 and named its
# 177 parameters, the bands in Q1BandField's order. Each global was swept over 33
# points and printed:
#   169 Output Gain     -inf, -41.81, -29.76, -18.00 at 0.25, then 72 dB per unit
#                       to +36.00 -- Pro-Q 2's Output Level, point for point
#   170 Output Pan      -1.000 to 1.000, linear
#   171 Display Range   6, 12, 30 dB
#   172 Process Mode    Zero Latency, Linear Phase - Low / Medium / High / Maximum Latency
#   173 Channel Mode    Left/Right, Mid/Side
#   174 Bypass          Not Bypassed, Bypassed
#   175 Receive Midi    Enabled, Disabled
#   176 Analyzer        Off, Pre-EQ, Post-EQ
# Each enum switches where index over top index rounds half up. Pro-Q 1's pan law
# is not printed, only the position. Across the collection (1,578 devices, 138
# distinct banks) the pan, bypass and MIDI switch never move, Display Range is
# 12 or 30 dB, and 4 banks use linear phase. The defaults below are what the
# hosted plugin's own default program holds, and so do the collection's untouched
# banks: 0 dB, centre, 12 dB, Zero Latency, Left/Right, on, MIDI, Post-EQ.
PRO_Q1_DEFAULT_GLOBALS: tuple[float, ...] = (0.5, 0.5, 0.5, 0.0, 0.0, 0.0, 0.0, 1.0)
PRO_Q1_GLOBALS = len(PRO_Q1_DEFAULT_GLOBALS)
PRO_Q1_PARAMETERS = 1 + PRO_Q1_BANDS * PRO_Q1_BAND_SLOTS + PRO_Q1_GLOBALS

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

# Pro-Q 3's 46 global slots at their defaults, from the same reading. A converted
# patch keeps these wherever Pro-Q 1 has no global to put there.
PRO_Q3_DEFAULT_GLOBALS: tuple[float, ...] = (
    0.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0,
    1.0, -1.0, 1.0, 2.0, 2.0, 3.0, 0.0, 1.0, 1.0, 2.0,
    0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
    0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
    0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
)  # fmt: skip

PRO_Q3_DEFAULT_PARAMETERS: tuple[float, ...] = PRO_Q3_DEFAULT_BAND * PRO_Q3_BANDS + PRO_Q3_DEFAULT_GLOBALS


# Pro-Q 2's host bank: seven slots for each of 24 bands, then its globals.
PRO_Q2_PARAMETERS = 191
PRO_Q2_BAND_SLOTS = 7


# -- the plugins -------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class DerivedFabF:
    """A FabF-generation VST3: the plugin's own VST2 host bank, re-encoded through its derived table.

    Only the way from the bank is measured: the table turns normalized bank
    floats into the plugin's units, and nothing derives the inverse yet.
    """

    table: DerivedTable

    def lift(self, state: bytes) -> bytes:
        raise StateTransformError(f"Rebuilding a {self.table.product} VST2 bank from its VST3 state is not measured")

    def lower(self, patch: bytes) -> bytes:
        return self.table.convert(patch)


_HOSTED_ON = datetime.date(2026, 8, 13)


def _ffbs_version_1(patch: bytes) -> bool:
    return len(patch) >= _FFBS_HEADER.size and patch[:4] == FFBS_MAGIC and _U32.unpack_from(patch, 4)[0] == 1


def _bank_of(count: int) -> Layout:
    """A host bank of ``count`` parameters: the preset name, then one float each."""
    return Layout(f"{count}-parameter host bank", lambda patch: len(patch) == LEGACY_NAME_BYTES + 4 * count)


# Every FFBS-generation VST2 patch in the user's sets is version 1.
_FFBS_LAYOUTS = (Layout("FFBS version 1", _ffbs_version_1),)


def _ffbs_plugin(
    key: str,
    vst2_name: str,
    vst2_id: int,
    vst3_name: str,
    vst3_id: tuple[int, int, int, int],
    measured: MeasuredState,
    controller: FfbsControllerState | None,
    *,
    vst2_plan: str | None = None,
    vst3_plan: str | None = None,
) -> PluginMap:
    """An FFBS-generation FabFilter: VST2 holds its own chunk, VST3 the reframed halves."""
    return PluginMap(
        id=f"fabfilter.{key}",
        name=vst2_name,
        version=None,
        scope="VST2 chunk to VST3 processor and controller halves, measured in the host rig",
        vendor="FabFilter",
        vst2=None
        if vst2_plan is None
        else PluginFormat(Vst2Id(vst2_id), vst2_name, VERBATIM, measured, plan_name=vst2_plan),
        vst3=None
        if vst3_plan is None
        else PluginFormat(Vst3Id(vst3_id), vst3_name, FfbsVst3(), measured, controller, plan_name=vst3_plan),
        layouts=_FFBS_LAYOUTS,
    )


_REFRAMED = MeasuredState(StateRung.REFRAME, (StateEvidence.HOSTED,), _HOSTED_ON)


@register
def pro_q_3() -> PluginMap:
    return _ffbs_plugin(
        "pro-q-3",
        "FabFilter Pro-Q 3",
        1179726704,
        "Pro-Q 3",
        (1925503857, 2051884442, -1182903948, 1568977821),
        _REFRAMED,
        PRO_Q3_CONTROLLER,
        vst2_plan="pro_q_3_vst2",
        vst3_plan="pro_q_3_vst3",
    )


@register
def saturn_2() -> PluginMap:
    return _ffbs_plugin(
        "saturn-2",
        "FabFilter Saturn 2",
        1179857505,
        "Saturn 2",
        (-1928956621, -660584163, -1163695098, 1282063355),
        _REFRAMED,
        SATURN_2_CONTROLLER,
        vst2_plan="saturn_2_vst2",
        vst3_plan="saturn_2_vst3",
    )


@register
def timeless_3() -> PluginMap:
    # Heard on 2026-08-10 and re-measured in the host rig on 2026-08-13.
    return _ffbs_plugin(
        "timeless-3",
        "FabFilter Timeless 3",
        1177769075,
        "Timeless 3",
        (-756127758, -984465363, -1876879981, 452832875),
        MeasuredState(StateRung.REFRAME, (StateEvidence.EAR, StateEvidence.HOSTED), _HOSTED_ON),
        TIMELESS_3_CONTROLLER,
        vst2_plan="timeless_3_vst2",
        vst3_plan="timeless_3_vst3",
    )


@register
def volcano_3() -> PluginMap:
    # The processor half is measured; Volcano 3's controller half is not, so its
    # VST3 is refused as a target until someone measures it.
    return _ffbs_plugin(
        "volcano-3",
        "FabFilter Volcano 3",
        1180054380,
        "Volcano 3",
        (698034028, -495235247, -1785296369, -2046760495),
        _REFRAMED,
        None,
        vst2_plan="volcano_3_vst2",
        vst3_plan="volcano_3_vst3",
    )


def _host_bank_plugin(key: str, name: str, unique_id: int, parameters: int, plan_name: str) -> PluginMap:
    """An older FabFilter whose VST2 exposes no chunk, so a set holds the host's bank."""
    return PluginMap(
        id=f"fabfilter.{key}",
        name=name,
        version=None,
        scope=f"VST2 host bank of {parameters} parameters, read in the host rig",
        vendor="FabFilter",
        vst2=PluginFormat(Vst2Id(unique_id), name, VERBATIM, _BANK_READ, plan_name=plan_name),
        layouts=(_bank_of(parameters),),
    )


# The host rig read real banks of both back out of their own VST2.
_BANK_READ = MeasuredState(StateRung.VERBATIM, (StateEvidence.HOSTED,), _HOSTED_ON)


@register
def pro_q_1() -> PluginMap:
    # "FabFilter Pro-Q.64", a jBridged 32-bit build, stores the same id.
    return _host_bank_plugin("pro-q-1", "FabFilter Pro-Q", 1179668850, PRO_Q1_PARAMETERS, "pro_q_vst2")


@register
def pro_q_2() -> PluginMap:
    # So do "FabFilter Pro-Q 2 x64" and the jBridged ".64" builds.
    return _host_bank_plugin("pro-q-2", "FabFilter Pro-Q 2", 1179726448, PRO_Q2_PARAMETERS, "pro_q_2_vst2")


def _derived_plugin(
    key: str,
    name: str,
    vst2_id: int,
    vst3_name: str,
    vst3_id: tuple[int, int, int, int],
    table: DerivedTable,
    measured: MeasuredState,
    *,
    vst2_plan: str | None = None,
    vst3_plan: str | None = None,
) -> PluginMap:
    """A FabF-generation FabFilter: the VST2 holds the host's bank, the VST3 what the derived table writes."""
    vst2 = None if vst2_plan is None else PluginFormat(Vst2Id(vst2_id), name, VERBATIM, measured, plan_name=vst2_plan)
    vst3 = (
        None
        if vst3_plan is None
        else PluginFormat(
            Vst3Id(vst3_id), vst3_name, DerivedFabF(table), measured, FABFILTER_CONSTANT_CONTROLLER, plan_name=vst3_plan
        )
    )
    return PluginMap(
        id=f"fabfilter.{key}",
        name=name,
        version=None,
        scope="VST2 host bank to VST3 FabF state through the derived table",
        vendor="FabFilter",
        vst2=vst2,
        vst3=vst3,
        layouts=(_bank_of(len(table.parameters)),),
    )


# Pro-C 2 is the product the derivation was proved on: 46 parameters, every
# state slot owned by exactly one of them, and nine real patches out of the
# user's sets that the installed VST3 read back byte for byte with all 46
# parameters agreeing with the VST2's own reading of the same bank.
PRO_C_2_TABLE = read_derived_table(DERIVED_TABLES / "pro-c-2.json")


@register
def pro_c_2() -> PluginMap:
    return _derived_plugin(
        "pro-c-2",
        "FabFilter Pro-C 2",
        1178808944,
        "Pro-C 2",
        (2046039523, -924366841, -1386372145, 1881470488),
        PRO_C_2_TABLE,
        MeasuredState(StateRung.RE_ENCODE, (StateEvidence.HOSTED,), _HOSTED_ON),
        vst2_plan="pro_c_2_vst2",
        vst3_plan="pro_c_2_vst3",
    )


# Pro-L 2, Pro-MB and Pro-G had their tables derived on 2026-08-13 and were
# checked the way Pro-C 2 was on 2026-10-24: the user's own patches (9 Pro-L 2,
# 12 Pro-MB, 12 Pro-G) converted here, then read by both installed plugins,
# agree on every parameter, and each VST3 handed the converted state back byte
# for byte. Older builds of Pro-L 2 and Pro-MB saved one parameter fewer (31 and
# 150); their banks fit no layout here and are refused rather than shifted.
_DERIVED_CHECKED = MeasuredState(StateRung.RE_ENCODE, (StateEvidence.HOSTED,), datetime.date(2026, 10, 24))

PRO_L_2_TABLE = read_derived_table(DERIVED_TABLES / "pro-l-2.json")
PRO_MB_TABLE = read_derived_table(DERIVED_TABLES / "pro-mb.json")

# Pro-G's VST3 keeps one style slot where its VST2 has two knobs: "Style"
# (Classic to Upward, 0 to 4) and "Ex Style", whose Ducking writes 5 over it.
# A patch with Ducking on loses the Style underneath, which only the VST2 kept.
PRO_G_TABLE = read_derived_table(DERIVED_TABLES / "pro-g.json")


@register
def pro_l_2() -> PluginMap:
    return _derived_plugin(
        "pro-l-2",
        "FabFilter Pro-L 2",
        1179398768,
        "Pro-L 2",
        (-1344721038, -1710995529, -1243033131, 1759422853),
        PRO_L_2_TABLE,
        _DERIVED_CHECKED,
        vst2_plan="pro_l_2_vst2",
        vst3_plan="pro_l_2_vst3",
    )


@register
def pro_mb() -> PluginMap:
    # "FabFilter Pro-MB x64" and the jBridged ".64" builds store the same id.
    return _derived_plugin(
        "pro-mb",
        "FabFilter Pro-MB",
        1179667810,
        "Pro-MB",
        (-1011449534, -946321678, -2101920597, -552365962),
        PRO_MB_TABLE,
        _DERIVED_CHECKED,
        vst2_plan="pro_mb_vst2",
        vst3_plan="pro_mb_vst3",
    )


@register
def pro_g() -> PluginMap:
    # So do "FabFilter Pro-G x64" and its ".64" builds.
    return _derived_plugin(
        "pro-g",
        "FabFilter Pro-G",
        1179666290,
        "Pro-G",
        (-1658991369, -1853209745, -1546791151, 1569397051),
        PRO_G_TABLE,
        _DERIVED_CHECKED,
        vst2_plan="pro_g_vst2",
        vst3_plan="pro_g_vst3",
    )


# Pro-DS's table is derived and checked the way the three above are. Real
# Pro-DS banks converted here load into the installed VST3 with all 20
# parameters agreeing with what the VST2 says the bank means, and come back
# byte for byte or with the low-pass frequency one float32 step (0.8450983
# against 0.8450984) from what was written, which the plugin rounds to its own
# value. Micro's table is not shipped: a saved Micro bank holds 8 parameters
# where the installed build has 9.
PRO_DS_TABLE = read_derived_table(DERIVED_TABLES / "pro-ds.json")


@register
def pro_ds() -> PluginMap:
    return _derived_plugin(
        "pro-ds",
        "FabFilter Pro-DS",
        1179669362,
        "Pro-DS",
        (1508058320, -1897393695, -1202224895, -1142580456),
        PRO_DS_TABLE,
        MeasuredState(StateRung.RE_ENCODE, (StateEvidence.HOSTED,), datetime.date(2026, 10, 26)),
        vst2_plan="pro_ds_vst2",
        vst3_plan="pro_ds_vst3",
    )
