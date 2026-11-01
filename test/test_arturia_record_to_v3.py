"""Arturia V2 patch records into their V3s, word by word under the V3's names."""

from __future__ import annotations

import struct

import pytest

from abletoolz.formats.arturia_archive import TwoArchives
from abletoolz.plugins.converters import arturia_v2 as conversion
from abletoolz.plugins.identity.ids import PluginKind
from abletoolz.plugins.plan.machine import Machine
from abletoolz.plugins.plan.model import Route
from abletoolz.plugins.plan.targets import mapped_targets
from abletoolz.plugins.state import arturia_v2
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.errors import StateTransformError

INSTRUMENTS = arturia_v2.INSTRUMENTS


def record(instrument: arturia_v2.Instrument, **words: float) -> bytes:
    """A synthetic record: the first word and tag, zeros, then ``words`` at their offsets."""
    data = bytearray(instrument.chunk_bytes)
    struct.pack_into("<f", data, 0, instrument.version)
    data[4 : 4 + len(instrument.tag)] = instrument.tag
    for name, value in words.items():
        control = arturia_v2.controls(instrument)[name]
        struct.pack_into("<i" if control.integer else "<f", data, control.offset, value)
    return bytes(data)


def convert(instrument: arturia_v2.Instrument, data: bytes) -> dict[str, float]:
    patch = conversion.CONVERSIONS[instrument.map_id](data).patch
    archives = TwoArchives.parse(patch).archives
    assert archives[0].values == archives[1].values
    return archives[0].values


def test_a_word_stored_as_itself_is_copied_and_one_past_its_range_is_said() -> None:
    control = arturia_v2.Control(40)
    assert control.stored(0.25) == (0.25, True)
    assert control.stored(1.5) == (1.0, False)


def test_a_cord_s_source_is_written_as_its_item_and_none_as_the_first() -> None:
    cord = arturia_v2.Control(40, steps=(160, 1))
    assert cord.stored(-1.0) == (0.0, True)
    assert cord.stored(45.0) == (pytest.approx(46 / 159), True)
    assert cord.stored(200.0) == (1.0, False)


# Words the installed V3s were seen to write for these names when hosted, set one at a time, ahead of every word
# the V3 record adds, so the old record holds them at the same byte.
MEASURED = {
    arturia_v2.PROPHET_V2: {
        "COARSE_TUNE_OSC1": 40,
        "CUTOFF_FILTER": 136,
        "ATTACK_ENV_FILTER": 168,
        "DEPTH_CHORUS": 456,
        "AFTERTOUCH_CONTROL": 716,
    },
    arturia_v2.ARP_2600_V2: {
        "WIDTH_OSC1": 36,
        "WIDTH_OSC2": 96,
        "AMOUNT_AM_1_VOLTAGE_PROCESSOR": 604,
        "AMOUNT_1_TRACKING": 620,
        "WIDTH_SEQUENCEUR": 924,
    },
    arturia_v2.MODULAR_V2: {
        "VOLUME_VCA1": 36,
        "VOLUME_VCA2": 88,
        "ACCORD_OSC1": 640,
        "ACCORD_OSC2": 736,
        "TUNE_OSC9": 1396,
    },
}


@pytest.mark.parametrize("instrument", INSTRUMENTS, ids=lambda i: i.name)
def test_a_name_is_read_at_the_word_the_hosted_v3_writes_it_to(instrument: arturia_v2.Instrument) -> None:
    controls = arturia_v2.controls(instrument)
    assert {name: controls[name].offset for name in MEASURED[instrument]} == MEASURED[instrument]


@pytest.mark.parametrize(
    ("word", "said"),
    [(3.4, "MODE_JEUX holds 3.4, not a whole item; written as item 3"), (9.0, "past what Prophet V3 stores")],
)
def test_a_choice_between_two_items_or_past_the_last_is_said_as_such(word: float, said: str) -> None:
    notices: list[str] = []
    value = conversion.plan(record(arturia_v2.PROPHET_V2, MODE_JEUX=word), arturia_v2.PROPHET_V2, notices)["MODE_JEUX"]
    assert any(said in notice for notice in notices)
    assert value == (0.75 if word < 4 else 1.0)


