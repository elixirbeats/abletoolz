"""Arturia Prophet-5 V: its patch, read and edited by control in the plugin's own units.

Another product than Prophet V3, with its own UniqueId. Its VST2 state is two
Boost text archives behind their lengths (:class:`TwoArchives`), and its
controls are named numbers in the map each one holds. Measured on 1.3.3.6106
by hosting the VST2: written into both archives, every one of its 96 core
controls is restored and read back by the plugin as written, on a baseline
with no macro assignments (the vendor's default ties nine core controls to
macros, which rewrite them on reload).

The VST3 keeps the same two archives as the two halves of its preset: the
first is its processor state, the second its controller state, each as long
as the VST2's lengths say. Converted Pro-53 patches, split that way and pushed
into the installed VST3 in the host rig, come back with all 96 core controls in
both archives as written. A VST3 device is written, not read: its framing sees
one half.

``data/prophet-5-v.json`` holds the 96 core controls: the key the archives
store, the name the plugin shows, the unit it prints, and the curve its own
definitions declare from the stored 0 to 1 to that unit. Every curve here
reproduces all 672 prints the plugin makes of its controls at seven positions
each (a selector prints its first eight characters).

After them it holds the controls the Pro-53 converter writes beyond the core:
the saved mod wheel, the FX section's switch, FX slot 1's type, bypass and
dry/wet, and its delay's time, feedback, cuts and stereo width. The public ones
print as their definitions say at the same seven positions. The FX switch and
the slot type are not public: a slot type is stored as its item's index over
the top index of 17 (None to BL-20 Flanger), and renders hear the delay at item
2 only with the switch on. Bypass shows On while the slot plays. The hosted
plugin restores the saved wheel, reads it back and plays it, and its delay
echoes at the time it prints, feeds back the gain it prints, and cuts at the
corners it prints (12 dB per octave).
``data/prophet-5-v-baseline.state`` is that macro-free baseline, and what a
patch is built from.
"""

from __future__ import annotations

import dataclasses
import datetime
import functools
import json
import pathlib
from collections.abc import Mapping

from abletoolz.formats.arturia_archive import TwoArchives
from abletoolz.formats.errors import FormatError
from abletoolz.plugins.identity.ids import Vst2Id, Vst3Id
from abletoolz.plugins.state.arturia_v import (
    ArturiaValue,
    Control,
    FirstArchive,
    SecondArchive,
    control,
    is_two_archives,
)
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.framings import VERBATIM
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import Layout, PluginFormat, PluginMap

PROPHET_5_V_VST2 = Vst2Id(1347571509)
PROPHET_5_V_VST3 = Vst3Id((1098019957, 1096173907, 1347571509, 1349676899))
DATA = pathlib.Path(__file__).with_name("data")

type Prophet5Value = ArturiaValue


@functools.cache
def controls() -> dict[str, Control]:
    """The 96 core controls, then the ones beyond them, by the key the archives store."""
    data = json.loads((DATA / "prophet-5-v.json").read_text(encoding="utf-8"))
    return {entry["key"]: control(entry, data["tables"]) for entry in data["controls"]}


@functools.cache
def baseline() -> bytes:
    """The macro-free baseline the controls were measured on."""
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
        """Every mapped control by key, in the plugin's units."""
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
            PROPHET_5_V_VST3,
            "Prophet-5 V",
            FirstArchive("Prophet-5 V"),
            split,
            SecondArchive("Prophet-5 V"),
            plan_name="prophet_5_v_vst3",
        ),
        layouts=(Layout("two archives with their number maps", is_two_archives),),
    )
