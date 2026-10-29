"""Legacy MS-20 programs, standalone or in a Legacy Cell, into MS-20 V: what crosses, by what evidence, what is said."""

from __future__ import annotations

import math
from typing import Any

import pytest
from test_korg_legacy_cell import POLYSIX, cell, ms20
from test_korg_legacy_cell import bank as cell_bank
from test_korg_ms20 import CORPUS, HOSTED, bank, program
from test_plan import machine

from abletoolz.plugins.converters import korg_ms20 as conversion
from abletoolz.plugins.identity.ids import PluginKind
from abletoolz.plugins.plan.targets import mapped_targets
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.korg_legacy_cell import LEGACY_CELL_VST2, LegacyCellBank
from abletoolz.plugins.state.korg_ms20 import MS20_VST2, Cable, Ms20Program, Ms20Value
from abletoolz.plugins.state.korg_ms20_v import Ms20VPatch
from abletoolz.plugins.state.measured import StateEvidence

DATA = conversion._calibration()
# The plugin's own initial program, as a real set's MS-20 device holds it.
INIT = Ms20Program.parse(program(HOSTED["programs"][0]))


def converted(changes: dict[str, Ms20Value] | None = None, cables: list[Cable] | None = None) -> Any:
    source = INIT.with_values(changes or {}).with_cables(cables or [])
    return conversion.convert(bank(source.raw))


def target(changes: dict[str, Ms20Value] | None = None, cables: list[Cable] | None = None) -> dict[str, Any]:
    return Ms20VPatch.parse(converted(changes, cables).patch).values()


@pytest.mark.parametrize("source", ["korg.ms-20", "korg.legacy-cell"])
def test_both_legacy_plugins_join_ms20_v_and_the_conversion_is_not_predictable(source: str) -> None:
    found = CATALOG.converter(source, "arturia.korg-ms-20-v")
    assert found is not None
    assert found.measured.evidence == (StateEvidence.HOSTED,) and not found.measured.predictable


@pytest.mark.parametrize("stored", [MS20_VST2, LEGACY_CELL_VST2])
def test_a_plan_offers_ms20_v_as_the_format_a_device_can_be_rebuilt_in(stored: Any) -> None:
    """A device is rebuilt as another plugin in VST3 or AU only, so the VST2 device is offered MS-20 V's VST3."""
    source = CATALOG.find(stored)
    assert source is not None
    assert [target.id for target in mapped_targets(source, PluginKind.VST, machine())] == ["korg_ms_20_v_vst3"]


def test_calibrated_controls_land_where_ms20_v_measured_the_same_sound() -> None:
    values = target(
        {
            "Volume": 5.0, "Fine Tune": 2.5, "VCO 2 Tune": -7.0, "MG Frequency": 4.0, "Portamento Time": 8.0,
            "Pitch MG/T.Ext": 3.0,
        }
    )  # fmt: skip
    assert values["Master Volume"] == pytest.approx(-16.75 + 20 * math.log10(0.5), abs=0.01)
    assert values["Master Tune"] == pytest.approx(440 * 2 ** ((50 - 1.65) / 1200), abs=0.01)
    assert values["MS20VCO_FineTuneOsc2"] == pytest.approx(-7.0, abs=1e-5)
    assert values["MG_RateUnSynced"] == pytest.approx(0.109 + 1.8895 * 4, abs=1e-4)
    glide = DATA["glide_seconds"]["target"]
    u = conversion.controls()["Portamento_Time"].normalized(values["Portamento_Time"])
    assert conversion._interp(u, glide) == pytest.approx(0.31, rel=0.01)
    depth = DATA["pitch_depth_cents"]["mg"]["target"]
    assert conversion._interp(values["MS20VCO_FMTotalAmount"], depth) == pytest.approx(386.0, rel=0.01)
    assert values["VCF_Type"] == "MKI"


def test_knob_by_knob_controls_land_on_the_settings_that_sounded_nearest() -> None:
    """Measured positions land on their own setting, and a knob between two lands between them."""
    values = target(
        {
            "LPF Cutoff": 5.0, "HPF Cutoff": 8.0, "VCO 1 Pulse Width": 8.0, "EG 2 Sustain": 0.0, "EG 2 Hold": 2.5,
            "VCO 1 Level": 3.5,
        }
    )  # fmt: skip
    assert values["LP Cutoff"] == pytest.approx(539.9, rel=0.001)
    assert values["HP Cutoff"] == pytest.approx(4213.0, rel=0.001)
    assert values["MS20VCO_PW"] == pytest.approx(17.15, abs=0.01)
    assert values["EG2_Sustain"] == pytest.approx(0.1023, abs=0.0001)
    assert 0.0333 < values["EG2_Classic_Hold"] < 0.0824
    assert 3.406 < values["MS20VCO_VolumeOsc1"] < 4.42


