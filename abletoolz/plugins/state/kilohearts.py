"""Kilohearts' plugins.

Every kHs plugin's VST2 chunk is a zip holding its ``state.json``, and its VST3
holds the same zip behind two little-endian words, ``(1, length)``. Heard on kHs
Distortion and kHs Filter on 2026-08-10 and kHs Stereo on 2026-08-15, and found
the same in every kHs VST3 the corpus survey read; the rest of the range carry
that structural evidence, which labels them as needing a listen. None writes a
VST3 controller half.

The ids are what the user's sets and Live's plugin database store, and the two
agree for every plugin both hold.
"""

from __future__ import annotations

import dataclasses
import datetime
import functools

from abletoolz.plugins.container.controller import NO_CONTROLLER_STATE
from abletoolz.plugins.identity.ids import Vst2Id, Vst3Id
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.framings import VERBATIM, kilohearts_unwrap, kilohearts_wrap
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import PluginFormat, PluginMap


@dataclasses.dataclass(frozen=True, slots=True)
class KiloheartsHeader:
    """A Kilohearts VST3: the VST2 zip behind two words, ``(1, length)``."""

    def lift(self, state: bytes) -> bytes:
        payload = kilohearts_unwrap(state)
        if payload is None:
            raise StateTransformError("A Kilohearts VST3 state opens with (1, length), and this one does not")
        return payload

    def lower(self, patch: bytes) -> bytes:
        return kilohearts_wrap(patch)


_HEARD = {
    "Distortion": MeasuredState(StateRung.REFRAME, (StateEvidence.EAR,), datetime.date(2026, 8, 10)),
    "Filter": MeasuredState(StateRung.REFRAME, (StateEvidence.EAR,), datetime.date(2026, 8, 10)),
    "Stereo": MeasuredState(StateRung.REFRAME, (StateEvidence.EAR,), datetime.date(2026, 8, 15)),
}
_STRUCTURAL = MeasuredState(StateRung.REFRAME, (StateEvidence.STRUCTURAL,))

