"""KORG MS-20 V's patch, read and edited by control and cable, against what the hosted plugin printed.

``state_fixtures/korg_ms20/ms20_v_displays.json`` is the installed plugin's own print of
each public mapped control at 13 positions of its own setter, and after restoring a state
this package wrote with every mapped control at 0.391 or 0.713.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from abletoolz.plugins.identity.ids import PluginKind
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.arturia_v import ArturiaValue, Control
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.korg_ms20_v import (
    MS20_V_VST2,
    Ms20VPatch,
    baseline,
    controls,
    destinations,
    sources,
)
from abletoolz.plugins.state.plugin_map import FormatState

DISPLAYS: dict[str, Any] = json.loads(
    (pathlib.Path(__file__).parent / "state_fixtures" / "korg_ms20" / "ms20_v_displays.json").read_text(
        encoding="utf-8"
    )
)["displays"]


def prints(control: Control, value: ArturiaValue, shown: str) -> bool:
    """Whether ``value`` is what the plugin printed, to its digits; a choice prints its first eight characters."""
    if control.choices:
        if control.choices == ("Off", "On"):
            return value == (shown == "On")
        return str(value)[:8] == shown[:8]
    return abs(float(value) - float(shown)) <= 0.006 * abs(float(shown)) + 0.0006


@pytest.mark.parametrize("key", list(DISPLAYS))
def test_every_printed_control_reads_as_the_plugin_printed_it(key: str) -> None:
    control = controls()[key]
    for requested, shown in DISPLAYS[key]["sweep"] + DISPLAYS[key]["restored"]:
        assert prints(control, control.value(requested), shown.strip()), (requested, shown)


@pytest.mark.parametrize("key", list(DISPLAYS))
def test_every_printed_control_writes_back_what_it_reads(key: str) -> None:
    control = controls()[key]
    for requested, _ in DISPLAYS[key]["sweep"]:
        value = control.value(requested)
        assert control.value(control.normalized(value)) == pytest.approx(value, rel=1e-9, abs=1e-12)


def test_only_the_voice_controls_go_unprinted() -> None:
    assert set(controls()) - set(DISPLAYS) == {"Play Mode", "Unison", "Unison Voices", "Note Priority"}


def test_the_baseline_reads_as_the_factory_default() -> None:
    patch = Ms20VPatch.parse(baseline())
    values = patch.values()
    assert values["MS20VCO_WaveformOsc1"] == "Saw" and values["LP Cutoff"] == pytest.approx(20000.0)
    assert values["Play Mode"] == "Mono Legato" and values["VCF_Type"] == "MKII" and patch.cables == ()


def test_an_edit_and_the_patch_panel_land_in_both_archives() -> None:
    patch = Ms20VPatch.parse(baseline()).with_values(
        {"LP Cutoff": 481.0, "Play Mode": "4", "ESP_ESPEnvFollower_ThreshLvl": 5.769},
        [("Mod Gen", "VCO CV"), ("Keyboard CV", "HPF Cutoff")],
    )
    values = patch.values()
    assert values["LP Cutoff"] == pytest.approx(481.0) and values["Play Mode"] == "4"
    assert values["ESP_ESPEnvFollower_ThreshLvl"] == pytest.approx(5.769)
    assert patch.cables == (("Mod Gen", "VCO CV"), ("Keyboard CV", "HPF Cutoff"))
    stored = patch.archives.values
    assert stored["section[0]/Modulations_PatchBayConnectSrc1_Dest4"] == 1.0
    assert stored["section[1]/Modulations_PatchBayConnectSrc27_Dest1"] == 1.0
    assert Ms20VPatch.parse(patch.raw).with_values({}, []).cables == ()


def test_the_patch_panel_is_numbered_in_the_plugin_s_own_order() -> None:
    assert (len(sources()), len(destinations())) == (29, 19)
    assert sources()[:2] == ("Mod Gen", "Mod Gen Square") and destinations()[3] == "VCO CV"


@pytest.mark.parametrize(
    ("change", "cables", "message"),
    [
        ({"Cutoff": 1.0}, None, "no mapped control"),
        ({"LP Cutoff": 30000.0}, None, "outside"),
        ({"Play Mode": "7"}, None, "no setting"),
        ({}, [("Mod Gen", "Nowhere")], "no patch destination"),
        ({}, [("Phones", "VCO CV")], "no patch source"),
    ],
)
def test_an_edit_the_plugin_cannot_hold_is_refused(
    change: dict[str, ArturiaValue], cables: list[tuple[str, str]] | None, message: str
) -> None:
    with pytest.raises(StateTransformError, match=message):
        Ms20VPatch.parse(baseline()).with_values(change, cables)


def test_an_ms20_v_device_finds_its_map_and_writes_both_vst3_halves() -> None:
    found = CATALOG.find(MS20_V_VST2)
    assert found is not None and found.id == "arturia.korg-ms-20-v"
    first, second = Ms20VPatch.parse(baseline()).archives.archives
    assert found.lower(baseline(), PluginKind.VST3) == FormatState(first.data, second.data)
