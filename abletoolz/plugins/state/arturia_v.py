"""What Arturia's V Collection instruments share: a control's curve from its stored 0 to 1, and the VST3 halves.

Each V instrument ships definitions saying how a control's stored 0 to 1 reads
in the unit it prints. :class:`Control` evaluates the shapes those definitions
use, and the instrument modules hold the measured controls as data. Their VST2
state is two Boost text archives behind their lengths
(:class:`~abletoolz.formats.arturia_archive.TwoArchives`); the VST3 keeps the
same two archives as its processor and controller halves. An instrument that
saves the same archive twice is an :class:`ArchiveInstrument`, edited by the
names in its map.
"""

from __future__ import annotations

import dataclasses
import datetime
import functools
import math
import pathlib
from collections.abc import Mapping
from typing import Any

from abletoolz.formats.arturia_archive import TwoArchives
from abletoolz.formats.errors import FormatError
from abletoolz.plugins.identity.ids import Vst2Id, Vst3Id
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.framings import VERBATIM
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import Layout, PluginFormat, PluginMap

type ArturiaValue = bool | int | float | str

ARCHIVE_SIGNATURE = b"22 serialization::archive "
DATA = pathlib.Path(__file__).with_name("data")


@dataclasses.dataclass(frozen=True, slots=True)
class Control:
    """One control: the key both archives store, the name the plugin shows, and its curve to ``unit``.

    Exactly one of ``choices``, ``table`` and ``curve`` says how the stored 0 to
    1 reads. ``choices`` are the floor of the value times their count, stored as
    an item's index over the top index; ``table`` is values at evenly spaced
    points, linear between them, read from its far end when ``flipped``;
    ``curve`` is ``Linear``, ``Exp(r)`` (``expm1(r u) / expm1(r)`` of the way
    from ``low`` to ``high``) or ``BipolarExp(r)`` (the same, mirrored around
    the middle).
    """

    key: str
    name: str
    unit: str | None
    fields: tuple[tuple[str, float, float], ...]
    choices: tuple[str, ...] = ()
    table: tuple[float, ...] = ()
    flipped: bool = False
    curve: str = ""
    low: float = 0.0
    high: float = 1.0

    def value(self, normalized: float) -> ArturiaValue:
        """What the plugin shows for the stored ``normalized``; a value outside 0 to 1 is refused.

        Every factory preset of both instruments stays within 0 to 1 but for float rounding (2.4e-9 at most).
        """
        if not -1e-6 <= normalized <= 1 + 1e-6:
            raise StateTransformError(f"{self.key} stores {normalized}, outside 0 to 1")
        if self.choices:
            item = self.choices[min(math.floor(normalized * len(self.choices)), len(self.choices) - 1)]
            if self.choices == ("Off", "On"):
                return item == "On"
            return int(item) if self.unit == "st" else item
        if self.table:
            scaled = (1 - normalized if self.flipped else normalized) * (len(self.table) - 1)
            index = min(int(scaled), len(self.table) - 2)
            return self.table[index] + (scaled - index) * (self.table[index + 1] - self.table[index])
        return self.low + self._shape(normalized) * (self.high - self.low)

    def normalized(self, value: ArturiaValue) -> float:
        """The stored 0 to 1 at which the plugin shows ``value``; refused outside the control's range."""
        if self.choices:
            item = ("On" if value else "Off") if type(value) is bool and self.choices == ("Off", "On") else str(value)
            if item not in self.choices or type(value) is float:
                raise StateTransformError(f"{self.key} has no setting {value!r}")
            return self.choices.index(item) / (len(self.choices) - 1)
        if type(value) not in (int, float) or not math.isfinite(value):  # type: ignore[arg-type]
            raise StateTransformError(f"{self.key} takes a finite number, not {value!r}")
        number = float(value)
        if self.table:
            if not self.table[0] <= number <= self.table[-1]:
                raise StateTransformError(f"{number} is outside {self.key}'s {self.table[0]} to {self.table[-1]}")
            step = 1 / (len(self.table) - 1)
            position = 1.0
            for index, (left, right) in enumerate(zip(self.table, self.table[1:], strict=False)):
                if number <= right:
                    position = (index + (number - left) / (right - left)) * step
                    break
            return 1 - position if self.flipped else position
        lowest, highest = min(self.low, self.high), max(self.low, self.high)
        if not lowest - 1e-9 <= number <= highest + 1e-9:
            raise StateTransformError(f"{number} is outside {self.key}'s {self.low} to {self.high}")
        return min(max(self._unshape((number - self.low) / (self.high - self.low)), 0.0), 1.0)

    def _rate(self) -> float:
        return float(self.curve[self.curve.index("(") + 1 : -1])

    def _shape(self, u: float) -> float:
        if self.curve.startswith("Exp("):
            rate = self._rate()
            return math.expm1(rate * u) / math.expm1(rate)
        if self.curve.startswith("BipolarExp("):
            rate, d = self._rate(), 2 * u - 1
            return (1 + math.copysign(math.expm1(rate * abs(d)) / math.expm1(rate), d)) / 2
        return u

    def _unshape(self, x: float) -> float:
        if self.curve.startswith("Exp("):
            rate = self._rate()
            return math.log1p(x * math.expm1(rate)) / rate
        if self.curve.startswith("BipolarExp("):
            rate, d = self._rate(), 2 * x - 1
            return (1 + math.copysign(math.log1p(abs(d) * math.expm1(rate)) / rate, d)) / 2
        return x