def test_every_knob_by_knob_table_covers_the_whole_knob_inside_ms20_v_s_range() -> None:
    for key, table in DATA["matched"].items():
        knobs = [knob for knob, _ in table["points"]]
        assert knobs == sorted(knobs) and knobs[0] == 0.0 and knobs[-1] == 10.0, key
        for _, setting in table["points"]:
            conversion.controls()[key].normalized(setting)


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        (
            {"VCO 1 Wave Form": "Pulse Width", "VCO 2 Wave Form": "Pulse 50%"},
            {"MS20VCO_WaveformOsc1": "Pulse", "MS20VCO_WaveformOsc2": "Square"},
        ),
        ({"Poly Mode": "Mono", "Trigger Mode": "Single"}, {"Play Mode": "Mono Legato"}),
        ({"Poly Mode": "Mono", "Trigger Mode": "Multi"}, {"Play Mode": "Mono Retrig"}),
        ({"Poly Mode": "Poly", "Poly Voices": 4}, {"Play Mode": "4"}),
        (
            {"MG Tempo Sync": "On", "MG Base Note": "1/16", "MG Times": 1},
            {"MG_MidiSync": "Sync", "MG_RateSynced": "1/16"},
        ),
        (
            {"MG Tempo Sync": "On", "MG Base Note": "1/8T", "MG Times": 1},
            {"MG_RateSynced": "1/8", "MG_RateSyncedType": "Bars Triplet"},
        ),
        ({"MG Key Sync": "On"}, {"MG_Retrig": "Reset"}),
        (
            {"VCO 1 Scale": "16'", "VCO 2 Scale": "8'", "Transpose": 12},
            {"MS20VCO_ScaleOsc1": "8'", "MS20VCO_ScaleOsc2": "4'"},
        ),
        ({"Pitch Bend Range": 7.0}, {"Pitch Bend Range": 7}),
        ({"EG 1 Attack": 6.0}, {"EG1_Attack": 6.0}),
    ],
)
def test_switches_and_printed_positions_cross_as_the_same_setting(
    changes: dict[str, Ms20Value], expected: dict[str, Any]
) -> None:
    values = target(changes)
    for key, value in expected.items():
        assert values[key] == pytest.approx(value) if isinstance(value, float) else values[key] == value, key


def test_cables_cross_by_jack_name_and_the_two_without_a_jack_are_reported() -> None:
    result = converted(
        cables=[
            Cable("MG TRI OUT", "VCO CV IN"),
            Cable("KBD CV OUT", "HPF CUTOFF IN"),
            Cable("PHONES OUT", "ESP SIGNAL IN"),
            Cable("EG1 OUT", "VCA INIGAIN IN"),
        ]
    )
    assert Ms20VPatch.parse(result.patch).cables == (("Mod Gen", "VCO CV"), ("Keyboard CV", "HPF Cutoff"))
    reported = result.notices[-1]
    assert "PHONES OUT to ESP SIGNAL IN" in reported and "EG1 OUT to VCA INIGAIN IN" in reported


def test_what_ms20_v_has_no_counterpart_for_is_reported() -> None:
    reported = converted({"Analog": 3.0, "Pan": -2.0, "Transpose": 7}).notices[-1]
    assert reported.startswith("Not carried:")
    assert "analog drift" in reported and "the pan" in reported and "transpose +7" in reported
    assert "external modulation routes" not in reported
    edited = INIT.with_values({"ExtMod1 > LPF Cutoff": 2.0})
    assert "external modulation routes" in conversion.convert(bank(edited.raw)).notices[-1]


@pytest.mark.parametrize(
    ("changes", "key", "written", "warning"),
    [
        ({"MG Frequency": 10.0}, "MG_RateUnSynced", 17.8, "MG frequency"),
        ({"Poly Mode": "Poly", "Poly Voices": 12}, "Play Mode", "6", "12 voices"),
        ({"EG 2 Decay": 10.0, "EG 2 Sustain": 0.0}, "EG2_Decay", 1.0, "EG2 decay"),
        ({"Pitch EG 1/Ext": 10.0}, "MS20VCO_FMAmount", 1.0, "EG1 pitch modulation"),
        ({"Pitch Bend Range": 0.2}, "Pitch Bend Range", 1, "pitch bend range"),
    ],
)
def test_a_setting_past_ms20_v_s_range_converts_as_near_as_it_can_and_warns(
    changes: dict[str, Ms20Value], key: str, written: Any, warning: str
) -> None:
    result = converted(changes)
    assert any(warning in notice for notice in result.notices)
    value = Ms20VPatch.parse(result.patch).values()[key]
    assert value == pytest.approx(written) if isinstance(written, float) else value == written
    with pytest.raises(StateTransformError, match=warning):
        conversion.plan(INIT.with_values(changes))


def test_a_cell_s_ms20_converts_and_its_effects_and_mixer_are_named() -> None:
    result = conversion.convert_cell(cell_bank(cell("Bass", ms20(), effects=("OD/HiGain + Wah", "No Effect"))))
    reported = result.notices[-1]
    assert "the cell's effects (OD/HiGain + Wah, Reverb Hall)" in reported and "mixer" in reported
    assert result.notices[0] == conversion.NOT_HEARD_NOTE


def test_a_cell_with_a_polysix_beside_its_ms20_converts_the_ms20_and_warns() -> None:
    raw = cell_bank(cell("Layer", ms20(), POLYSIX))
    result = conversion.convert_cell(raw)
    assert any("Polysix in slot B is not carried" in notice for notice in result.notices)
    with pytest.raises(StateTransformError, match="Polysix in slot B"):
        conversion._cell_notices(LegacyCellBank.parse(raw), None)


def test_a_cell_without_an_ms20_is_refused() -> None:
    with pytest.raises(StateTransformError, match="plays Polysix, no MS-20"):
        conversion.convert_cell(cell_bank(cell("Pad", POLYSIX)))


@pytest.mark.skipif(not CORPUS.is_dir(), reason="the banks from real sets live only on the machine that holds them")
def test_every_bank_from_real_sets_converts_or_is_refused_for_holding_no_ms20() -> None:
    banks = sorted(CORPUS.glob("*.state"))
    assert banks
    for path in banks:
        raw = path.read_bytes()
        if len(raw) > 1_000_000:
            if LegacyCellBank.parse(raw).working.slots[0].ms20 is None:
                with pytest.raises(StateTransformError, match="no MS-20"):
                    conversion.convert_cell(raw)
                continue
            Ms20VPatch.parse(conversion.convert_cell(raw).patch)
        else:
            Ms20VPatch.parse(conversion.convert(raw).patch)
