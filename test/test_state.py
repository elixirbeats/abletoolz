"""The state axis: what is known about a patch crossing, and the controller state beside a VST3's patch.

Two halves. The first is the evidence:
:data:`~abletoolz.plugins.state.MEASURED_STATE` and what each row's
evidence is worth, since a rung the bytes merely imply must never print as a
measurement. The second is the ControllerState Live writes, read off sets Live
wrote; what a plugin map writes there is in ``test_plugin_maps``.
"""

from __future__ import annotations

import pytest
from conftest import SKELETONS, parse_editor_state, skeleton_set

from abletoolz.elements import get_element
from abletoolz.formats import hex_text
from abletoolz.plugins.state.fabfilter import FABFILTER_CONSTANT_CONTROLLER, FABFILTER_CONTROLLER_TRAILER
from abletoolz.plugins.state.measured import (
    MEASURED_STATE,
    UNMEASURED,
    MeasuredState,
    StateEvidence,
    StateRung,
    measured_state,
)

# -- what a measurement is worth --------------------------------------------


@pytest.mark.parametrize(
    ("evidence", "predictable"),
    [
        (StateEvidence.EAR, True),
        (StateEvidence.DECLARED, True),
        # The rig watches readback, parameters and two renders. None of them hears
        # the patch, so a plugin that takes a patch and sounds wrong passes every one.
        (StateEvidence.HOSTED, False),
        (StateEvidence.STRUCTURAL, False),
    ],
)
def test_only_a_listen_or_the_vendor_s_word_makes_a_conversion_predictable(
    evidence: StateEvidence, predictable: bool
) -> None:
    assert MeasuredState(StateRung.VERBATIM, (evidence,)).predictable is predictable


def test_an_unknown_name_is_an_experiment_rather_than_an_error_and_says_so_in_one_line() -> None:
    assert measured_state("Nothing At All", None) is UNMEASURED
    assert not UNMEASURED.predictable
    assert UNMEASURED.annotation == "state: unknown -- experiment, audition before trusting"


@pytest.mark.parametrize(
    ("plugin", "annotation"),
    [
        ("FabFilter Volcano 2", "state: re-encode (hosted 2026-08-13)"),
        ("FabFilter Pro-C", "state: re-encode (hosted 2026-08-13)"),
    ],
)
def test_the_annotation_names_the_rung_and_the_evidence(plugin: str, annotation: str) -> None:
    assert MEASURED_STATE[plugin].annotation == annotation


def test_a_measurement_is_found_under_either_name_it_is_asked_about() -> None:
    """A migration is a fact about the source plugin, so both ends of a mapping are asked."""
    assert measured_state("Volcano 3", "FabFilter Volcano 2") is MEASURED_STATE["FabFilter Volcano 2"]


# -- the ControllerState beside it ------------------------------------------


def controller_states(key: str) -> dict[str, bytes]:
    """Every VST3 device's ControllerState in a skeleton, by the name Live shows."""
    live_set = skeleton_set(key)
    found: dict[str, bytes] = {}
    for info in live_set.root.iter("Vst3PluginInfo"):
        text = get_element(info, "Preset.Vst3Preset.ControllerState").text
        payload = hex_text.decode(text)
        found[get_element(info, "Name", attribute="Value")] = payload
    return found


@pytest.mark.parametrize(("key", "plugin"), [("10.1.18", "Pro-R"), ("10.1.30", "Pro-L 2")])
def test_the_fabfilter_constant_is_what_a_set_holds_for_those_products(key: str, plugin: str) -> None:
    """The oracle is a set Live wrote: twelve bytes, the same twelve for each of them."""
    assert controller_states(key)[plugin] == FABFILTER_CONTROLLER_TRAILER
    assert FABFILTER_CONSTANT_CONTROLLER.build(b"any patch") == FABFILTER_CONTROLLER_TRAILER


def test_a_populated_controller_state_in_a_set_carries_the_preset_name() -> None:
    """Which is the statement this replaces: the editor state is in the file after all."""
    payload = controller_states("10.1.30")["Pro-Q 3"]
    assert payload.endswith(FABFILTER_CONTROLLER_TRAILER)
    assert parse_editor_state(payload).preset_name == "Default Setting"


@pytest.mark.parametrize("skeleton", sorted(path.stem for path in SKELETONS.glob("*.als")))
def test_every_vst3_preset_pairs_one_processor_state_with_one_controller_state(skeleton: str) -> None:
    """Measured over 150 sets and true of every fixture: the second element is never absent."""
    live_set = skeleton_set(skeleton)
    for preset in live_set.root.iter("Vst3Preset"):
        assert len(preset.findall("ProcessorState")) == 1
        assert len(preset.findall("ControllerState")) == 1
