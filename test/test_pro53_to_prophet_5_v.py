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
    # Cutoff 70 of 127 is 66.1 st, between the matched 60.5 st (224 Hz) and 68.0 st (358 Hz).
    assert 224.0 < values["Prophet5VCF_Freq"] < 358.0  # type: ignore[operator]
    assert converted.notices[0] == conversion.NOT_HEARD_NOTE


def test_what_prophet_5_v_has_no_counterpart_for_is_reported_and_left_out() -> None:
    converted = conversion.convert(with_values(Analog=40.0, **{"Velocity_on/off": True}))
    reported = converted.notices[-1]
    assert reported.startswith("Not carried:") and "analog drift" in reported and "velocity" in reported
    assert Prophet5Patch.parse(converted.patch).values()["FX_Enable"] is False


# A delay inside Prophet-5 V's ranges: 149 ms, 50% feedback, half wet, cuts at 60 and 72 st.
DELAY = {
    "Delay_Effect_On": True,
    "Delay_Effect_Time": 149.0,
    "Delay_Effect_Feedback": 50.0,
    "Delay_Effect_Wet": 50.0,
    "Delay_Effect_Low_Cut": 60.0,
    "Delay_Effect_High_Cut": 72.0,
}


def test_the_delay_goes_to_fx_slot_1_by_its_rendered_points() -> None:
    converted = conversion.convert(with_values(**DELAY))
    values = Prophet5Patch.parse(converted.patch).values()
    assert values["FX_Enable"] is True and values["FX1_ModuleType"] == "Delay" and values["FX1_Bypass"] is True
    # Pro-53's knobs land on 127 steps: 149.0 ms, 50.4% feedback and wet.
    assert values["FX1_Delay_DelayTimeMs"] == pytest.approx(149.0, abs=0.1)
    assert values["FX1_Delay_Feedback"] == pytest.approx(0.504, abs=0.001)
    # The cuts' half-power corners as rendered: 60.47 st low cut at 258 Hz, 72.13 st high cut at 566 Hz.
    assert values["FX1_Delay_HPFreq"] == pytest.approx(258.3, rel=0.01)
    assert values["FX1_Delay_LPFreq"] == pytest.approx(566.0, rel=0.01)
    # Half wet on Pro-53's equal-power mix: echo level with the dry, both 3 dB down, made up in the volume.
    mix, gain = conversion._wet(100 * 64 / 127)
    assert values["FX1_Dry_Wet"] == pytest.approx(mix) and 50 < mix < 51
    calibrated = controls()["Volume"].value(conversion._calibration()["master_volume"])
    assert values["Volume"] == pytest.approx(calibrated + gain) and gain == pytest.approx(-2.96, abs=0.01)
    assert converted.notices[4:] == ("Not carried: the saved pitch bend (0.00, centre is 0.50).",)


@pytest.mark.parametrize(
    ("percent", "mix", "gain"), [(0.0, 0.0, 0.0), (25.0, 20.7, -0.69), (75.0, 79.3, -0.69), (100.0, 100.0, 0.0)]
)
def test_the_mix_keeps_pro_53_s_echo_to_dry_ratio(percent: float, mix: float, gain: float) -> None:
    assert conversion._wet(percent) == pytest.approx((mix, gain), abs=0.05)


def test_master_volume_is_a_straight_gain_around_50_percent() -> None:
    calibrated = float(controls()["Volume"].value(conversion._calibration()["master_volume"]))
    for percent, offset in ((100.0, 6.02), (25.0, -6.02)):
        values = Prophet5Patch.parse(conversion.convert(with_values(Master_Volume=percent)).patch).values()
        assert values["Volume"] == pytest.approx(calibrated + offset, abs=0.01)


def test_the_delay_s_invert_modulation_and_midi_sync_are_reported() -> None:
    converted = conversion.convert(
        with_values(**DELAY, Delay_Effect_Invert=True, Delay_Effect_MIDI_Sync=True, Delay_Effect_Depth=0.001)
    )
    reported = converted.notices[-1]
    assert "invert" in reported and "MIDI sync" in reported and "time modulation" in reported


