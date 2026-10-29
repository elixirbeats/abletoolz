"""Surge Synth Team's plugins: Surge XT and Surge XT Effects.

JUCE builds with no VST2: the AU holds the plugin's own patch as
``jucePluginState``, and the VST3 holds it with JUCE's trailer. Surge XT has a
bypass parameter of its own, so its trailer is the short form. Measured
2026-10-22: an edited patch crossed AU to VST3 and back read back as each
format reads its own, and neither VST3 writes a controller half. Both AUs
declare themselves sandbox-safe, so each registers with flags 2.

Surge 1.x is a different plugin (VST2 UniqueId ``cjs3``), so turning one of its
patches into Surge XT's is a conversion between the two plugins, not a crossing
inside one: :mod:`abletoolz.plugins.converters.surge` is that step, and Surge
XT's map writes the result in whichever format the device becomes.
"""

from __future__ import annotations

import datetime

from abletoolz.plugins.container.controller import NO_CONTROLLER_STATE
from abletoolz.plugins.identity.ids import AuId, AuRegistration, Vst2Id, Vst3Id
from abletoolz.plugins.state.audio_unit import JuceClassInfo
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.framings import VERBATIM, JuceTrailer
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import Layout, PluginFormat, PluginMap

_REFRAMED = MeasuredState(StateRung.REFRAME, (StateEvidence.HOSTED,), datetime.date(2026, 10, 22))


def _surge(
    key: str, name: str, au: AuId, vst3_id: tuple[int, int, int, int], own_bypass: bool, plan_names: tuple[str, str]
) -> PluginMap:
    return PluginMap(
        id=f"surge-synth-team.{key}",
        name=name,
        version="1",
        scope="Build 1.3.4 in VST3 and AU, measured headless with edited patches both ways",
        vendor="Surge Synth Team",
        vst3=PluginFormat(
            Vst3Id(vst3_id), name, JuceTrailer(own_bypass), _REFRAMED, NO_CONTROLLER_STATE, plan_name=plan_names[0]
        ),
        au=PluginFormat(
            au,
            name,
            JuceClassInfo(au.identifier),
            _REFRAMED,
            registration=AuRegistration("Surge Synth Team", 2),
            plan_name=plan_names[1],
        ),
    )


@register
def surge_xt() -> PluginMap:
    return _surge(
        "surge-xt",
        "Surge XT",
        AuId.of("aumu", "SgXT", "VmbA"),
        (-1412567295, -1853687061, 1450009153, 1399281748),
        own_bypass=True,
        plan_names=("surge_xt_vst3", "surge_xt_au"),
    )


@register
def surge_xt_effects() -> PluginMap:
    return _surge(
        "surge-xt-effects",
        "Surge XT Effects",
        AuId.of("aufx", "SFXT", "VmbA"),
        (-1412567295, -1853687061, 1450009153, 1397119060),
        own_bypass=False,
        plan_names=("surge_xt_effects_vst3", "surge_xt_effects_au"),
    )


# -- Surge 1.x into Surge XT ---------------------------------------------------

SURGE_1_UNIQUE_ID = 1667920691
SURGE_1_MAGIC = b"sub3"


@register
def surge_1() -> PluginMap:
    return PluginMap(
        id="surge-synth-team.surge-1",
        name="Surge",
        version="1",
        scope="VST2 chunks of patch revision 10, as the user's sets hold them",
        vendor="Surge Synth Team",
        vst2=PluginFormat(
            Vst2Id(SURGE_1_UNIQUE_ID),
            # What its devices store, lower case; "surge.64" is a jBridged build of it.
            "surge",
            VERBATIM,
            MeasuredState(StateRung.VERBATIM, (StateEvidence.STRUCTURAL,), datetime.date(2026, 10, 22)),
            plan_name="surge_vst2",
        ),
        layouts=(Layout("sub3 patch", lambda patch: patch.startswith(SURGE_1_MAGIC)),),
    )
