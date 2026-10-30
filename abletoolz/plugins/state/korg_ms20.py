"""KORG MS-20 (KORG Legacy Collection): its bank, read and edited by control name in the plugin's own units.

MS-20 is a 32-bit VST2 only; a jBridge wrapper saves it as ``MS-20.64`` with the
same UniqueId and the same state. Live stores its whole bank, 71,696 bytes:

    ``bolg`` and 2,044 bytes of global settings (its MIDI control map), carried as they are
    32 programs of 2,048 bytes
    the selected program (u32), a u32, 8 zero bytes
    the working program -- what the plugin plays and its getters report -- then a second copy

A bank a fresh instance hands out (zeros at bytes 9 to 11 of its global block,
where every bank a set holds carries ``04 02 01``) restored as nothing when
handed back; edits built on a bank from a set restore.

A program is ``prog``, a u32 2,040 (the bytes after it), a u32 version, a u32 0,
the name in 32 bytes, 84 float32 slots, then 17 cable records of four bytes
(one jack, the other jack, the cable's colour, 0; ``FF FF 00 00`` is no cable),
then zeros. Every count, length and magic is checked; the values of a program
are read only from the measured version, ``00 04 02 01``.

The controls were measured by hosting the plugin itself -- ``MS-20.dll``, x86,
UniqueId ``KLMV`` -- without an editor or audio. It reports 96 controls. Of the
ones its own setter moves, each was set at 255 positions and read back with the
program it saved; each stored slot was also written at 21 values into a program,
restored, and every control read. That ties all 84 slots to their controls. The
plugin names 47 of them; the rest carry the names in the plugin's own string
table, which lists them in control order and agrees with what each prints
(sources for the two external modulations, Mono/Poly, Single/Multi, 1 to 32
voices, -24 to +24 semitones). Six of the external modulation amounts are reset
to their centre by the plugin when a program restores, and two input switches
read Off whatever is written; those are kept as raw slots, unmeasured.

A choice slot does not decode by rounding: restores at 361 values put each
item's boundaries between two measured points, and a value is read by those
(one inside a boundary's uncertainty is refused). Each item is written as the
value the plugin's own getter reports for it, which restores to that item.

The patch panel's 35 jacks are numbered in the order the plugin's string table
lists them, ``TOTAL IN`` 0 to ``ESP TRIG OUT`` 34; under that numbering every
cable stored in real sets' programs joins one output to one input.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import math
import pathlib
import struct
from collections.abc import Mapping, Sequence

from abletoolz.formats.ansi import decode_ansi
from abletoolz.plugins.identity.ids import Vst2Id
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.framings import VERBATIM
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import Layout, PluginFormat, PluginMap

MS20_VST2 = Vst2Id(0x4B4C4D56)
MS20_SHA256 = "13c7ffb5e3183e472de0f46ae9d0d2e4141ed7fdd3eeefa63667ef466489cf46"

PROGRAM_BYTES = 0x800
PROGRAMS = 32
SLOTS = 84
CABLES = 17
_PROGRAM_MAGIC = b"prog" + struct.pack("<I", PROGRAM_BYTES - 8)
VERSION = bytes.fromhex("00040201")
_NAME = slice(0x10, 0x30)
_SLOTS_AT = 0x30
_CABLES_AT = 0x180
_CABLES_END = _CABLES_AT + 4 * CABLES
_NO_CABLE = 0xFF
_BANK_MAGIC = b"bolg"
_SELECTED = 0x800 + PROGRAMS * PROGRAM_BYTES
WORKING = _SELECTED + 16
BANK_BYTES = WORKING + 2 * PROGRAM_BYTES
_F32 = struct.Struct("<f")
DATA = pathlib.Path(__file__).with_name("data")

type Ms20Value = int | float | str

JACKS = (
    "TOTAL IN",
    "MG TRI OUT",
    "SAMPLE HOLD IN",
    "ESP SIGNAL IN",
    "SAMPLE HOLD CLOCK IN",
    "VCO FREQ IN",
    "MG PW OUT",
    "SAMPLE HOLD OUT",
    "ESP AMP OUT",
    "EXT SIGNAL IN",
    "EG1 OUT",
    "EG1 REV OUT",
    "VCA IN",
    "HPF CUTOFF IN",
    "VCA CONTROL IN",
    "ESP BPF OUT",
    "VCA OUT",
    "LPF CUTOFF IN",
    "EG1 TRIG IN",
    "PINK NOISE OUT",
    "ESP FV CONV CV OUT",
    "VCA INIGAIN IN",
    "EG2 REV OUT",
    "WHITE NOISE OUT",
    "VCO2 CV IN",
    "VCO CV IN",
    "KBD TRIG IN",
    "WHEEL OUT",
    "ESP ENV OUT",
    "SIGNAL OUT",
    "PHONES OUT",
    "KBD CV OUT",
    "KBD TRIG OUT",
    "MOMENTARY SW OUT",
    "ESP TRIG OUT",
)


# -- how a slot's 0 to 1 reads in the plugin's units ---------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class Line:
    """A straight line from ``low`` at 0 to ``high`` at 1; the plugin prints it to two decimals."""

    low: float
    high: float
    unit: str

    def value(self, normalized: float) -> float:
        return self.low + (self.high - self.low) * normalized

    def normalized(self, value: Ms20Value) -> float:
        if type(value) not in (int, float) or not math.isfinite(value):  # type: ignore[arg-type]
            raise StateTransformError(f"{value!r} is not a finite number")
        normalized = (float(value) - self.low) / (self.high - self.low)
        if not -1e-9 <= normalized <= 1 + 1e-9:
            raise StateTransformError(f"{value} {self.unit} is outside {self.low:g} to {self.high:g}")
        return min(max(normalized, 0.0), 1.0)


@dataclasses.dataclass(frozen=True, slots=True)
class Choice:
    """Items, each with the value the plugin's getter reports for it, and the measured boundaries between them.

    ``cuts[k]`` is the stretch inside which item ``k`` gives way to item ``k + 1``:
    a value at or above its top reads past it, one at or below its bottom does
    not, and one strictly inside was not resolved and is refused.
    """

    items: tuple[tuple[float, str | int], ...]
    cuts: tuple[tuple[float, float], ...]

    def value(self, normalized: float) -> str | int:
        item = 0
        for low, high in self.cuts:
            if normalized >= high:
                item += 1
            elif normalized > low:
                raise StateTransformError(f"{normalized} lies between two MS-20 items, where no restore was measured")
        return self.items[item][1]

    def normalized(self, value: Ms20Value) -> float:
        for stored, label in self.items:
            if label == value and type(label) is type(value):
                return stored
        raise StateTransformError(f"this MS-20 control has no setting {value!r}")


type Curve = Line | Choice


KNOB = Line(0.0, 10.0, "")
BIPOLAR = Line(-5.0, 5.0, "")


@dataclasses.dataclass(frozen=True, slots=True)
class Control:
    """One stored slot: the plugin's control index, its name, and how its value reads."""

    slot: int
    index: int
    name: str
    curve: Curve


