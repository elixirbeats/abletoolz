"""Plugin maps: each plugin found by the id a device stores, its patch crossing its own formats.

Every crossing goes through the catalog and a plugin's map. The Valhalla and
Surge fixtures are native: each format's own readback of one edited patch, taken
with a headless host per format (see ``state_fixtures/audio_units/manifest.json``),
so a crossing that matches them byte for byte is one the plugin accepted.
"""

from __future__ import annotations

import concurrent.futures
import itertools
import pathlib
import struct
import time
from dataclasses import replace

import pytest
from conftest import AU_UNITS, au_fixture

from abletoolz.plugins.container.controller import NO_CONTROLLER_STATE
from abletoolz.plugins.converters.surge import import_surge_1
from abletoolz.plugins.identity.ids import AuId, PluginKind, Vst2Id, Vst3Id
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.audio_unit import ClassInfo
from abletoolz.plugins.state.catalog import Catalog, Converted, Converter
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.fabfilter import (
    EDITOR_STATE_VERSION,
    FABFILTER_CONTROLLER_TRAILER,
    FABFILTER_PROCESSOR_TRAILER,
    NO_INSTANCE,
    PRO_Q3_DEFAULT_PARAMETERS,
    PRO_Q3_EDITOR_MAGIC,
    SATURN_2_EDITOR_MAGIC,
    TIMELESS_3_EDITOR_MAGIC,
    EditorState,
    FfbsState,
)
from abletoolz.plugins.state.framings import VERBATIM
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import FormatState, PluginFormat, PluginMap
from abletoolz.plugins.state.surge_synth_team import SURGE_1_UNIQUE_ID

AU = PluginKind.AU
VST2 = PluginKind.VST
VST3 = PluginKind.VST3

WITH_VST2 = {slug: au for slug, au in AU_UNITS.items() if slug.startswith("valhalla")}


def plugin(stored: Vst2Id | Vst3Id | AuId) -> PluginMap:
    found = CATALOG.find(stored)
    assert found is not None, stored
    return found


def cross(stored: Vst2Id | Vst3Id | AuId, state: bytes, source: PluginKind, target: PluginKind) -> FormatState:
    return plugin(stored).cross(FormatState(state), source, target)


# -- finding a plugin ----------------------------------------------------------


def test_every_format_of_every_plugin_finds_its_map() -> None:
    """By the id a device stores and by the name it stores, and the map by its own id."""
    for plugin_map in CATALOG:
        assert CATALOG.by_map_id(plugin_map.id) is plugin_map
        for item in plugin_map.formats.values():
            assert plugin(item.id) is plugin_map, item
            assert plugin_map in CATALOG.named(item.name), item


def test_a_plugin_nobody_mapped_is_not_found() -> None:
    assert CATALOG.find(Vst2Id(1)) is None


def _map(map_id: str, stored: Vst2Id, plan_name: str | None = None) -> PluginMap:
    measured = MeasuredState(StateRung.VERBATIM, (StateEvidence.HOSTED,))
    vst2 = PluginFormat(stored, map_id, VERBATIM, measured, plan_name=plan_name or f"{map_id}_vst2")
    return PluginMap(map_id, map_id, None, "fixture", vst2=vst2)


@pytest.mark.parametrize(
    "second",
    [_map("one", Vst2Id(2)), _map("two", Vst2Id(1)), _map("two", Vst2Id(2), plan_name="one_vst2")],
    ids=["same map id", "same stored id", "same plan name"],
)
def test_two_plugins_claiming_one_identity_are_refused(second: PluginMap) -> None:
    """A plan file names a target by its plan name, so two formats sharing one would make convert_to ambiguous."""
    catalog = Catalog()
    catalog.register(lambda: _map("one", Vst2Id(1)))
    catalog.register(lambda: second)
    with pytest.raises(StateTransformError, match="registered as|both claim|both use the plan name"):
        catalog.find(Vst2Id(1))


def test_a_plugin_registered_after_the_catalog_is_built_is_refused() -> None:
    catalog = Catalog()
    catalog.register(lambda: _map("one", Vst2Id(1)))
    catalog.find(Vst2Id(1))
    with pytest.raises(StateTransformError, match="already built"):
        catalog.register(lambda: _map("two", Vst2Id(2)))


def _converter(source: str, target: str) -> Converter:
    measured = MeasuredState(StateRung.RE_ENCODE, (StateEvidence.HOSTED,))
    return Converter(source, target, lambda patch, program: Converted(patch), measured)