@pytest.mark.parametrize("instrument", INSTRUMENTS, ids=lambda i: i.name)
def test_every_mapped_word_sits_once_inside_the_record_and_names_a_v3_control(
    instrument: arturia_v2.Instrument,
) -> None:
    controls = arturia_v2.controls(instrument)
    offsets = [control.offset for control in controls.values()]
    assert len(set(offsets)) == len(offsets) and min(offsets) >= 4 + len(instrument.tag)
    assert max(offsets) <= instrument.chunk_bytes - 4
    assert set(controls) <= set(TwoArchives.parse(conversion.baseline(instrument.target)).archives[0].values)


@pytest.mark.parametrize("instrument", INSTRUMENTS, ids=lambda i: i.name)
def test_a_record_converts_into_the_v3_default_with_its_words(instrument: arturia_v2.Instrument) -> None:
    name, control = next((n, c) for n, c in arturia_v2.controls(instrument).items() if not c.steps and not c.integer)
    values = convert(instrument, record(instrument, **{name: 0.375}))
    assert values[name] == pytest.approx(0.375)
    default = TwoArchives.parse(conversion.baseline(instrument.target)).archives[0].values
    assert set(values) == set(default)


@pytest.mark.parametrize("instrument", INSTRUMENTS, ids=lambda i: i.name)
def test_a_word_no_setting_holds_keeps_the_default_and_is_said(instrument: arturia_v2.Instrument) -> None:
    name = next(n for n, c in arturia_v2.controls(instrument).items() if not c.integer)
    data = bytearray(record(instrument))
    struct.pack_into("<I", data, arturia_v2.controls(instrument)[name].offset, 0xCDCDCDCE)
    notices: list[str] = []
    assert name not in conversion.plan(bytes(data), instrument, notices)
    assert any(name in notice for notice in notices)
    with pytest.raises(StateTransformError):
        conversion.plan(bytes(data), instrument)


@pytest.mark.parametrize("instrument", INSTRUMENTS, ids=lambda i: i.name)
def test_a_record_the_plugin_did_not_write_is_refused(instrument: arturia_v2.Instrument) -> None:
    good = record(instrument)
    bad = [good[:-4], b"\0\0\0\0" + good[4:]] + ([good[:4] + b"Nope" + good[8:]] if instrument.tag else [])
    for data in bad:
        with pytest.raises(StateTransformError):
            conversion.CONVERSIONS[instrument.map_id](data)


@pytest.mark.parametrize("instrument", INSTRUMENTS, ids=lambda i: i.name)
def test_a_plan_offers_the_v3_as_a_vst2_and_a_vst3_written_as_two_halves(instrument: arturia_v2.Instrument) -> None:
    source = CATALOG.by_map_id(instrument.map_id)
    assert source is not None and CATALOG.find(source.plugin_format(PluginKind.VST).id) is source
    machine = Machine(vst2=(), vst3={}, au=lambda identifier: False, loadable=lambda info: False)
    plan = instrument.target.removeprefix("arturia.").replace("-", "_")
    targets = {target.id: target for target in mapped_targets(source, PluginKind.VST, machine)}
    assert set(targets) == {f"{plan}_vst2", f"{plan}_vst3"} and targets[f"{plan}_vst3"].route is Route.CONVERTER
    patch = conversion.CONVERSIONS[instrument.map_id](record(instrument)).patch
    target = CATALOG.by_map_id(instrument.target)
    assert target is not None
    vst3 = target.lower(patch, PluginKind.VST3)
    assert (vst3.state, vst3.controller) == TwoArchives.split(patch)
