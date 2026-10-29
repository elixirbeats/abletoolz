"""Prophet-5 V's patch, read and edited by control, against what the hosted plugin printed.

``state_fixtures/prophet_5_v_displays.json`` is the installed plugin's own print of
each of its 96 core controls and the public ones beyond them at seven positions, taken
on the macro-free baseline the package builds patches from. The FX switch and
slot type are not public, so the plugin prints neither.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from abletoolz.plugins.identity.ids import PluginKind
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.plugin_map import FormatState
from abletoolz.plugins.state.prophet_5_v import (
    PROPHET_5_V_VST2,
    Control,
    Prophet5Patch,
    Prophet5Value,
    baseline,
    controls,
)

DISPLAYS = json.loads((pathlib.Path(__file__).parent / "state_fixtures" / "prophet_5_v_displays.json").read_text())


def prints(control: Control, value: Prophet5Value, shown: str | float) -> bool:
    """Whether ``value`` is what the plugin printed, to the digits it prints; a selector prints eight characters."""
    if isinstance(shown, str):
        if control.choices == ("Off", "On"):
            return value == (shown == "On")
        return value == int(shown) if control.unit == "st" else str(value)[:8] == shown
    assert isinstance(value, float)
    return abs(value - shown) <= 0.006 * abs(shown) + 0.0006


@pytest.mark.parametrize("key", list(DISPLAYS))
def test_every_printed_control_reads_as_the_plugin_printed_it(key: str) -> None:
    control = controls()[key]
    for requested, shown in DISPLAYS[key]:
        assert prints(control, control.value(requested), shown), (requested, shown)


@pytest.mark.parametrize("key", list(DISPLAYS))
def test_every_printed_control_writes_back_what_it_reads(key: str) -> None:
    control = controls()[key]
    for requested, _ in DISPLAYS[key]:
        value = control.value(requested)
        assert control.value(control.normalized(value)) == pytest.approx(value, rel=1e-9, abs=1e-12)


def test_only_the_fx_switch_and_slot_type_go_unprinted() -> None:
    assert set(controls()) - set(DISPLAYS) == {"FX_Enable", "FX1_ModuleType"}


def test_the_baseline_reads_as_the_init_patch() -> None:
    values = Prophet5Patch.parse(baseline()).values()
    assert len(values) == 106
    assert values["FX_Enable"] is False and values["FX1_ModuleType"] == "Chorus" and values["VST3_CtrlModWheel"] == 0
    assert values["Prophet5VCO_Freq1"] == 0 and values["Prophet5VCO_SawOn1"] is True
    assert values["Prophet5VCF_Freq"] == pytest.approx(6214.0)


def test_a_slot_type_is_its_item_over_the_top_index() -> None:
    patch = Prophet5Patch.parse(baseline()).with_values({"FX_Enable": True, "FX1_ModuleType": "Delay"})
    assert patch.archives.values["section[1]/FX1_ModuleType"] == pytest.approx(2 / 16)
    assert patch.values()["FX1_ModuleType"] == "Delay" and patch.values()["FX_Enable"] is True


def test_an_edit_lands_in_both_archives() -> None:
    patch = Prophet5Patch.parse(baseline()).with_values(
        {"Prophet5VCF_Freq": 1000.0, "VCA_Prophet5ADSR_Attack": 0.5, "Prophet5VCO_Freq1": -12, "UnisonSwitch": True}
    )
    values = patch.values()
    assert values["Prophet5VCF_Freq"] == pytest.approx(1000.0) and values["VCA_Prophet5ADSR_Attack"] == pytest.approx(
        0.5
    )
    assert values["Prophet5VCO_Freq1"] == -12 and values["UnisonSwitch"] is True
    stored = patch.archives.values
    assert stored["section[0]/Prophet5VCF_Freq"] == stored["section[1]/Prophet5VCF_Freq"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"Cutoff": 1.0}, "no core control"),
        ({"Prophet5VCF_Freq": 7000.0}, "outside"),
        ({"Prophet5VCO_Freq1": 37}, "no setting"),
        ({"Prophet5VCO_Freq1": 1.0}, "no setting"),
        ({"VCA_Prophet5ADSR_Attack": 100.0}, "outside"),
        ({"Volume": float("inf")}, "finite"),
    ],
)
def test_an_edit_the_plugin_cannot_hold_is_refused(change: dict[str, Prophet5Value], message: str) -> None:
    with pytest.raises(StateTransformError, match=message):
        Prophet5Patch.parse(baseline()).with_values(change)


def test_a_prophet_5_v_device_finds_its_map_and_writes_both_vst3_halves() -> None:
    prophet = CATALOG.find(PROPHET_5_V_VST2)
    assert prophet is not None and prophet.id == "arturia.prophet-5-v"
    written = prophet.lower(baseline(), PluginKind.VST3)
    first, second = Prophet5Patch.parse(baseline()).archives.archives
    assert written == FormatState(first.data, second.data)
