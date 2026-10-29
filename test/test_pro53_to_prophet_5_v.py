"""Pro-53's working patch into Prophet-5 V: what crosses and by what evidence, what is reported, what warns."""

from __future__ import annotations

import os
import pathlib

import pytest
from test_pro53 import PLAIN, bank

from abletoolz.plugins.converters import pro53 as conversion
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.measured import StateEvidence
from abletoolz.plugins.state.pro53 import CONTROL_BY_NAME, Pro53Bank
from abletoolz.plugins.state.prophet_5_v import Prophet5Patch, controls

# PLAIN with both pulse widths at the middle: everything in it is measured or at a shared end.
CARRIED = {**PLAIN, 8: 64 / 127, 44: 64 / 127}
CORPUS = pathlib.Path(
    os.environ.get(
        "ABLETOOLZ_PRO53_BANKS",
        pathlib.Path(__file__).parents[1] / "unfinished_sandbox/format_translation/pro53_source_20261021/corpus",
    )
)


def with_values(**changes: object) -> bytes:
    edits = {name.replace("_", " "): value for name, value in changes.items()}
    return Pro53Bank.parse(bank(CARRIED)).with_values(edits).encode()  # type: ignore[arg-type]


def test_the_converter_joins_the_two_maps_and_is_not_predictable() -> None:
    found = CATALOG.converter("ni.pro-53", "arturia.prophet-5-v")
    assert found is not None
    assert found.measured.evidence == (StateEvidence.HOSTED,) and not found.measured.predictable


def test_a_patch_of_measured_and_shared_controls_converts_and_reads_back_in_prophet_5_v_s_units() -> None:
    converted = conversion.convert(bank(CARRIED))
    values = Prophet5Patch.parse(converted.patch).values()
    assert values["Prophet5VCO_Freq1"] == 0 and values["Prophet5VCO_SawOn1"] is True
    assert values["Prophet5VCO_Osc1Level"] == pytest.approx(10.0) and values["Prophet5VCO_Osc2Level"] == 0.0
    assert values["Prophet5VCO_PW1"] == pytest.approx(100 * 64 / 127)
    assert values["Prophet5Lfo_RateUnSynced"] == pytest.approx(CONTROL_BY_NAME["LFO Frequency"].curve.value(0.0))
    assert values["VCA_Prophet5ADSR_Sustain"] == pytest.approx(1.0)
    # Cutoff 70 of 127 sits between the calibration's 64 and 79.
    cutoff = controls()["Prophet5VCF_Freq"]
    assert cutoff.value(0.5115) < values["Prophet5VCF_Freq"] < cutoff.value(0.6395)  # type: ignore[operator]
    assert converted.notices[0] == conversion.NOT_HEARD_NOTE


def test_what_prophet_5_v_has_no_counterpart_for_is_reported_and_left_out() -> None:
    converted = conversion.convert(with_values(Delay_Effect_On=True, Analog=40.0, Master_Volume=80.0))
    reported = converted.notices[-1]
    assert "the delay effect" in reported and "analog drift" in reported and "master volume 80%" in reported


def test_resonance_envelope_amount_and_levels_cross_by_their_coarse_evidence() -> None:
    values = Prophet5Patch.parse(
        conversion.convert(
            with_values(
                Filter_Resonance=100.0, Filter_Envelope_Amount=48.0, Mixer_Oscillator_B=50.0, Filter_Sustain=25.0
            )
        ).patch
    ).values()
    # 100% resonance rose 47 dB on Pro-53, between Prophet-5 V's 8 (45 dB) and 10 (50 dB).
    assert 8.0 < values["Prophet5VCF_Res"] < 10.0  # type: ignore[operator]
    # 48 st of envelope moved Pro-53's cutoff about 46 st, Prophet-5 V's 4.
    assert values["Prophet5VCF_EnvAmt"] == pytest.approx(4.0, abs=0.2)
    # Pro-53 knobs hold 127 steps, so 50% lands on 50.4%.
    assert values["Prophet5VCO_Osc2Level"] == pytest.approx(5.0, abs=0.05)
    assert values["VCF_Prophet5ADSR_Sustain"] == pytest.approx(0.25, abs=0.005)


def test_a_cutoff_past_prophet_5_v_s_top_is_written_at_its_top_and_reported() -> None:
    converted = conversion.convert(with_values(Filter_Cutoff=120.0))
    assert Prophet5Patch.parse(converted.patch).values()["Prophet5VCF_Freq"] == pytest.approx(6214.0)
    assert "cutoff above Prophet-5 V's 6214 Hz" in converted.notices[-1]


def test_glide_outside_unison_is_reported_and_inside_unison_carried_with_a_warning() -> None:
    assert "glide 50%" in conversion.convert(with_values(Glide_Time=50.0)).notices[-1]
    converted = conversion.convert(with_values(Glide_Time=63.0, Unisono_Mode=True))
    # Pro-53's glide reached half way in 0.113 s at 63% in unison.
    assert Prophet5Patch.parse(converted.patch).values()["Glider_Time"] == pytest.approx(0.113, abs=0.002)
    assert any("glide in unison" in notice for notice in converted.notices)
    # Below 37% Pro-53's glide did not move at all in unison: nothing to carry or warn about.
    quiet = conversion.convert(with_values(Glide_Time=30.0, Unisono_Mode=True))
    assert not any("glide" in notice for notice in quiet.notices)


@pytest.mark.parametrize(
    ("changes", "key", "written", "warning"),
    [
        ({"Filter_HPF-Mode": True}, "Prophet5VCF_Freq", 6214.0, "high-pass"),
        ({"Filter_Envelope_Invert": True, "Filter_Envelope_Amount": 48.0}, "Prophet5VCF_EnvAmt", 0.0, "invert"),
        ({"Release_on/off": False}, "VCA_Prophet5ADSR_Release", 0.004, "release switch"),
        ({"PolyMod_Source_Filt_Env": 50.0, "PolyMod_Dest_Filter": True}, "PolyModFilterEnvAmount", 5.04, "Poly-Mod"),
        ({"Oscillator_A_PulseWidth": 0.0}, "Prophet5VCO_PW1", controls()["Prophet5VCO_PW1"].low, "below"),
        ({"LFO_MIDI_Sync": True}, None, None, "LFO MIDI sync"),
    ],
)
def test_a_control_with_no_counterpart_converts_as_near_as_it_can_and_warns(
    changes: dict[str, object], key: str | None, written: float | None, warning: str
) -> None:
    patch = with_values(**changes)
    converted = conversion.convert(patch)
    assert any(warning in notice for notice in converted.notices)
    if key is not None:
        assert Prophet5Patch.parse(converted.patch).values()[key] == pytest.approx(written, abs=0.01)
    # Called directly without notices, the same setting refuses the conversion.
    with pytest.raises(StateTransformError, match=warning):
        conversion.plan(Pro53Bank.parse(patch))


@pytest.mark.skipif(not CORPUS.is_dir(), reason="the owner's Pro-53 banks live on the machine that holds the sets")
def test_every_one_of_the_owner_s_banks_converts() -> None:
    banks = sorted(CORPUS.glob("*.state"))
    assert banks
    for path in banks:
        Prophet5Patch.parse(conversion.convert(path.read_bytes()).patch)
