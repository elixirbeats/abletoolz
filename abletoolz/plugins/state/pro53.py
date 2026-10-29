"""Native Instruments Pro-53: its saved bank, read and edited by control name in the plugin's own units.

Pro-53 is a 32-bit VST2 only. Live stores its whole bank: 512 factory programs,
the program the device has selected, and the working patch the device plays.
The layout, measured 2026-10-21 on the 25 distinct banks in the owner's sets:

    3a 01 01 00 00, "PRO-53" length-prefixed, a u32 304, then that block:
        float32 master tune and volume at bytes 19 and 23, the selected
        program as a u32 at 35, and 70 float32 at 39 -- the snapshot
    the saved patch name, u32 301, u32 512, then 512 programs (a name and two words each)
    the saved name again, two words, u32 70
    70 records in control order: the control's own u32 id and 513 bytes (each
        factory program's value, then the working patch's), or ``FFFFFFFF``
        alone for master tune (66), master volume (67) and pitch bend (69)
    2,076 bytes after them

Every one of those counts and lengths is checked, and a bank that departs from
them is refused, so only the measured layout is read.

Each control is saved twice, and the two do different jobs. The snapshot is
what the engine plays: a cutoff written only there changed the sound and not
the knob. The working byte is what the knob shows, a 7-bit value over 127.
Written together they agree with the plugin's own setter, sample for sample
(measured on the effect build, 2026-10-21). In the owner's banks the two agree
on 1,673 of 1,675 knobs, and the other two by less than half a step. A value
here is read off the knob, which is what the plugin's own getter and display
report; an edit writes both.

The controls, their names, order and display curves were measured 2026-10-28 by
hosting the instrument itself -- ``Pro-53.dll`` 3.0.0.13, x86, the ``P3SF`` every
device in the sets stores -- without an editor or audio, setting every control
to 255 positions and reading its getter and display back. It reports 69
controls; no control moves another. Pitch bend is the snapshot's 70th float and
is not one of them, though Live's own record of these devices, written through
jBridge, counts 70 parameters, so it is read here but never edited. All 25 real
banks restored in it read back, control by control, the values this module
reads, and so do 11 banks it edited, every control set (`test/test_pro53.py`).

Units are what the plugin prints. Envelope times, glide and the mixer print a
percentage, so a percentage is what they are here; their time and gain curves
are not measured. The oscillator pitch knobs print whole semitones.

Pro-53's own save (``effGetChunk``), hosted, is a scrambled form flagged with a
1 in byte 4 and the same length; every bank in the sets is the plain form, which
the plugin restores. Only the plain form is read and written.
"""

from __future__ import annotations

import dataclasses
import datetime
import math
import struct
from collections.abc import Mapping

from abletoolz.plugins.identity.ids import Vst2Id
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.framings import VERBATIM
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import Layout, PluginFormat, PluginMap

# The instrument every Pro-53 device in a set stores, ``P3SF``.
PRO53_VST2 = Vst2Id(0x50335346)
PRO53_SHA256 = "1faa7f8951d7548c52e66ffe6ae567b96caade9fcb4b88ccf4dd1696eebd098b"
SNAPSHOT_FLOATS = 70
PROGRAMS = 512

_SIGNATURE = bytes.fromhex("3a010100000600000050524f2d3533")
_BLOCK = 304
_SNAPSHOT = 39
_SELECTED = 35
_GLOBAL_FLOATS = {66: 19, 67: 23}
_DIRECTORY_VERSION = 301
# Each control's record opens with its own id, and the plugin files a record by that id (measured
# 2026-10-28: a bank whose ids were renumbered restored into other controls). The three saved
# only as floats carry FFFFFFFF.
RECORD_IDS = (
    1029, 1030, 1031, 1032, 1033, 1001, 1002, 1003, 1000, 1004, 1026, 1025, 1027, 1101, 1022, 1023, 1021, 1024,
    1017, 1018, 1019, 1020, 1102, 1103, 1104, 1105, 1106, 1108, 1107, 1110, 1111, 1109, 1112, 1113, 1072, 1037,
    1034, 1035, 1036, 1010, 1006, 1007, 1008, 1009, 1005, 1011, 1012, 1043, 1038, 1039, 1040, 1041, 1042, 1046,
    1050, 1013, 1014, 1015, 1016, 1052, 1047, 1114, 1115, 1116, 1117, 1049, 0xFFFFFFFF, 0xFFFFFFFF, 1044, 0xFFFFFFFF,
)  # fmt: skip
_TAIL = 2076
_NO_RECORD = 0xFFFFFFFF
_U32 = struct.Struct("<I")
_F32 = struct.Struct("<f")
_STEPS = 127

type Pro53Value = bool | int | float


# -- how a control's 0 to 1 reads in the plugin's units ----------------------