@pytest.mark.parametrize(
    ("changes", "wheel", "warned"),
    [
        ({"Modulation_Wheel": 50.0, "WheelMod_Dest_PWidth_A": True}, 0.504, False),
        ({"Modulation_Wheel": 25.0, "WheelMod_Dest_Freq_A": True}, 0.504, False),
        ({"Modulation_Wheel": 75.0, "WheelMod_Dest_Freq_B": True}, 1.0, True),
        ({"Modulation_Wheel": 50.0, "WheelMod_Dest_Freq_A": True, "WheelMod_Dest_Filter": True}, 0.504, True),
    ],
)
def test_the_saved_wheel_is_written_where_it_moves_its_destinations_as_far(
    changes: dict[str, object], wheel: float, warned: bool
) -> None:
    converted = conversion.convert(with_values(**changes))
    assert Prophet5Patch.parse(converted.patch).values()["VST3_CtrlModWheel"] == pytest.approx(wheel, abs=0.005)
    assert any("half as far" in notice for notice in converted.notices) is warned


def test_resonance_and_the_filter_sustain_cross_by_their_coarse_evidence() -> None:
    values = Prophet5Patch.parse(conversion.convert(with_values(Filter_Resonance=100.0, Filter_Sustain=25.0)).patch)
    # 100% resonance rose 47 dB on Pro-53, between Prophet-5 V's 8 (45 dB) and 10 (50 dB).
    assert 8.0 < values.values()["Prophet5VCF_Res"] < 10.0  # type: ignore[operator]
    # Pro-53 knobs hold 127 steps, so 25% lands on 25.2%.
    assert values.values()["VCF_Prophet5ADSR_Sustain"] == pytest.approx(0.252, abs=0.001)


def test_matched_controls_take_the_value_that_sounded_nearest() -> None:
    changes = {
        "Filter_Envelope_Amount": 48.0,
        "Mixer_Oscillator_B": 50.0,
        "Amplifier_Sustain": 50.0,
        "Amplifier_Release": 70.0,
    }
    values = Prophet5Patch.parse(conversion.convert(with_values(**changes)).patch).values()
    # Each sits between its two matched neighbours: 48.4 st of envelope at 3.54, the rest interpolated.
    assert values["Prophet5VCF_EnvAmt"] == pytest.approx(3.54, abs=0.01)
    assert 4.088 < values["Prophet5VCO_Osc2Level"] < 5.669  # type: ignore[operator]
    # Prophet-5 V holds a little lower than Pro-53 at the same sustain fraction.
    assert 0.4338 < values["VCA_Prophet5ADSR_Sustain"] < 0.5406  # type: ignore[operator]
    # Prophet-5 V's release runs longer than the half-way calibration put it, to end where Pro-53's does.
    assert 1.116 < values["VCA_Prophet5ADSR_Release"] < 3.266  # type: ignore[operator]


def test_noise_louder_than_prophet_5_v_s_goes_is_written_at_full_and_warns() -> None:
    quiet = conversion.convert(with_values(Mixer_Noise=25.0))
    assert Prophet5Patch.parse(quiet.patch).values()["Prophet5VCO_NoiseLevel"] == pytest.approx(8.24, abs=0.02)
    assert not any("louder than" in notice for notice in quiet.notices)
    loud = conversion.convert(with_values(Mixer_Noise=100.0))
    assert Prophet5Patch.parse(loud.patch).values()["Prophet5VCO_NoiseLevel"] == pytest.approx(10.0)
    assert any("about 8 dB quieter" in notice for notice in loud.notices)


def test_a_cutoff_below_the_matched_span_falls_back_to_its_fitted_corner() -> None:
    low = conversion.convert(with_values(Filter_Cutoff=10.0))
    expected = min(max(conversion._pro53_cutoff_hz(120 * 11 / 127), 6.9), 6214.0)
    assert Prophet5Patch.parse(low.patch).values()["Prophet5VCF_Freq"] == pytest.approx(expected, rel=0.01)


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
        ({**DELAY, "Delay_Effect_Spread": 39.0}, "FX1_Delay_StereoWidth", 0.2, "spread"),
        ({**DELAY, "Delay_Effect_High_Cut": 40.0}, "FX1_Delay_LPFreq", 250.0, "high cut"),
        ({**DELAY, "Delay_Effect_Time": 1.0}, "FX1_Delay_DelayTimeMs", 2.0, "delay time"),
        ({"Master_Volume": 0.0}, "Volume", -60.0, "master volume"),
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
