"""Arturia's second-generation V instruments into their V3 successors."""

from __future__ import annotations

import struct

import pytest

from abletoolz.formats.arturia_archive import TwoArchives
from abletoolz.plugins.identity.ids import PluginKind
from abletoolz.plugins.plan.machine import Machine
from abletoolz.plugins.plan.model import Route
from abletoolz.plugins.plan.targets import mapped_targets
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.measured import StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import FormatState


def archive(**values: float) -> bytes:
    """A synthetic Boost text archive with a preset record and a map of named numbers."""
    fields = " ".join(f"{len(name)} {name} {value}" for name, value in sorted(values.items()))
    return f"22 serialization::archive 10 0 7 0 7 4 Test 0 0 0 {len(values)} 0 0 0 {fields} 0 ".encode()


def two(first: bytes, second: bytes) -> bytes:
    return struct.pack("<QQ", len(first), len(second)) + first + second


OLDER = archive(**{"Hammer Hardness": 0.1})
PATCH = archive(**{"Hammer Hardness": 0.7, "Master Volume": 0.5, "Reverb Wet": 0.25})


def convert(source: str, target: str, patch: bytes) -> bytes:
    found = CATALOG.converter(source, target)
    assert found is not None
    assert found.measured.rung is StateRung.VENDOR_COMPAT and found.measured.evidence == (StateEvidence.HOSTED,)
    return found.convert(patch, None).patch


def test_piano_v2_becomes_its_patch_archive_twice_and_the_older_copy_is_left() -> None:
    patch = convert("arturia.piano-v2", "arturia.piano-v3", two(OLDER, PATCH))
    assert TwoArchives.split(patch) == (PATCH, PATCH)
    assert TwoArchives.parse(patch).archives[0].values["Hammer Hardness"] == pytest.approx(0.7)


def test_piano_v2_says_what_piano_v3_s_import_re_sets() -> None:
    found = CATALOG.converter("arturia.piano-v2", "arturia.piano-v3")
    assert found is not None
    (notice,) = found.convert(two(OLDER, PATCH), None).notices
    assert all(what in notice for what in ("mic setup", "reverb level", "velocity curve"))


def test_wurli_v2_s_vst3_state_lifts_to_its_one_archive_twice_and_passes() -> None:
    source = CATALOG.by_map_id("arturia.wurli-v2")
    assert source is not None
    lifted = source.lift(FormatState(PATCH, PATCH), PluginKind.VST3)
    assert TwoArchives.split(lifted) == (PATCH, PATCH)
    assert convert("arturia.wurli-v2", "arturia.wurli-v3", lifted) == lifted


@pytest.mark.parametrize("map_id", ["arturia.piano-v3", "arturia.wurli-v3"])
def test_a_v3_patch_lowers_to_a_vst2_chunk_and_both_vst3_halves(map_id: str) -> None:
    target = CATALOG.by_map_id(map_id)
    assert target is not None
    patch = two(PATCH, PATCH)
    assert target.lower(patch, PluginKind.VST).state == patch
    vst3 = target.lower(patch, PluginKind.VST3)
    assert vst3.state == vst3.controller == PATCH


@pytest.mark.parametrize("data", [b"", b"\x00" * 64, two(b"not an archive", PATCH), two(OLDER, PATCH)[:-1]])
def test_a_patch_that_is_not_two_archives_is_refused(data: bytes) -> None:
    found = CATALOG.converter("arturia.piano-v2", "arturia.piano-v3")
    assert found is not None
    with pytest.raises(StateTransformError):
        found.convert(data, None)
    source = CATALOG.by_map_id("arturia.wurli-v2")
    assert source is not None
    with pytest.raises(StateTransformError):
        source.lift(FormatState(data, data), PluginKind.VST3)


@pytest.mark.parametrize(
    ("source_id", "kind", "plan"),
    [("arturia.piano-v2", PluginKind.VST, "piano_v3"), ("arturia.wurli-v2", PluginKind.VST3, "wurli_v3")],
)
def test_a_plan_offers_the_v3_as_a_vst2_and_a_vst3(source_id: str, kind: PluginKind, plan: str) -> None:
    mapped = CATALOG.by_map_id(source_id)
    assert mapped is not None
    source = CATALOG.find(mapped.plugin_format(kind).id)
    assert source is mapped
    machine = Machine(vst2=(), vst3={}, au=lambda identifier: False, loadable=lambda info: False)
    targets = {target.id: target for target in mapped_targets(source, kind, machine)}
    assert set(targets) == {f"{plan}_vst2", f"{plan}_vst3"}
    assert targets[f"{plan}_vst3"].route is Route.CONVERTER