@dataclasses.dataclass(frozen=True, slots=True)
class Switch:
    """Off or on. The plugin's setter turns it on from 0.5; a bank holds 0 or 127."""

    unit: None = None

    def value(self, normalized: float) -> bool:
        return normalized >= 0.5

    def normalized(self, value: Pro53Value) -> float:
        if type(value) is not bool:
            raise StateTransformError("a Pro-53 switch takes True or False")
        return 1.0 if value else 0.0


@dataclasses.dataclass(frozen=True, slots=True)
class Line:
    """A straight line from ``low`` at 0 to ``high`` at 1."""

    low: float
    high: float
    unit: str

    def value(self, normalized: float) -> float:
        return self.low + (self.high - self.low) * normalized

    def normalized(self, value: Pro53Value) -> float:
        return _in_range((_number(value) - self.low) / (self.high - self.low), value, self)


@dataclasses.dataclass(frozen=True, slots=True)
class Exponential:
    """``low`` at 0 to ``high`` at 1, a constant ratio per step."""

    low: float
    high: float
    unit: str

    def value(self, normalized: float) -> float:
        return float(self.low * (self.high / self.low) ** normalized)

    def normalized(self, value: Pro53Value) -> float:
        number = _number(value)
        if number <= 0:
            raise StateTransformError(f"{value} {self.unit} is outside {self.low} to {self.high}")
        return _in_range(math.log(number / self.low) / math.log(self.high / self.low), value, self)


