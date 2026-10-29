"""Arturia Prophet-5 V: its patch, read and edited by control in the plugin's own units.

Another product than Prophet V3, with its own UniqueId. Its VST2 state is two
Boost text archives behind their lengths (:class:`TwoArchives`), and its
controls are named numbers in the map each one holds. Measured 2026-10-21 on
1.3.3.6106 by hosting the VST2 on the workstation: written into both archives,
every one of its 96 core controls was restored and read back by the plugin as
written, on a baseline with no macro assignments (the vendor's default ties
nine core controls to macros, which rewrite them on reload).

The VST3 keeps the same two archives as the two halves of its preset: the
first is its processor state, the second its controller state, each as long
as the VST2's lengths say. Measured 2026-10-26 in the host rig: the 25
converted Pro-53 patches, split that way and pushed into the installed VST3,
came back with all 96 core controls in both archives as written. A VST3
device is written, not read: its framing sees one half.

``data/prophet-5-v.json`` holds the 96 core controls: the key the archives
store, the name the plugin shows, the unit it prints, and the curve its own
definitions declare from the stored 0 to 1 to that unit. The plugin printed
each control at seven positions on 2026-10-21 and every curve here reproduces
all 672 prints (a selector prints its first eight characters).
``data/prophet-5-v-baseline.state`` is that macro-free baseline, and what a
patch is built from.
"""

from __future__ import annotations

import dataclasses
import datetime
import functools
import json
import math
import pathlib
from collections.abc import Mapping
from typing import Any

from abletoolz.formats.arturia_archive import TwoArchives
from abletoolz.formats.errors import FormatError
from abletoolz.plugins.identity.ids import Vst2Id, Vst3Id
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.framings import VERBATIM
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import Layout, PluginFormat, PluginMap

PROPHET_5_V_VST2 = Vst2Id(1347571509)
PROPHET_5_V_VST3 = Vst3Id((1098019957, 1096173907, 1347571509, 1349676899))
DATA = pathlib.Path(__file__).with_name("data")

type Prophet5Value = bool | int | float | str


@dataclasses.dataclass(frozen=True, slots=True)
class Control:
    """One core control: the key both archives store, the name the plugin shows, and its curve to ``unit``.

    Exactly one of ``choices``, ``table`` and ``curve`` says how the stored 0 to
    1 reads. ``choices`` are the floor of the value times their count, stored as
    an item's index over the top index; ``table`` is
    seconds at nine evenly spaced points, linear between them; ``curve`` is
    ``Linear``, ``Exp(r)`` (``expm1(r u) / expm1(r)`` of the way from ``low``
    to ``high``) or ``BipolarExp(r)`` (the same, mirrored around the middle).
    """

    key: str
    name: str
    unit: str | None
    fields: tuple[tuple[str, float, float], ...]
    choices: tuple[str, ...] = ()
    table: tuple[float, ...] = ()
    curve: str = ""
    low: float = 0.0
    high: float = 1.0

    def value(self, normalized: float) -> Prophet5Value:
        """What the plugin shows for the stored ``normalized``."""
        if self.choices:
            item = self.choices[min(math.floor(normalized * len(self.choices)), len(self.choices) - 1)]
            if self.choices == ("Off", "On"):
                return item == "On"
            return int(item) if self.unit == "st" else item
        if self.table:
            scaled = normalized * (len(self.table) - 1)
            index = min(int(scaled), len(self.table) - 2)
            return self.table[index] + (scaled - index) * (self.table[index + 1] - self.table[index])
        return self.low + self._shape(normalized) * (self.high - self.low)

    def normalized(self, value: Prophet5Value) -> float:
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
                raise StateTransformError(f"{number} s is outside {self.key}'s {self.table[0]} to {self.table[-1]}")
            step = 1 / (len(self.table) - 1)
            for index, (left, right) in enumerate(zip(self.table, self.table[1:], strict=False)):
                if number <= right:
                    return (index + (number - left) / (right - left)) * step
            return 1.0
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


def _control(entry: Mapping[str, Any], tables: Mapping[str, list[float]]) -> Control:
    curve = str(entry.get("curve", ""))
    if curve and not (curve == "Linear" or curve.startswith(("Exp(", "BipolarExp("))):
        raise StateTransformError(f"Prophet-5 V's {curve} curve is not one abletoolz evaluates")
    return Control(
        key=entry["key"],
        name=entry["name"],
        unit=entry.get("unit"),
        fields=tuple((str(field), float(offset), float(scale)) for field, offset, scale in entry["fields"]),
        choices=tuple(entry.get("choices", ())),
        table=tuple(tables[entry["table"]]) if "table" in entry else (),
        curve=curve,
        low=float(entry.get("low", 0.0)),
        high=float(entry.get("high", 1.0)),
    )