# Slot, control index, name, and its curve: a line, or CHOICE for the measured items in ``data/korg-ms-20.json``.
CHOICE = None
_TABLE: tuple[tuple[int, int, str, Line | None], ...] = (
    (0, 0, "VCO 1 Wave Form", CHOICE),
    (1, 1, "VCO 1 Pulse Width", KNOB),
    (2, 2, "VCO 1 Scale", CHOICE),
    (3, 3, "Portamento Time", KNOB),
    (4, 4, "VCO 2 Wave Form", CHOICE),
    (5, 5, "VCO 2 Tune", Line(-12.0, 12.0, "")),
    (6, 6, "VCO 2 Scale", CHOICE),
    (7, 7, "Fine Tune", BIPOLAR),
    (8, 8, "VCO 1 Level", KNOB),
    (9, 9, "VCO 2 Level", KNOB),
    (10, 10, "Pitch MG/T.Ext", KNOB),
    (11, 11, "Pitch EG 1/Ext", KNOB),
    (12, 12, "HPF Cutoff", KNOB),
    (13, 13, "HPF Peak", KNOB),
    (14, 14, "HPF MG/T.Ext", KNOB),
    (15, 15, "HPF EG 2/Ext", KNOB),
    (16, 16, "LPF Cutoff", KNOB),
    (17, 17, "LPF Peak", KNOB),
    (18, 18, "LPF MG/T.Ext", KNOB),
    (19, 19, "LPF EG 2/Ext", KNOB),
    (20, 20, "MG Wave Form", KNOB),
    (21, 21, "MG Frequency", KNOB),
    (22, 22, "EG 1 Delay", KNOB),
    (23, 23, "EG 1 Attack", KNOB),
    (24, 24, "EG 1 Release", KNOB),
    (25, 25, "EG 2 Hold", KNOB),
    (26, 26, "EG 2 Attack", KNOB),
    (27, 27, "EG 2 Decay", KNOB),
    (28, 28, "EG 2 Sustain", KNOB),
    (29, 29, "EG 2 Release", KNOB),
    (30, 30, "ESP Signal Level", KNOB),
    (31, 31, "ESP Low Cut", KNOB),
    (32, 32, "ESP High Cut", KNOB),
    (33, 33, "ESP CV Adjust", KNOB),
    (34, 34, "ESP Threshold", KNOB),
    (35, 35, "Volume", KNOB),
    (36, 36, "Wheel", KNOB),
    (37, 37, "Momentary Switch", CHOICE),
    (39, 47, "Unison Detune", KNOB),
    (40, 48, "Poly Voices", CHOICE),
    (42, 50, "Transpose", CHOICE),
    (43, 51, "Pitch Bend Range", Line(0.0, 12.0, "")),
    (44, 52, "Analog", KNOB),
    (45, 53, "Control 53", CHOICE),
    (46, 54, "Control 54", Line(20.0, 400.0, "")),
    (47, 55, "Pan", BIPOLAR),
    (48, 56, "MG Tempo Sync", CHOICE),
    (49, 57, "MG Key Sync", CHOICE),
    (50, 58, "MG Base Note", CHOICE),
    (51, 59, "MG Times", CHOICE),
    (52, 60, "Control 60", CHOICE),
    (53, 61, "Control 61", CHOICE),
    (55, 63, "Control 63", CHOICE),
    (56, 64, "ExtMod1 > VCO1 PW", BIPOLAR),
    (57, 65, "ExtMod1 > VCO2 Pitch", BIPOLAR),
    (59, 67, "ExtMod1 > HPF Cutoff", BIPOLAR),
    (61, 69, "ExtMod1 > LPF Cutoff", BIPOLAR),
    (63, 71, "ExtMod1 > Amp", BIPOLAR),
    (66, 74, "ExtMod2 > VCO2 Pitch", BIPOLAR),
    (67, 75, "ExtMod2 > FM MG/T.Ext", BIPOLAR),
    (68, 76, "ExtMod2 > HPF Cutoff", BIPOLAR),
    (69, 77, "ExtMod2 > HPF MG/T.Ext", BIPOLAR),
    (70, 78, "ExtMod2 > LPF Cutoff", BIPOLAR),
    (72, 44, "Poly Mode", CHOICE),
    (73, 45, "Trigger Mode", CHOICE),
    (74, 42, "EXT.Mod.Src.1", CHOICE),
    (75, 43, "EXT.Mod.Src.2", CHOICE),
    (76, 41, "Unison Spread", KNOB),
    (79, 82, "Ext Audio Input Gain", Line(-48.0, 12.0, "dB")),
    (80, 83, "Ext Audio Output Gain", Line(-48.0, 12.0, "dB")),
)