@dataclasses.dataclass(frozen=True, slots=True)
class Semitones:
    """Whole semitones, -24 to 24, two or three knob steps each: ``(3 * step + offset) // 8 - 24``."""

    offset: int
    unit: str = "st"

    def value(self, normalized: float) -> int:
        return (3 * _step(normalized) + self.offset) // 8 - 24

    def normalized(self, value: Pro53Value) -> float:
        if type(value) is not int:
            raise StateTransformError("a Pro-53 oscillator pitch takes whole semitones")
        steps = [step for step in range(_STEPS + 1) if (3 * step + self.offset) // 8 - 24 == value]
        if not steps:
            raise StateTransformError(f"{value} st is outside a Pro-53 oscillator's -24 to 24")
        # Every step of the run prints the same semitone; the middle one sits furthest from its neighbours.
        return steps[len(steps) // 2] / _STEPS


type Curve = Switch | Line | Exponential | Semitones


def _number(value: Pro53Value) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise StateTransformError(f"{value!r} is not a finite number")
    return float(value)


def _in_range(normalized: float, value: Pro53Value, curve: Line | Exponential) -> float:
    if not -1e-9 <= normalized <= 1 + 1e-9:
        raise StateTransformError(f"{value} {curve.unit} is outside {curve.low} to {curve.high}")
    return min(max(normalized, 0.0), 1.0)


def _step(normalized: float) -> int:
    """The 7-bit knob step nearest ``normalized``, half up."""
    return math.floor(normalized * _STEPS + 0.5)


SWITCH = Switch()
PERCENT = Line(0.0, 100.0, "%")


@dataclasses.dataclass(frozen=True, slots=True)
class Control:
    """One of Pro-53's controls: its index, the name the plugin gives it, and how its value reads."""

    index: int
    name: str
    curve: Curve


# Measured 2026-10-28 on the hosted instrument: each display read at 255 positions.
# Percentages print round-half-up of 100 x the value, the other lines likewise in
# their unit; delay time prints whole milliseconds; the two rates print six
# significant digits and agree with these exponentials to 1.5e-5. Delay depth
# prints one significant digit, which bounds its ends to about 1%.
_TABLE: tuple[tuple[str, Curve], ...] = (
    ("PolyMod Source Filt Env", PERCENT),
    ("PolyMod Source Osc B", PERCENT),
    ("PolyMod Dest Freq A", SWITCH),
    ("PolyMod Dest PWidth A", SWITCH),
    ("PolyMod Dest Filter", SWITCH),
    ("Oscillator A Frequency", Semitones(7)),
    ("Oscillator A Sawtooth", SWITCH),
    ("Oscillator A Pulse", SWITCH),
    ("Oscillator A PulseWidth", PERCENT),
    ("Oscillator A Sync", SWITCH),
    ("Mixer Oscillator A", PERCENT),
    ("Mixer Oscillator B", PERCENT),
    ("Mixer Noise", PERCENT),
    ("Mixer External Input", PERCENT),
    ("Filter Cutoff", Line(0.0, 120.0, "st")),
    ("Filter Resonance", PERCENT),
    ("Filter Envelope Amount", Line(0.0, 96.0, "st")),
    ("Filter Keyboard Follow", PERCENT),
    ("Filter Attack", PERCENT),
    ("Filter Decay", PERCENT),
    ("Filter Sustain", PERCENT),
    ("Filter Release", PERCENT),
    ("Delay Effect Time", Exponential(1.0, 1000.0, "ms")),
    ("Delay Effect Spread", PERCENT),
    ("Delay Effect Depth", Exponential(0.000185, 0.0274, "ms")),
    ("Delay Effect Rate", Exponential(0.255495, 25.9567, "Hz")),
    ("Delay Effect Feedback", PERCENT),
    ("Delay Effect Low Cut", Line(0.0, 120.0, "st")),
    ("Delay Effect High Cut", Line(40.0, 120.0, "st")),
    ("Delay Effect Invert", SWITCH),
    ("Delay Effect On", SWITCH),
    ("Delay Effect Wet", PERCENT),
    ("Delay Effect Sync", SWITCH),
    ("Delay Effect MIDI Sync", SWITCH),
    ("LFO MIDI Sync", SWITCH),
    ("LFO Frequency", Exponential(0.0386438, 32.7034, "Hz")),
    ("LFO Sawtooth", SWITCH),
    ("LFO Triangle", SWITCH),
    ("LFO Pulse", SWITCH),
    ("Oscillator B Frequency", Semitones(3)),
    ("Oscillator B Freq Fine", Line(0.0, 100.0, "ct")),
    ("Oscillator B Sawtooth", SWITCH),
    ("Oscillator B Triangle", SWITCH),
    ("Oscillator B Pulse", SWITCH),
    ("Oscillator B PulseWidth", PERCENT),
    ("Oscillator B Low Freq", SWITCH),
    ("Oscillator B Key Follow", SWITCH),
    ("WheelMod LFO-Noise Mix", PERCENT),
    ("WheelMod Dest Freq A", SWITCH),
    ("WheelMod Dest Freq B", SWITCH),
    ("WheelMod Dest PWidth A", SWITCH),
    ("WheelMod Dest PWidth B", SWITCH),
    ("WheelMod Dest Filter", SWITCH),
    ("Glide Time", PERCENT),
    ("Unisono Mode", SWITCH),
    ("Amplifier Attack", PERCENT),
    ("Amplifier Decay", PERCENT),
    ("Amplifier Sustain", PERCENT),
    ("Amplifier Release", PERCENT),
    ("Release on/off", SWITCH),
    ("Velocity on/off", SWITCH),
    ("LFO Envelope Trigger", SWITCH),
    ("Amplifier Hold", SWITCH),
    ("Filter HPF-Mode", SWITCH),
    ("Filter Envelope Invert", SWITCH),
    ("Analog", PERCENT),
    ("Master Tune", Line(-70.0, 70.0, "ct")),
    ("Master Volume", PERCENT),
    ("Modulation Wheel", PERCENT),
)
CONTROLS = tuple(Control(index, name, curve) for index, (name, curve) in enumerate(_TABLE))
CONTROL_BY_NAME = {control.name: control for control in CONTROLS}


# -- the bank ----------------------------------------------------------------


class _Cursor:
    def __init__(self, raw: bytes, at: int) -> None:
        self.raw, self.at = raw, at

    def u32(self) -> int:
        if self.at + 4 > len(self.raw):
            raise StateTransformError(f"this Pro-53 bank ends inside a word at byte {self.at}")
        (value,) = _U32.unpack_from(self.raw, self.at)
        self.at += 4
        return int(value)

    def name(self) -> bytes:
        length = self.u32()
        if length > 256 or self.at + length > len(self.raw):
            raise StateTransformError(f"a Pro-53 name at byte {self.at} is longer than its bank")
        value = self.raw[self.at : self.at + length]
        self.at += length
        return value


@dataclasses.dataclass(frozen=True, slots=True)
class Pro53Bank:
    """A Pro-53 bank as Live stores it, every byte kept: the working patch, its name and the selected program."""

    raw: bytes
    name: str
    selected: int
    knobs: tuple[int | None, ...]
    """The working patch's 7-bit knob values in control order (70); None for the three saved only as floats."""
    knob_offsets: tuple[int | None, ...]

    @classmethod
    def parse(cls, raw: bytes) -> Pro53Bank:
        if not raw.startswith(_SIGNATURE):
            raise StateTransformError("a Pro-53 bank opens with 3a 01 01 00 00 and PRO-53, and this one does not")
        cursor = _Cursor(raw, len(_SIGNATURE))
        if cursor.u32() != _BLOCK or len(raw) < _SNAPSHOT + 4 * SNAPSHOT_FLOATS:
            raise StateTransformError("this Pro-53 bank's leading block is not the measured 304 bytes")
        sound = struct.unpack_from(f"<{SNAPSHOT_FLOATS}f", raw, _SNAPSHOT)
        if not all(0 <= value <= 1 for value in sound):
            raise StateTransformError("this Pro-53 bank's snapshot holds a value outside 0 to 1")
        (selected,) = _U32.unpack_from(raw, _SELECTED)
        cursor.at = len(_SIGNATURE) + 4 + _BLOCK
        name = cursor.name()
        if (cursor.u32(), cursor.u32()) != (_DIRECTORY_VERSION, PROGRAMS):
            raise StateTransformError("this Pro-53 bank's program directory is not the measured 301 / 512")
        for _ in range(PROGRAMS):
            cursor.name()
            cursor.u32(), cursor.u32()
        if cursor.name() != name:
            raise StateTransformError("this Pro-53 bank does not repeat its saved name where it should")
        cursor.u32(), cursor.u32()
        if cursor.u32() != SNAPSHOT_FLOATS:
            raise StateTransformError("this Pro-53 bank does not hold the measured 70 control records")
        knobs: list[int | None] = []
        offsets: list[int | None] = []
        for index in range(SNAPSHOT_FLOATS):
            if cursor.u32() != RECORD_IDS[index]:
                raise StateTransformError(f"Pro-53 control {index} is not saved where the measured layout has it")
            if RECORD_IDS[index] == _NO_RECORD:
                knobs.append(None)
                offsets.append(None)
                continue
            values = raw[cursor.at : cursor.at + PROGRAMS + 1]
            if len(values) != PROGRAMS + 1 or max(values) > _STEPS:
                raise StateTransformError(f"Pro-53 control {index}'s values are cut short or not 7-bit")
            knobs.append(values[PROGRAMS])
            offsets.append(cursor.at + PROGRAMS)
            cursor.at += PROGRAMS + 1
        if len(raw) - cursor.at != _TAIL:
            raise StateTransformError(
                f"this Pro-53 bank ends {len(raw) - cursor.at} bytes after its records, not {_TAIL}"
            )
        if selected >= PROGRAMS:
            raise StateTransformError(f"this Pro-53 bank selects program {selected} of {PROGRAMS}")
        return cls(raw, name.decode("cp1252"), selected, tuple(knobs), tuple(offsets))

    @property
    def sound(self) -> tuple[float, ...]:
        """The snapshot, what the engine plays: one value 0 to 1 per control, pitch bend last (70)."""
        return struct.unpack_from(f"<{SNAPSHOT_FLOATS}f", self.raw, _SNAPSHOT)

    def normalized(self, control: Control) -> float:
        """``control`` as the plugin's getter reports it, 0 to 1."""
        knob = self.knobs[control.index]
        if knob is None:
            return float(_F32.unpack_from(self.raw, _GLOBAL_FLOATS[control.index])[0])
        return knob / _STEPS

    def values(self) -> dict[str, Pro53Value]:
        """The working patch by control name, in the plugin's units (see :data:`CONTROLS`)."""
        return {control.name: control.curve.value(self.normalized(control)) for control in CONTROLS}

    def with_values(self, changes: Mapping[str, Pro53Value]) -> Pro53Bank:
        """This bank with the working patch's ``changes`` written into both the knob and the snapshot.

        A knob holds 127 steps, so a value lands on the nearest one; master tune
        and volume are floats and land exactly. A name the plugin does not use,
        or a value outside a control's range, is refused.
        """
        raw = bytearray(self.raw)
        for name, value in changes.items():
            control = CONTROL_BY_NAME.get(name)
            if control is None:
                raise StateTransformError(f"Pro-53 has no control named {name!r}")
            normalized = control.curve.normalized(value)
            offset = self.knob_offsets[control.index]
            if offset is None:
                written = _F32.pack(normalized)
                raw[_GLOBAL_FLOATS[control.index] : _GLOBAL_FLOATS[control.index] + 4] = written
            else:
                step = _step(normalized)
                raw[offset] = step
                written = _F32.pack(step / _STEPS)
            at = _SNAPSHOT + 4 * control.index
            raw[at : at + 4] = written
        return Pro53Bank.parse(bytes(raw))

    def encode(self) -> bytes:
        return self.raw


def _is_bank(patch: bytes) -> bool:
    try:
        Pro53Bank.parse(patch)
    except StateTransformError:
        return False
    return True


@register
def pro53() -> PluginMap:
    measured = MeasuredState(StateRung.VERBATIM, (StateEvidence.HOSTED,), datetime.date(2026, 10, 28))
    return PluginMap(
        id="ni.pro-53",
        name="Pro-53",
        version="3",
        scope="VST2 only; the plain bank layout the 25 banks in the owner's sets share",
        vendor="Native Instruments",
        vst2=PluginFormat(PRO53_VST2, "Pro-53", VERBATIM, measured),
        layouts=(Layout("Pro-53 3 bank", _is_bank),),
        binary_sha256=(PRO53_SHA256,),
    )