@functools.cache
def controls() -> dict[str, Control]:
    """The 96 core controls by the key the archives store, in the plugin's order."""
    data = json.loads((DATA / "prophet-5-v.json").read_text(encoding="utf-8"))
    return {entry["key"]: _control(entry, data["tables"]) for entry in data["controls"]}


@functools.cache
def baseline() -> bytes:
    """The macro-free baseline the 96 core controls were measured on."""
    return (DATA / "prophet-5-v-baseline.state").read_bytes()


@dataclasses.dataclass(frozen=True, slots=True)
class Prophet5Patch:
    """A Prophet-5 V VST2 state: its two archives, every byte kept."""

    archives: TwoArchives
    raw: bytes

    @classmethod
    def parse(cls, raw: bytes) -> Prophet5Patch:
        try:
            return cls(TwoArchives.parse(raw), raw)
        except FormatError as error:
            raise StateTransformError(f"not a Prophet-5 V patch: {error}") from None

    def normalized(self, control: Control) -> float:
        field, offset, scale = control.fields[0]
        stored = self.archives.values.get(field)
        if stored is None:
            raise StateTransformError(f"this Prophet-5 V patch does not store {control.key}")
        return (stored - offset) / scale

    def values(self) -> dict[str, Prophet5Value]:
        """The 96 core controls by key, in the plugin's units."""
        return {key: control.value(self.normalized(control)) for key, control in controls().items()}

    def with_values(self, changes: Mapping[str, Prophet5Value]) -> Prophet5Patch:
        """This patch with ``changes`` (by key, in the plugin's units) written into both archives."""
        edits: dict[str, float] = {}
        for key, value in changes.items():
            control = controls().get(key)
            if control is None:
                raise StateTransformError(f"Prophet-5 V has no core control {key!r}")
            normalized = control.normalized(value)
            edits.update({field: offset + normalized * scale for field, offset, scale in control.fields})
        try:
            return Prophet5Patch.parse(self.archives.with_values(edits))
        except FormatError as error:
            raise StateTransformError(f"this Prophet-5 V patch did not take the edit: {error}") from None


@dataclasses.dataclass(frozen=True, slots=True)
class _FirstArchive:
    """A VST3 processor state that is the first of the VST2 state's two archives."""

    def lift(self, state: bytes) -> bytes:
        raise StateTransformError("Prophet-5 V's VST3 keeps its patch across both halves; reading one is not measured")

    def lower(self, patch: bytes) -> bytes:
        try:
            return TwoArchives.split(patch)[0]
        except FormatError as error:
            raise StateTransformError(f"not a Prophet-5 V patch: {error}") from None


@dataclasses.dataclass(frozen=True, slots=True)
class _SecondArchive:
    """A VST3 controller state that is the second of the VST2 state's two archives."""

    def build(self, source: bytes) -> bytes | None:
        try:
            return TwoArchives.split(source)[1]
        except FormatError as error:
            raise StateTransformError(f"not a Prophet-5 V patch: {error}") from None


def _is_two_archives(patch: bytes) -> bool:
    try:
        TwoArchives.parse(patch)
    except FormatError:
        return False
    return True


@register
def prophet_5_v() -> PluginMap:
    measured = MeasuredState(StateRung.VERBATIM, (StateEvidence.HOSTED,), datetime.date(2026, 10, 21))
    split = MeasuredState(StateRung.REFRAME, (StateEvidence.HOSTED,), datetime.date(2026, 10, 26))
    return PluginMap(
        id="arturia.prophet-5-v",
        name="Prophet-5 V",
        version="1",
        scope="1.3.3.6106; two archives, each with its map of named numbers; the VST3 is written, not read",
        vendor="Arturia",
        vst2=PluginFormat(PROPHET_5_V_VST2, "Prophet-5 V", VERBATIM, measured, plan_name="prophet_5_v_vst2"),
        vst3=PluginFormat(
            PROPHET_5_V_VST3, "Prophet-5 V", _FirstArchive(), split, _SecondArchive(), plan_name="prophet_5_v_vst3"
        ),
        layouts=(Layout("two archives with their number maps", _is_two_archives),),
    )