def _choices() -> dict[int, Choice]:
    data = json.loads((DATA / "korg-ms-20.json").read_text(encoding="utf-8"))["choices"]
    return {
        int(slot): Choice(
            tuple((float(value), label) for value, label in entry["items"]),
            tuple((float(low), float(high)) for low, high in entry["cuts"]),
        )
        for slot, entry in data.items()
    }


_CHOICES = _choices()
CONTROLS = tuple(Control(slot, index, name, curve or _CHOICES[slot]) for slot, index, name, curve in _TABLE)
CONTROL_BY_NAME = {control.name: control for control in CONTROLS}

# Slots held but not measured: the plugin resets or ignores what is written there on restore.
UNMEASURED = {
    41: "Unison Voices",
    54: "Control 62",
    58: "ExtMod1 > FM MG/T.Ext",
    60: "ExtMod1 > HPF MG/T.Ext",
    62: "ExtMod1 > LPF MG/T.Ext",
    64: "ExtMod2 > VCO1 PW",
    65: "ExtMod2 > VCO2 Pitch",
    71: "ExtMod2 > Amp",
    77: "Ext Audio In",
    78: "ESP Audio In",
}
# Slots a restore leaves without effect on any control.
UNUSED = (38, 81, 82, 83)


# -- the patch panel -------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class Cable:
    """A patch cable from an output jack to an input jack, and the colour it is drawn in."""

    out: str
    to: str
    colour: int = 0

    def __post_init__(self) -> None:
        if not (self.out in JACKS and self.out.endswith(" OUT")):
            raise StateTransformError(f"{self.out!r} is not an MS-20 output jack")
        if not (self.to in JACKS and self.to.endswith(" IN")):
            raise StateTransformError(f"{self.to!r} is not an MS-20 input jack")
        if not 0 <= self.colour <= 255:
            raise StateTransformError(f"a cable colour is one byte, not {self.colour}")


