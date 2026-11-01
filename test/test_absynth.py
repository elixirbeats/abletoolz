"""Absynth 3 devices into Absynth 5, which reads an Absynth 3 chunk and migrates it itself."""

from __future__ import annotations

import pathlib

import pytest
from conftest import serum_as, skeleton
from test_plan import machine

from abletoolz.plugins.conversion import as_plugin, write
from abletoolz.plugins.converters.absynth import import_absynth_3
from abletoolz.plugins.identity.ids import PluginKind, Vst2Record
from abletoolz.plugins.identity.vst2_records import live_flags
from abletoolz.plugins.identity.vst3_parameters import no_parameters
from abletoolz.plugins.plan.targets import mapped_targets
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.measured import StateEvidence, StateRung
from abletoolz.plugins.state.native_instruments import ABSYNTH_3_UNIQUE_ID, ABSYNTH_5_UNIQUE_ID

# The opening an Absynth 3 chunk has: the outer block's reversed tag, eight zero bytes, the patch block's
# reversed tag, then a body; the patch name sits 0x44 in.
CHUNK = b"malc" + bytes(8) + b"4ctp" + bytes(52) + b"Synthetic Pad".ljust(32, b"\0") + bytes(64)
ABSYNTH_5 = Vst2Record(
    path=pathlib.Path("C:/Program Files/Native Instruments/VSTPlugins 64 bit/Absynth 5.dll"),
    unique_id=ABSYNTH_5_UNIQUE_ID,
    parameters=21,
    programs=128,
    flags=live_flags(0x131),
    version=5300,
    vst_version=2400,
    category=2,
    chunks=True,
)


def records(unique_id: int) -> Vst2Record | None:
    return ABSYNTH_5 if unique_id == ABSYNTH_5_UNIQUE_ID else None


def test_absynth_3_joins_absynth_5_as_a_migration_the_plugin_does_itself() -> None:
    found = CATALOG.converter("native-instruments.absynth-3", "native-instruments.absynth-5")
    assert found is not None
    assert found.measured.rung is StateRung.VENDOR_COMPAT
    assert found.measured.evidence == (StateEvidence.HOSTED,) and not found.measured.predictable


def test_a_plan_offers_absynth_5_for_an_absynth_3_device() -> None:
    source = CATALOG.find(CATALOG.named("Absynth 3")[0].plugin_format(PluginKind.VST).id)
    assert source is not None
    assert [target.id for target in mapped_targets(source, PluginKind.VST, machine())] == ["absynth_5_vst2"]


def test_an_absynth_3_device_becomes_an_absynth_5_device_holding_the_same_chunk() -> None:
    device = serum_as(skeleton("11.3.42"), "Absynth 3", ABSYNTH_3_UNIQUE_ID, CHUNK)
    (absynth_3,) = CATALOG.named("Absynth 3")
    (absynth_5,) = CATALOG.named("Absynth 5")
    converter = CATALOG.converter(absynth_3.id, absynth_5.id)
    assert converter is not None
    answer = as_plugin(
        device, converter, absynth_3, absynth_5, PluginKind.VST, parameters=no_parameters, records=records
    )
    write(device, answer)
    assert (device.identity.name, device.identity.unique_id) == ("Absynth 5", ABSYNTH_5_UNIQUE_ID)
    assert device.reference.file_name == "Absynth 5.dll"
    assert device.saved_state.state == CHUNK
    assert device.saved_state.unique_id == ABSYNTH_5_UNIQUE_ID
    assert any("migrates" in notice for notice in answer.warnings)


@pytest.mark.parametrize("chunk", [b"", b"malc" + bytes(8) + b"xxxx", CHUNK[4:]])
def test_a_chunk_that_is_not_absynth_3_is_refused(chunk: bytes) -> None:
    with pytest.raises(StateTransformError, match="Absynth 3 chunk"):
        import_absynth_3(chunk)