# (name after "kHs ", VST2 UniqueId, VST3 class id)
_PLUGINS: tuple[tuple[str, int, tuple[int, int, int, int], tuple[str, str]], ...] = (
    ("3-Band EQ", 1802727781, (-694766222, 3359932, 7521461, 10558482), ("khs_3_band_eq_vst2", "khs_3_band_eq_vst3")),
    ("Bitcrush", 1802723939, (1912498557, 12601455, 4795924, 5368217), ("khs_bitcrush_vst2", "khs_bitcrush_vst3")),
    (
        "Channel Mixer",
        1802724205,
        (-1203053380, 4277667, 1950508, 1750242),
        ("khs_channel_mixer_vst2", "khs_channel_mixer_vst3"),
    ),
    ("Chorus", 1802724200, (344807275, 1984018, 4780697, 5974798), ("khs_chorus_vst2", "khs_chorus_vst3")),
    ("Clipper", 1801677936, (-2042449466, 3621737, 3134669, 10730883), ("khs_clipper_vst2", "khs_clipper_vst3")),
    (
        "Comb Filter",
        1802724198,
        (1543289216, 81964, 12040936, 3817390),
        ("khs_comb_filter_vst2", "khs_comb_filter_vst3"),
    ),
    (
        "Compactor",
        1802724195,
        (-1732840348, 10175121, 11477019, 12307647),
        ("khs_compactor_vst2", "khs_compactor_vst3"),
    ),
    ("Compressor", 1802724208, (883669104, 6440993, 4006517, 12940371), ("khs_compressor_vst2", "khs_compressor_vst3")),
    ("Convolver", 1802724207, (-522825550, 10240212, 11463663, 6446811), ("khs_convolver_vst2", "khs_convolver_vst3")),
    ("Delay", 1802724460, (1569784016, 9061824, 12922386, 12294591), ("khs_delay_vst2", "khs_delay_vst3")),
    (
        "Distortion",
        1802724468,
        (-209286662, 4410480, 16003238, 13171824),
        ("khs_distortion_vst2", "khs_distortion_vst3"),
    ),
    ("Dual Delay", 1802724452, (15545650, 11093335, 2622330, 1530382), ("khs_dual_delay_vst2", "khs_dual_delay_vst3")),
    ("Dynamics", 1802727284, (-797046631, 2179884, 8516472, 12254846), ("khs_dynamics_vst2", "khs_dynamics_vst3")),
    ("Ensemble", 1802728814, (-281941871, 16730712, 2351001, 4140397), ("khs_ensemble_vst2", "khs_ensemble_vst3")),
    ("Filter", 1802724969, (2034193867, 5261734, 12553643, 11857824), ("khs_filter_vst2", "khs_filter_vst3")),
    ("Flanger", 1802724972, (1800651355, 12863136, 6113730, 6862169), ("khs_flanger_vst2", "khs_flanger_vst3")),
    (
        "Formant Filter",
        1802729062,
        (1168195342, 2837374, 13980174, 12611695),
        ("khs_formant_filter_vst2", "khs_formant_filter_vst3"),
    ),
    (
        "Frequency Shifter",
        1802724979,
        (153373496, 11225137, 15786774, 12701300),
        ("khs_frequency_shifter_vst2", "khs_frequency_shifter_vst3"),
    ),
    ("Gain", 1802725230, (-1481959855, 7488716, 14899853, 7635833), ("khs_gain_vst2", "khs_gain_vst3")),
    ("Gate", 1802725236, (3851411, 3886112, 6022332, 26116), ("khs_gate_vst2", "khs_gate_vst3")),
    ("Haas", 1802725473, (1962102844, 11685198, 683057, 13989269), ("khs_haas_vst2", "khs_haas_vst3")),
    (
        "Ladder Filter",
        1802726497,
        (-1605258123, 1786056, 4719525, 4286487),
        ("khs_ladder_filter_vst2", "khs_ladder_filter_vst3"),
    ),
    ("Limiter", 1802726516, (-310285317, 6373674, 1840336, 15227668), ("khs_limiter_vst2", "khs_limiter_vst3")),
    (
        "Nonlinear Filter",
        1802724454,
        (351369588, 1591489, 3542236, 7953958),
        ("khs_nonlinear_filter_vst2", "khs_nonlinear_filter_vst3"),
    ),
    (
        "Phase Distortion",
        1802727524,
        (1841041440, 10766429, 4083350, 15534865),
        ("khs_phase_distortion_vst2", "khs_phase_distortion_vst3"),
    ),
    ("Phaser", 1802727528, (951207379, 4278610, 11315647, 8701992), ("khs_phaser_vst2", "khs_phaser_vst3")),
    (
        "Pitch Shifter",
        1802727539,
        (71069735, 7292768, 16303554, 3603799),
        ("khs_pitch_shifter_vst2", "khs_pitch_shifter_vst3"),
    ),
    ("Resonator", 1802728037, (-2088718863, 7422503, 11789685, 14115248), ("khs_resonator_vst2", "khs_resonator_vst3")),
    ("Reverb", 1802728054, (1472643199, 6636054, 12535850, 3865210), ("khs_reverb_vst2", "khs_reverb_vst3")),
    ("Reverser", 1802728050, (-340309580, 16401996, 2073598, 16384869), ("khs_reverser_vst2", "khs_reverser_vst3")),
    ("Ring Mod", 1802728045, (-472182897, 4604537, 9784478, 13833432), ("khs_ring_mod_vst2", "khs_ring_mod_vst3")),
    ("Shaper", 1802728306, (-1441646888, 16534903, 10808450, 8941447), ("khs_shaper_vst2", "khs_shaper_vst3")),
    ("Stereo", 1802728308, (715028626, 1853894, 3494560, 15867113), ("khs_stereo_vst2", "khs_stereo_vst3")),
    ("Tape Stop", 1802728563, (908448062, 6441371, 525339, 10573682), ("khs_tape_stop_vst2", "khs_tape_stop_vst3")),
    (
        "Trance Gate",
        1802728551,
        (-183266600, 9520690, 1991633, 9467344),
        ("khs_trance_gate_vst2", "khs_trance_gate_vst3"),
    ),
    (
        "Transient Shaper",
        1802728562,
        (954499243, 12207330, 135165, 15573623),
        ("khs_transient_shaper_vst2", "khs_transient_shaper_vst3"),
    ),
)


def _khs(name: str, vst2_id: int, vst3_id: tuple[int, int, int, int], plan_names: tuple[str, str]) -> PluginMap:
    full = f"kHs {name}"
    measured = _HEARD.get(name, _STRUCTURAL)
    return PluginMap(
        id=f"kilohearts.{name.lower().replace(' ', '-')}",
        name=full,
        version=None,
        scope="VST2 zip and VST3 header",
        vendor="Kilohearts",
        vst2=PluginFormat(Vst2Id(vst2_id), full, VERBATIM, measured, plan_name=plan_names[0]),
        vst3=PluginFormat(
            Vst3Id(vst3_id), full, KiloheartsHeader(), measured, NO_CONTROLLER_STATE, plan_name=plan_names[1]
        ),
    )


for _name, _vst2_id, _vst3_id, _plan_names in _PLUGINS:
    register(functools.partial(_khs, _name, _vst2_id, _vst3_id, _plan_names))
