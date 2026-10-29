"""Valhalla DSP's plugins.

Every one is a JUCE build whose VST3 replaces its VST2: the VST2 chunk is the
plugin's own patch, the AU holds the same bytes as ``jucePluginState``, and the
VST3 holds them with JUCE's trailer inside a ``VstW`` bank carrying the VST2
UniqueId and the version code. Measured 2026-10-22 with a headless host per
format: an edited patch read back unchanged in every direction, the ``VstW``
bank rebuilt from those two numbers matched the plugin's own byte for byte, and
the VST3 writes no controller half. All eight Room and Shimmer patches in the
user's sets loaded in the AU and read back as the VST2 reads them. None of the
components declares itself sandbox-safe, so each registers with flags 0.
"""

from __future__ import annotations

import datetime

from abletoolz.plugins.container.controller import NO_CONTROLLER_STATE
from abletoolz.plugins.identity.ids import AuId, AuRegistration, Vst2Id, Vst3Id
from abletoolz.plugins.state.audio_unit import JuceClassInfo
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.framings import VERBATIM, JuceVstw
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import PluginFormat, PluginMap

_MEASURED_ON = datetime.date(2026, 10, 22)
_CHUNK = MeasuredState(StateRung.VERBATIM, (StateEvidence.HOSTED,), _MEASURED_ON)
_REFRAMED = MeasuredState(StateRung.REFRAME, (StateEvidence.HOSTED,), _MEASURED_ON)
_REGISTRATION = AuRegistration("Valhalla DSP, LLC", 0)


def _valhalla(
    name: str,
    au_subtype: str,
    vst2_id: int,
    version: int,
    vst3_id: tuple[int, int, int, int],
    build: str,
    *,
    vst2_plan: str | None = None,
    vst3_plan: str | None = None,
    au_plan: str | None = None,
) -> PluginMap:
    au = AuId.of("aufx", au_subtype, "oDin")
    return PluginMap(
        id=f"valhalla.{name.removeprefix('Valhalla').lower()}",
        name=name,
        version=build.split(".")[0],
        scope=f"Build {build} in VST2, VST3 and AU, measured headless with edited patches both ways",
        vendor="Valhalla DSP",
        vst2=None if vst2_plan is None else PluginFormat(Vst2Id(vst2_id), name, VERBATIM, _CHUNK, plan_name=vst2_plan),
        vst3=None
        if vst3_plan is None
        else PluginFormat(
            Vst3Id(vst3_id), name, JuceVstw(vst2_id, version), _REFRAMED, NO_CONTROLLER_STATE, plan_name=vst3_plan
        ),
        au=None
        if au_plan is None
        else PluginFormat(
            au, name, JuceClassInfo(au.identifier), _REFRAMED, registration=_REGISTRATION, plan_name=au_plan
        ),
    )


@register
def supermassive() -> PluginMap:
    return _valhalla(
        "ValhallaSupermassive",
        "sMas",
        1934451059,
        327680,
        (1448301683, 1298232182, 1634494561, 1819042163),
        "5.0.0",
        vst2_plan="valhalla_supermassive_vst2",
        vst3_plan="valhalla_supermassive_vst3",
        au_plan="valhalla_supermassive_au",
    )


@register
def freq_echo() -> PluginMap:
    return _valhalla(
        "ValhallaFreqEcho",
        "FqEh",
        1181828456,
        66056,
        (1448301638, 1900374134, 1634494561, 1819042150),
        "1.2.8",
        vst2_plan="valhalla_freq_echo_vst2",
        vst3_plan="valhalla_freq_echo_vst3",
        au_plan="valhalla_freq_echo_au",
    )


@register
def room() -> PluginMap:
    return _valhalla(
        "ValhallaRoom",
        "Ruum",
        1383429485,
        67080,
        (1448301650, 1970630006, 1634494561, 1819042162),
        "1.6.8",
        vst2_plan="valhalla_room_vst2",
        vst3_plan="valhalla_room_vst3",
        au_plan="valhalla_room_au",
    )


@register
def shimmer() -> PluginMap:
    return _valhalla(
        "ValhallaShimmer",
        "shmr",
        1936223602,
        66304,
        (1448301683, 1752003190, 1634494561, 1819042163),
        "1.3.0",
        vst2_plan="valhalla_shimmer_vst2",
        vst3_plan="valhalla_shimmer_vst3",
        au_plan="valhalla_shimmer_au",
    )


@register
def delay() -> PluginMap:
    return _valhalla(
        "ValhallaDelay",
        "dLay",
        1682727289,
        132353,
        (1448301668, 1281456502, 1634494561, 1819042148),
        "2.5.1",
        vst2_plan="valhalla_delay_vst2",
        vst3_plan="valhalla_delay_vst3",
        au_plan="valhalla_delay_au",
    )


@register
def plate() -> PluginMap:
    return _valhalla(
        "ValhallaPlate",
        "plat",
        1886151028,
        67080,
        (1448301680, 1818326134, 1634494561, 1819042160),
        "1.6.8",
        vst2_plan="valhalla_plate_vst2",
        vst3_plan="valhalla_plate_vst3",
        au_plan="valhalla_plate_au",
    )