def _cable(record: bytes) -> Cable | None:
    first, second, colour, zero = record
    if (first, second) == (_NO_CABLE, _NO_CABLE):
        if (colour, zero) != (0, 0):
            raise StateTransformError("an empty MS-20 cable record holds bytes after its jacks")
        return None
    if first >= len(JACKS) or second >= len(JACKS) or zero:
        raise StateTransformError(f"an MS-20 cable record {record.hex()} names no measured jacks")
    ends = sorted((JACKS[first], JACKS[second]), key=lambda jack: not jack.endswith(" OUT"))
    return Cable(ends[0], ends[1], colour)


# -- a program -------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class Ms20Program:
    """One 2,048-byte MS-20 program, every byte kept."""

    raw: bytes

    @classmethod
    def parse(cls, raw: bytes) -> Ms20Program:
        if len(raw) != PROGRAM_BYTES or not raw.startswith(_PROGRAM_MAGIC):
            raise StateTransformError("an MS-20 program is 2,048 bytes opening with prog and 2040")
        if raw[8:12] != VERSION or raw[12:16] != bytes(4):
            raise StateTransformError(f"MS-20 program version {raw[8:12].hex()} is not the measured {VERSION.hex()}")
        slots = struct.unpack_from(f"<{SLOTS}f", raw, _SLOTS_AT)
        if not all(math.isfinite(value) and 0 <= value <= 1 for value in slots):
            raise StateTransformError("this MS-20 program holds a value outside 0 to 1")
        if any(raw[_CABLES_END:]):
            raise StateTransformError("this MS-20 program holds bytes after its cable records")
        program = cls(raw)
        program.cables  # noqa: B018 -- refuses records naming no measured jacks
        return program

    @property
    def name(self) -> str:
        return decode_ansi(self.raw[_NAME].split(b"\0", 1)[0])

    def slot(self, slot: int) -> float:
        """The stored 0 to 1 at ``slot``, measured or not."""
        if not 0 <= slot < SLOTS:
            raise StateTransformError(f"an MS-20 program has slots 0 to {SLOTS - 1}, not {slot}")
        return float(_F32.unpack_from(self.raw, _SLOTS_AT + 4 * slot)[0])

    def values(self) -> dict[str, Ms20Value]:
        """Every measured control by name, in the plugin's units."""
        return {control.name: control.curve.value(self.slot(control.slot)) for control in CONTROLS}

    @property
    def cables(self) -> tuple[Cable, ...]:
        records = (self.raw[at : at + 4] for at in range(_CABLES_AT, _CABLES_END, 4))
        return tuple(cable for cable in map(_cable, records) if cable is not None)

    def with_values(self, changes: Mapping[str, Ms20Value]) -> Ms20Program:
        """This program with ``changes`` (by name, in the plugin's units) written; anything else refused."""
        raw = bytearray(self.raw)
        for name, value in changes.items():
            control = CONTROL_BY_NAME.get(name)
            if control is None:
                raise StateTransformError(f"MS-20 has no measured control named {name!r}")
            _F32.pack_into(raw, _SLOTS_AT + 4 * control.slot, control.curve.normalized(value))
        return Ms20Program.parse(bytes(raw))

    def with_cables(self, cables: Sequence[Cable]) -> Ms20Program:
        """This program with its patch panel replaced by ``cables``, at most 17, each jack used once."""
        if len(cables) > CABLES:
            raise StateTransformError(f"an MS-20 program holds at most {CABLES} cables, not {len(cables)}")
        jacks = [jack for cable in cables for jack in (cable.out, cable.to)]
        if len(set(jacks)) != len(jacks):
            raise StateTransformError("an MS-20 jack takes one cable")
        # A cable the program already holds keeps its record, which the plugin may have stored input first.
        held = {_cable(self.raw[at : at + 4]): self.raw[at : at + 4] for at in range(_CABLES_AT, _CABLES_END, 4)}
        records = b"".join(held.get(c) or bytes((JACKS.index(c.out), JACKS.index(c.to), c.colour, 0)) for c in cables)
        records += bytes((_NO_CABLE, _NO_CABLE, 0, 0)) * (CABLES - len(cables))
        return Ms20Program.parse(self.raw[:_CABLES_AT] + records + self.raw[_CABLES_END:])