def test_a_second_translation_for_one_pair_is_refused() -> None:
    catalog = Catalog()
    catalog.add_converter(_converter("one", "two"))
    with pytest.raises(StateTransformError, match="Two converters"):
        catalog.add_converter(_converter("one", "two"))
    catalog.add_converter(_converter("two", "one"))


def test_a_translation_naming_a_plugin_nothing_registers_is_refused() -> None:
    catalog = Catalog()
    catalog.register(lambda: _map("one", Vst2Id(1)))
    catalog.add_converter(_converter("one", "missing"))
    with pytest.raises(StateTransformError, match="'missing', which nothing registers"):
        catalog.converter("one", "missing")


def test_a_translation_into_the_same_plugin_is_its_map_s_job() -> None:
    with pytest.raises(StateTransformError, match="into itself"):
        Catalog().add_converter(_converter("one", "one"))


SHIPPED = {
    ("fabfilter.pro-q-1", "fabfilter.pro-q-3"),
    ("fabfilter.pro-q-2", "fabfilter.pro-q-3"),
    ("izotope.trash1", "izotope.trash2"),
    ("ni.pro-53", "arturia.prophet-5-v"),
    ("surge-synth-team.surge-1", "surge-synth-team.surge-xt"),
}


def test_every_shipped_translation_is_found_from_its_two_plugins_and_goes_one_way() -> None:
    assert {(converter.source, converter.target) for converter in CATALOG.converters()} == SHIPPED
    for source, target in SHIPPED:
        converter = CATALOG.converter(source, target)
        assert converter is not None
        assert converter.measured.date is not None
        assert CATALOG.converter(target, source) is None


def test_the_trash_translation_needs_the_program_the_device_plays() -> None:
    converter = CATALOG.converter("izotope.trash1", "izotope.trash2")
    assert converter is not None
    source = (
        pathlib.Path(__file__).parent / "state_fixtures" / "trash" / "trash-conversion-01-source.state"
    ).read_bytes()
    with pytest.raises(StateTransformError, match="which of its bank's programs"):
        converter.convert(source, None)


def test_a_format_must_hold_its_own_kind_of_id() -> None:
    measured = MeasuredState(StateRung.VERBATIM, (StateEvidence.HOSTED,))
    with pytest.raises(StateTransformError, match="holds a Vst2Id"):
        PluginMap("x", "x", None, "fixture", vst3=PluginFormat(Vst2Id(1), "x", VERBATIM, measured, plan_name="x_vst3"))


@pytest.mark.parametrize("change", ["no_formats", "bad_digest", "duplicate_digest", "blank_scope"])
def test_a_plugin_map_refuses_ambiguous_identity_and_scope(change: str) -> None:
    measured = MeasuredState(StateRung.VERBATIM, (StateEvidence.HOSTED,))
    knowledge = PluginMap(
        "x", "x", None, "fixture", vst2=PluginFormat(Vst2Id(1), "x", VERBATIM, measured, plan_name="x_vst2")
    )
    with pytest.raises(StateTransformError):
        match change:
            case "no_formats":
                replace(knowledge, vst2=None)
            case "bad_digest":
                replace(knowledge, binary_sha256=("not-a-sha256",))
            case "duplicate_digest":
                replace(knowledge, binary_sha256=("a" * 64, "a" * 64))
            case "blank_scope":
                replace(knowledge, scope=" ")


# -- JUCE plugins across AU, VST2 and VST3 -------------------------------------


def saved_formats(slug: str) -> dict[PluginKind, bytes]:
    """One edited patch as each format the plugin was measured in saved it."""
    saved = {AU: au_fixture(slug, "au.plist"), VST3: au_fixture(slug, "vst3.state")}
    if slug in WITH_VST2:
        saved[VST2] = au_fixture(slug, "vst2.chunk")
    return saved


@pytest.mark.parametrize(("slug", "au"), AU_UNITS.items())
def test_each_format_s_patch_crosses_into_every_other_as_the_plugin_read_it_back(slug: str, au: AuId) -> None:
    """An AU preset is compared by what it holds -- the plugin's identifier and its patch -- the rest byte for byte.

    The VST2 chunk is the AU's patch, and none of these plugins keeps a VST3 controller half.
    """
    saved = saved_formats(slug)
    for source, target in itertools.permutations(saved, 2):
        crossed = cross(au, saved[source], source, target)
        if target is AU:
            written = ClassInfo.parse(crossed.state)
            assert written.identifier == au.identifier, (source, target)
            assert written.plugin_state == ClassInfo.parse(saved[AU]).plugin_state, (source, target)
        else:
            assert crossed.state == saved[target], (source, target)
        if target is VST3:
            assert crossed.controller == b"", (source, target)


