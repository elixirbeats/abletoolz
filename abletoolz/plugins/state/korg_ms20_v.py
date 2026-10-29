"""Arturia KORG MS-20 V: its patch, read and edited by control and patch cable in the plugin's own units.

Its VST2 state is two Boost text archives behind their lengths
(:class:`~abletoolz.formats.arturia_archive.TwoArchives`), each holding a map of
named numbers, and every control is written into both. Measured on 1.3.3.6106
by hosting the VST2.

``data/korg-ms-20-v.json`` holds the controls a converted patch needs: the key
the archives store, the name the plugin shows, the unit it prints, and the curve
the plugin's own definitions declare from the stored 0 to 1 to that unit.
Swept at 13 positions each, every public one prints as its curve says, and a
patch with all of them written at two positions no sweep used restores and reads
back as written. Five are not public, so the plugin prints none of them: the
voice mode (``Play Mode``, mono retrigger or legato, or 2 to 6 voices),
``Unison``, ``Unison Voices`` and ``Note Priority``; they are stored as an
item's index over the top index, as the public choices are.

The patch panel is 29 sources by 19 destinations, each a switch stored as
``Modulations_PatchBayConnectSrc{s}_Dest{d}``, numbered from 1 in the order of
the plugin's own source and destination lists (``patch_sources`` and
``patch_destinations`` in the data). Renders hear a cable at the destination
that numbering names.

``data/korg-ms-20-v-baseline.state`` is the plugin's own save of its factory
``Default`` preset, no macros assigned; a converted patch is built from it.

The VST3 keeps the same two archives as the two halves of its preset, the first
its processor state and the second its controller state: a patch split that way
and pushed into the installed VST3 comes back with every mapped control in both
halves as written. A VST3 device is written, not read: its framing sees one half.
"""

from __future__ import annotations

import dataclasses
import datetime
import functools
import json
import pathlib
from collections.abc import Iterable, Mapping
from typing import Any

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

MS20_V_VST2 = Vst2Id(0x4B4D5356)
MS20_V_VST3 = Vst3Id((1098019957, 1096173907, 1263358806, 1349676899))
DATA = pathlib.Path(__file__).with_name("data")
PRODUCT = "KORG MS-20 V"
_CABLE_KEY = "Modulations_PatchBayConnectSrc{source}_Dest{destination}"


@functools.cache
def _data() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((DATA / "korg-ms-20-v.json").read_text(encoding="utf-8"))
    return data


@functools.cache
def controls() -> dict[str, Control]:
    """The mapped controls by the key the archives store."""
    data = _data()
    return {str(entry["key"]): control(entry, data["tables"]) for entry in data["controls"]}


@functools.cache
def sources() -> tuple[str, ...]:
    """The patch panel's sources, in the plugin's order."""
    return tuple(_data()["patch_sources"])


@functools.cache
def destinations() -> tuple[str, ...]:
    """The patch panel's destinations, in the plugin's order."""
    return tuple(_data()["patch_destinations"])


@functools.cache
def baseline() -> bytes:
    """The factory Default the controls were measured on."""
    return (DATA / "korg-ms-20-v-baseline.state").read_bytes()


def _cable_fields(source: str, destination: str) -> tuple[str, str]:
    if source not in sources():
        raise StateTransformError(f"{PRODUCT} has no patch source {source!r}")
    if destination not in destinations():
        raise StateTransformError(f"{PRODUCT} has no patch destination {destination!r}")
    key = _CABLE_KEY.format(source=sources().index(source) + 1, destination=destinations().index(destination) + 1)
    return f"section[0]/{key}", f"section[1]/{key}"


@dataclasses.dataclass(frozen=True, slots=True)
class Ms20VPatch:
    """A KORG MS-20 V VST2 state: its two archives, every byte kept."""

    archives: TwoArchives
    raw: bytes

    @classmethod
    def parse(cls, raw: bytes) -> Ms20VPatch:
        try:
            return cls(TwoArchives.parse(raw), raw)
        except FormatError as error:
            raise StateTransformError(f"not a {PRODUCT} patch: {error}") from None

    def values(self) -> dict[str, ArturiaValue]:
        """Every mapped control by key, in the plugin's units."""
        stored = self.archives.values
        out: dict[str, ArturiaValue] = {}
        for key, entry in controls().items():
            field, offset, scale = entry.fields[0]
            if field not in stored:
                raise StateTransformError(f"this {PRODUCT} patch does not store {key}")
            out[key] = entry.value((stored[field] - offset) / scale)
        return out

    @property
    def cables(self) -> tuple[tuple[str, str], ...]:
        """The patch panel's connections, as (source, destination)."""
        stored = self.archives.values
        return tuple(
            (source, destination)
            for source in sources()
            for destination in destinations()
            if stored.get(_cable_fields(source, destination)[0], 0.0) >= 0.5
        )

    def with_values(
        self, changes: Mapping[str, ArturiaValue], cables: Iterable[tuple[str, str]] | None = None
    ) -> Ms20VPatch:
        """This patch with ``changes`` (by key, in its units) in both archives; ``cables`` replace the panel."""
        edits: dict[str, float] = {}
        for key, value in changes.items():
            entry = controls().get(key)
            if entry is None:
                raise StateTransformError(f"{PRODUCT} has no mapped control {key!r}")
            normalized = entry.normalized(value)
            edits.update({field: offset + normalized * scale for field, offset, scale in entry.fields})
        if cables is not None:
            wanted = {field for cable in cables for field in _cable_fields(*cable)}
            for source in sources():
                for destination in destinations():
                    for field in _cable_fields(source, destination):
                        edits[field] = 1.0 if field in wanted else 0.0
        try:
            return Ms20VPatch.parse(self.archives.with_values(edits))
        except FormatError as error:
            raise StateTransformError(f"this {PRODUCT} patch did not take the edit: {error}") from None


@register
def korg_ms20_v() -> PluginMap:
    measured = MeasuredState(StateRung.VERBATIM, (StateEvidence.HOSTED,), datetime.date(2026, 10, 29))
    split = MeasuredState(StateRung.REFRAME, (StateEvidence.HOSTED,), datetime.date(2026, 10, 29))
    return PluginMap(
        id="arturia.korg-ms-20-v",
        name=PRODUCT,
        version="1",
        scope="1.3.3.6106; two archives, each with its map of named numbers; the VST3 is written, not read",
        vendor="Arturia",
        vst2=PluginFormat(MS20_V_VST2, PRODUCT, VERBATIM, measured, plan_name="korg_ms_20_v_vst2"),
        vst3=PluginFormat(
            MS20_V_VST3,
            PRODUCT,
            FirstArchive(PRODUCT),
            split,
            SecondArchive(PRODUCT),
            plan_name="korg_ms_20_v_vst3",
        ),
        layouts=(Layout("two archives with their number maps", is_two_archives),),
    )