# -- the bank ----------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class Ms20Bank:
    """An MS-20 bank as Live stores it, every byte kept."""

    raw: bytes
    selected: int

    @classmethod
    def parse(cls, raw: bytes) -> Ms20Bank:
        if len(raw) != BANK_BYTES or not raw.startswith(_BANK_MAGIC):
            raise StateTransformError(f"an MS-20 bank is {BANK_BYTES:,} bytes opening with bolg")
        for at in [*range(0x800, _SELECTED, PROGRAM_BYTES), WORKING, WORKING + PROGRAM_BYTES]:
            if not raw[at:].startswith(_PROGRAM_MAGIC):
                raise StateTransformError(f"this MS-20 bank holds no program at byte {at}")
        (selected,) = struct.unpack_from("<I", raw, _SELECTED)
        if selected >= PROGRAMS or any(raw[_SELECTED + 8 : WORKING]):
            raise StateTransformError("this MS-20 bank's selected program is not one of its 32")
        bank = cls(raw, selected)
        bank.working  # noqa: B018 -- refuses a working program of another layout
        return bank

    @property
    def working(self) -> Ms20Program:
        """The program the plugin plays."""
        return Ms20Program.parse(self.raw[WORKING : WORKING + PROGRAM_BYTES])

    def with_working(self, program: Ms20Program) -> Ms20Bank:
        return Ms20Bank.parse(self.raw[:WORKING] + program.raw + self.raw[WORKING + PROGRAM_BYTES :])

    def encode(self) -> bytes:
        return self.raw


def _is_bank(patch: bytes) -> bool:
    try:
        Ms20Bank.parse(patch)
    except StateTransformError:
        return False
    return True


@register
def korg_ms20() -> PluginMap:
    measured = MeasuredState(StateRung.VERBATIM, (StateEvidence.HOSTED,), datetime.date(2026, 10, 29))
    return PluginMap(
        id="korg.ms-20",
        name="MS-20",
        version="1",
        scope="VST2 only; the 71,696-byte bank, its working program read",
        vendor="KORG",
        vst2=PluginFormat(MS20_VST2, "MS-20", VERBATIM, measured, plan_name="ms_20_vst2"),
        layouts=(Layout("MS-20 bank", _is_bank),),
        binary_sha256=(MS20_SHA256,),
    )