def control(entry: Mapping[str, Any], tables: Mapping[str, list[float]]) -> Control:
    """A :class:`Control` from one entry of an instrument's control data."""
    curve = str(entry.get("curve", ""))
    if curve and not (curve == "Linear" or curve.startswith(("Exp(", "BipolarExp("))):
        raise StateTransformError(f"the {curve} curve is not one abletoolz evaluates")
    return Control(
        key=entry["key"],
        name=entry["name"],
        unit=entry.get("unit"),
        fields=tuple((str(field), float(offset), float(scale)) for field, offset, scale in entry["fields"]),
        choices=tuple(entry.get("choices", ())),
        table=tuple(tables[entry["table"]]) if "table" in entry else (),
        flipped=bool(entry.get("flipped", False)),
        curve=curve,
        low=float(entry.get("low", 0.0)),
        high=float(entry.get("high", 1.0)),
    )


@dataclasses.dataclass(frozen=True, slots=True)
class FirstArchive:
    """A VST3 processor state that is the first of the VST2 state's two archives."""

    product: str

    def lift(self, state: bytes) -> bytes:
        raise StateTransformError(
            f"{self.product}'s VST3 keeps its patch across both halves; reading one is not measured"
        )

    def lower(self, patch: bytes) -> bytes:
        try:
            return TwoArchives.split(patch)[0]
        except FormatError as error:
            raise StateTransformError(f"not a {self.product} patch: {error}") from None


@dataclasses.dataclass(frozen=True, slots=True)
class SecondArchive:
    """A VST3 controller state that is the second of the VST2 state's two archives."""

    product: str

    def build(self, source: bytes) -> bytes | None:
        try:
            return TwoArchives.split(source)[1]
        except FormatError as error:
            raise StateTransformError(f"not a {self.product} patch: {error}") from None


@dataclasses.dataclass(frozen=True, slots=True)
class OneArchive:
    """A VST3 that saves one archive as both its processor and its controller state: the archive twice."""

    product: str

    def lift(self, state: bytes) -> bytes:
        if not state.startswith(ARCHIVE_SIGNATURE):
            raise StateTransformError(f"a {self.product} state is a Boost text archive, and this one is not")
        return TwoArchives.join(state, state)

    def lower(self, patch: bytes) -> bytes:
        raise StateTransformError(f"writing {self.product} is not measured")


def holds_two_archives(patch: bytes) -> bool:
    """Whether ``patch`` is two Boost text archives behind their lengths, whatever their maps hold."""
    try:
        return all(archive.startswith(ARCHIVE_SIGNATURE) for archive in TwoArchives.split(patch))
    except FormatError:
        return False


def is_two_archives(patch: bytes) -> bool:
    try:
        TwoArchives.parse(patch)
    except FormatError:
        return False
    return True


def with_values(patch: bytes, changes: Mapping[str, float], product: str) -> bytes:
    """``patch`` with each named control set to its stored 0 to 1 in both archives, spelled as the plugin does."""
    edits = {f"section[{n}]/{name}": value for n in (0, 1) for name, value in changes.items()}
    try:
        return TwoArchives.parse(patch).with_values(edits, single=True)
    except FormatError as error:
        raise StateTransformError(f"this {product} patch did not take the edit: {error}") from None


@functools.cache
def _read(path: pathlib.Path) -> bytes:
    return path.read_bytes()


@dataclasses.dataclass(frozen=True, slots=True)
class ArchiveInstrument:
    """A V instrument that saves the same archive twice, edited by the names in its map; its VST3 is written only.

    ``baseline`` is a state the installed plugin saved, in the VST2 form, that a converted patch is built from.
    """

    map_id: str
    product: str
    unique_id: int
    build: str
    baseline_file: str
    measured: datetime.date

    def baseline(self) -> bytes:
        return _read(DATA / self.baseline_file)

    def with_values(self, patch: bytes, changes: Mapping[str, float]) -> bytes:
        return with_values(patch, changes, self.product)

    def plugin_map(self) -> PluginMap:
        verbatim = MeasuredState(StateRung.VERBATIM, (StateEvidence.HOSTED,), self.measured)
        split = MeasuredState(StateRung.REFRAME, (StateEvidence.HOSTED,), self.measured)
        plan_name = self.map_id.removeprefix("arturia.").replace("-", "_")
        return PluginMap(
            id=self.map_id,
            name=self.product,
            version="3",
            scope=f"{self.build}; the same archive twice, with its map of named numbers; the VST3 is written, not read",
            vendor="Arturia",
            vst2=PluginFormat(Vst2Id(self.unique_id), self.product, VERBATIM, verbatim, plan_name=f"{plan_name}_vst2"),
            vst3=PluginFormat(
                Vst3Id((1098019957, 1096173907, self.unique_id, 1349676899)),
                self.product,
                FirstArchive(self.product),
                split,
                SecondArchive(self.product),
                plan_name=f"{plan_name}_vst3",
            ),
            layouts=(Layout("two archives with their number maps", is_two_archives),),
        )