def test_a_plugin_with_no_vst2_build_refuses_one() -> None:
    with pytest.raises(StateTransformError, match="no measured vst format"):
        cross(AU_UNITS["surge-xt"], au_fixture("surge-xt", "au.plist"), AU, VST2)


def test_a_vst3_state_outside_its_envelope_is_refused() -> None:
    with pytest.raises(StateTransformError, match="VstW"):
        cross(AU_UNITS["valhalla-supermassive"], au_fixture("surge-xt-effects", "vst3.state"), VST3, AU)


def test_a_vst3_state_without_the_trailer_is_refused() -> None:
    with pytest.raises(StateTransformError, match="trailer"):
        cross(AU_UNITS["surge-xt-effects"], b"<surgefx/>", VST3, AU)


# -- Surge 1.x into Surge XT ---------------------------------------------------


def legacy_surge(revision: int = 10) -> bytes:
    """A Surge 1.x chunk's shape: the magic, the XML's size, padding, then the patch XML."""
    xml = f'<?xml version="1.0" encoding="UTF-8" standalone="yes" ?><patch revision="{revision}"></patch>'.encode()
    return b"sub3" + len(xml).to_bytes(4, "little") + bytes(24) + xml


def test_a_surge_1_patch_goes_into_a_surge_xt_au_preset_as_it_is() -> None:
    imported = import_surge_1(legacy_surge())
    surge_xt = plugin(AU_UNITS["surge-xt"])
    written = ClassInfo.parse(surge_xt.plugin_format(AU).framing.lower(imported.patch))
    assert written.plugin_state == legacy_surge()
    assert any("voice limit" in notice for notice in imported.notices)
    assert SURGE_1_UNIQUE_ID not in {getattr(item.id, "unique_id", None) for item in surge_xt.formats.values()}


def test_a_chunk_that_is_not_surge_1_is_refused() -> None:
    with pytest.raises(StateTransformError, match="opens with"):
        import_surge_1(b"sub2" + legacy_surge()[4:])


@pytest.mark.parametrize(
    ("chunk", "revision"), [(legacy_surge(revision=9), "revision 9"), (b"sub3" + bytes(28), "revision None")]
)
def test_a_surge_1_revision_nobody_measured_converts_and_says_to_listen(chunk: bytes, revision: str) -> None:
    imported = import_surge_1(chunk)
    assert imported.patch == chunk
    assert f"Surge XT was not measured importing Surge 1.x {revision}; listen to it first" in imported.notices


# -- FabFilter -----------------------------------------------------------------

FFBS_EDITORS = {
    "Pro-Q 3": PRO_Q3_EDITOR_MAGIC,
    "Saturn 2": SATURN_2_EDITOR_MAGIC,
    "Timeless 3": TIMELESS_3_EDITOR_MAGIC,
}


def ffbs_chunk(magic: bytes, version: int = 1) -> tuple[bytes, bytes]:
    """An FFBS VST2 chunk -- the processor's state, then the editor's -- and its editor half."""
    editor = EditorState(
        magic=magic,
        version=EDITOR_STATE_VERSION,
        preset_name="Air",
        instance_index=NO_INSTANCE,
        label="",
        controllers=(),
    ).encode()
    processor = FfbsState(version=version, parameters=PRO_Q3_DEFAULT_PARAMETERS, tail=b"").encode()
    return processor + editor, editor


def ffbs_plugin(name: str) -> PluginMap:
    (found,) = CATALOG.named(name)
    return found


@pytest.mark.parametrize(("name", "magic"), FFBS_EDITORS.items())
def test_an_ffbs_chunk_splits_into_its_vst3_halves(name: str, magic: bytes) -> None:
    """The processor half gains FabFilter's trailer; the editor half, whole, becomes the controller state."""
    chunk, editor = ffbs_chunk(magic)
    crossed = ffbs_plugin(name).cross(FormatState(chunk), VST2, VST3)
    assert crossed.state == chunk[: chunk.index(magic)] + FABFILTER_PROCESSOR_TRAILER
    assert crossed.controller == editor + FABFILTER_CONTROLLER_TRAILER


def test_another_product_s_editor_half_is_not_carried() -> None:
    """A Saturn 2 editor half is not Pro-Q 3's to carry."""
    chunk, _ = ffbs_chunk(SATURN_2_EDITOR_MAGIC)
    with pytest.raises(StateTransformError, match="does not open with"):
        ffbs_plugin("Pro-Q 3").cross(FormatState(chunk), VST2, VST3)


def test_a_pro_q_3_layout_nobody_recorded_is_refused() -> None:
    chunk, _ = ffbs_chunk(PRO_Q3_EDITOR_MAGIC, version=2)
    with pytest.raises(StateTransformError, match="no recorded layout"):
        ffbs_plugin("Pro-Q 3").cross(FormatState(chunk), VST2, VST3)


def test_a_vst3_whose_controller_half_nobody_measured_is_not_written() -> None:
    """Volcano 3's processor half is measured; its controller half is not."""
    volcano_3 = plugin(Vst2Id(1180054380))
    assert not volcano_3.can_write(VST3)
    with pytest.raises(StateTransformError, match="controller half is not measured"):
        volcano_3.cross(FormatState(b"FFBS"), VST2, VST3)


# -- iZotope -------------------------------------------------------------------


@pytest.mark.parametrize("vst2_id", [1517179954, 1515141464], ids=["Trash 2", "Ozone 9 Exciter"])
def test_an_izotope_vst3_is_not_written_without_its_controller_half(vst2_id: int) -> None:
    """Trash 2 reverts to its defaults without one, and neither plugin's is measured."""
    assert not plugin(Vst2Id(vst2_id)).can_write(VST3)


def test_an_izotope_chunk_is_the_plugin_s_patch_and_holds_the_vst3_state() -> None:
    """The chunk is the patch because it holds the name too; the VST3 state is inside it."""
    ozone = plugin(Vst2Id(1515141464))
    state = b"the vst3 state"
    chunk = len(state).to_bytes(4, "little") + state + (7).to_bytes(4, "little") + b"Default"
    assert ozone.lift(FormatState(chunk), VST2) == chunk
    assert ozone.plugin_format(VST3).framing.lower(chunk) == state
    with pytest.raises(StateTransformError, match="preset name"):
        ozone.plugin_format(VST3).framing.lift(state)
    with pytest.raises(StateTransformError, match="no recorded layout"):
        ozone.lift(FormatState(b"not a wrapper"), VST2)


# -- Kilohearts ----------------------------------------------------------------


@pytest.mark.parametrize("zipped", [b"PK\x03\x04zip", b"PK\x03\x04" + bytes(300)], ids=["short", "long"])
def test_every_kilohearts_plugin_wraps_its_zip_for_vst3(zipped: bytes) -> None:
    """The VST3 holds the VST2's zip behind two little-endian uint32, (1, length), and crosses back to it."""
    wrapped = struct.pack("<II", 1, len(zipped)) + zipped
    khs = [item for item in CATALOG if item.vendor == "Kilohearts"]
    assert len(khs) == 36
    for plugin_map in khs:
        stored = plugin_map.plugin_format(VST2).id
        assert cross(stored, zipped, VST2, VST3) == FormatState(wrapped, b"")
        assert cross(stored, wrapped, VST3, VST2).state == zipped


def test_only_the_kilohearts_plugins_somebody_heard_are_predictable() -> None:
    heard = {
        item.name for item in CATALOG if item.vendor == "Kilohearts" and item.plugin_format(VST3).measured.predictable
    }
    assert heard == {"kHs Distortion", "kHs Filter", "kHs Stereo"}


# -- what a plugin with nothing to say does ------------------------------------


def test_a_plugin_that_writes_no_controller_half_writes_an_empty_one() -> None:
    serum = plugin(Vst2Id(1483109208))
    assert serum.plugin_format(VST3).controller is NO_CONTROLLER_STATE
    assert serum.cross(FormatState(b"patch"), VST2, VST3) == FormatState(b"patch", b"")


def test_a_catalog_first_used_by_several_threads_at_once_builds_once() -> None:
    """Sets are processed on threads; a second thread must never see half an index."""
    catalog = Catalog()
    for index in range(40):

        def slow(index: int = index) -> PluginMap:
            time.sleep(0.001)
            return _map(f"map-{index}", Vst2Id(index))

        catalog.register(slow)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        found = list(pool.map(lambda index: catalog.find(Vst2Id(index)), range(40)))
    assert [plugin_map.id for plugin_map in found if plugin_map is not None] == [f"map-{i}" for i in range(40)]
