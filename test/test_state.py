"""The state axis: what a mapping entry may say, and what is known about it.

Two halves. The first is the seam -- the policies a config entry may name, the
two registries a vendor module fills, and the refusals that keep an unregistered
name from being quietly treated as verbatim. The second is the evidence:
:data:`~abletoolz.plugin_parsers.state.MEASURED_STATE` and what each row's
evidence is worth, since a rung the bytes merely imply must never print as a
measurement.
"""

from __future__ import annotations

import datetime
import pathlib
import struct
from collections.abc import Iterator

import pydantic
import pytest

from abletoolz import decode_encode
from abletoolz.live_set import AbletonSet
from abletoolz.misc import get_element
from abletoolz.plugin_parsers import PluginKind
from abletoolz.plugin_parsers.format_translation import (
    NamedTarget,
    TranslationTarget,
    parse_config_targets,
    translate_device,
)
from abletoolz.plugin_parsers.state import (
    _CUSTOM_TRANSFORMS as REGISTRY,
)
from abletoolz.plugin_parsers.state import (
    CUSTOM_PREFIX,
    MEASURED_STATE,
    NO_CONTROLLER_STATE,
    UNMEASURED,
    ConstantControllerState,
    CustomState,
    MeasuredState,
    NoControllerState,
    StateEvidence,
    StateRung,
    StateTransform,
    StateTransformError,
    custom_state,
    measured_state,
    parse_controller_state,
    parse_state,
    register_custom_state,
    registered_controller_states,
    registered_custom_states,
    state_bytes,
)
from abletoolz.plugin_parsers.state.fabfilter import (
    FABFILTER_CONSTANT_CONTROLLER,
    FABFILTER_CONTROLLER_TRAILER,
    FABFILTER_FABF_CONTROLLER,
    EditorState,
)

SKELETONS = pathlib.Path(__file__).parent / "version_fixtures" / "skeletons"

# -- what a measurement is worth --------------------------------------------


def test_a_heard_conversion_is_predictable() -> None:
    """The rule: measured by ear or declared by the vendor, or it is a guess."""
    assert MEASURED_STATE["Serum"].predictable
    assert MEASURED_STATE["kHs Distortion"].predictable


def test_the_plugin_accepting_a_patch_is_not_a_listen() -> None:
    """Hosted evidence is strong and still not predictive.

    The rig watches readback, parameters and two renders. None of them hears the
    patch, so a plugin that takes a patch and sounds wrong passes every one.
    """
    hosted = MEASURED_STATE["FabFilter Pro-Q 3"]
    assert StateEvidence.HOSTED in hosted.evidence
    assert not hosted.predictable
    assert MEASURED_STATE["FabFilter Timeless 3"].predictable  # a human heard this one too


def test_an_unmeasured_plugin_says_so_in_one_line() -> None:
    assert not UNMEASURED.predictable
    assert UNMEASURED.annotation == "state: unknown -- experiment, audition before trusting"


@pytest.mark.parametrize(
    ("plugin", "annotation"),
    [
        ("Serum", "state: verbatim (ear+declared 2026-08-10)"),
        ("Ghz Tupe 3", "state: verbatim (ear 2026-08-10)"),
        ("FabFilter Pro-Q 3", "state: reframe (hosted 2026-08-13)"),
        ("FabFilter Timeless 3", "state: reframe (ear+hosted 2026-08-13)"),
        ("FabFilter Pro-C", "state: re-encode (hosted 2026-08-15)"),
        ("kHs Stereo", "state: reframe (ear 2026-08-15)"),
        ("kHs Filter", "state: reframe (ear 2026-08-10)"),
    ],
)
def test_the_annotation_names_the_rung_and_the_evidence(plugin: str, annotation: str) -> None:
    assert MEASURED_STATE[plugin].annotation == annotation


def test_a_measurement_is_found_under_either_format_s_name() -> None:
    """A rung is a fact about a plugin, not about a spelling.

    "Serum_x64" is what a set stores and "Serum" is what the table lists, so the
    lookup is asked about both ends of the mapping and takes the first hit.
    """
    assert measured_state("Serum_x64", "Serum") is MEASURED_STATE["Serum"]
    assert measured_state("FabFilter Timeless 3", "Timeless 3") is MEASURED_STATE["FabFilter Timeless 3"]


def test_an_unknown_name_is_an_experiment_rather_than_an_error() -> None:
    assert measured_state("Nothing At All", None) is UNMEASURED


# -- the transform a mapping entry asks for ---------------------------------


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A registry that starts as it really is and is put back afterwards."""
    monkeypatch.setattr("abletoolz.plugin_parsers.state._CUSTOM_TRANSFORMS", dict(REGISTRY))
    yield


def _shout(payload: bytes) -> bytes:
    return b"LOUD" + payload


def test_verbatim_rewrites_nothing() -> None:
    assert state_bytes(StateTransform.VERBATIM) is None


def test_the_kilohearts_reframe_wraps_the_payload_in_its_header() -> None:
    rewrite = state_bytes(StateTransform.KILOHEARTS)
    assert rewrite is not None
    assert rewrite(b"payload") == b"\x01\x00\x00\x00\x07\x00\x00\x00payload"


def test_the_fabfilter_reframe_cuts_the_editor_half_off() -> None:
    """A VST2 chunk is the processor's state then the editor's; VST3 wants the first plus a trailer."""
    rewrite = state_bytes(StateTransform.FABFILTER)
    assert rewrite is not None
    assert rewrite(b"patch" + b"FV3l" + b"editor") == b"patch" + b"FFpr\x01\x00\x00\x00\x00\x00\x00\x00"


def test_the_fabfilter_reframe_refuses_a_chunk_it_cannot_find_the_seam_in() -> None:
    """The FabF-generation products write something else, and guessing at it would ship a wrong patch."""
    with pytest.raises(StateTransformError, match="FabF-generation"):
        state_bytes(StateTransform.FABFILTER)(b"no editor section here")  # type: ignore[misc]


def test_the_izotope_reframe_unwraps_the_length_and_the_preset_name() -> None:
    rewrite = state_bytes(StateTransform.IZOTOPE)
    assert rewrite is not None
    chunk = struct.pack("<I", 5) + b"patch" + struct.pack("<I", 7) + b"Default"
    assert rewrite(chunk) == b"patch"


def test_the_izotope_reframe_refuses_a_chunk_whose_lengths_disagree() -> None:
    """A blob that merely opens with a plausible number is not this shape."""
    with pytest.raises(StateTransformError, match="not an iZotope"):
        state_bytes(StateTransform.IZOTOPE)(struct.pack("<I", 5) + b"patch" + struct.pack("<I", 99) + b"Default")  # type: ignore[misc]


def test_a_registered_transform_is_reached_by_name(registry: None) -> None:
    register_custom_state("shout", _shout)
    assert "shout" in registered_custom_states()
    assert parse_state("custom:shout") == CustomState("shout")
    rewrite = state_bytes(CustomState("shout"))
    assert rewrite is not None
    assert rewrite(b"x") == b"LOUDx"


def test_a_custom_state_prints_the_way_it_is_written(registry: None) -> None:
    register_custom_state("shout", _shout)
    assert str(CustomState("shout")) == "custom:shout"
    assert CUSTOM_PREFIX == "custom:"


def test_an_unregistered_custom_transform_is_refused_loudly(registry: None) -> None:
    """The dangerous silence: falling back to verbatim would pass the old bytes."""
    with pytest.raises(StateTransformError, match="analog_lab"):
        parse_state("custom:analog_lab")
    with pytest.raises(StateTransformError):
        custom_state("analog_lab")


def test_an_unknown_built_in_state_is_refused_too() -> None:
    with pytest.raises(ValueError, match="verbatimm"):
        parse_state("verbatimm")


def test_registering_a_name_twice_with_two_transforms_is_refused(registry: None) -> None:
    register_custom_state("shout", _shout)
    register_custom_state("shout", _shout)
    with pytest.raises(StateTransformError, match="already registered"):
        register_custom_state("shout", lambda payload: payload)


# -- the ControllerState beside it ------------------------------------------


def controller_states(key: str) -> dict[str, bytes]:
    """Every VST3 device's ControllerState in a skeleton, by the name Live shows."""
    live_set = AbletonSet(SKELETONS / f"{key}.als")
    assert live_set.parse()
    found: dict[str, bytes] = {}
    for info in live_set.root.iter("Vst3PluginInfo"):
        text = get_element(info, "Preset.Vst3Preset.ControllerState").text
        payload = b"" if text is None or not text.strip() else bytes.fromhex(decode_encode.xml_to_string(text)[0])
        found[get_element(info, "Name", attribute="Value")] = payload
    return found


def test_a_target_that_declares_none_writes_nothing() -> None:
    """The default, and the whole truth for the plugins measured to keep no controller state."""
    assert NO_CONTROLLER_STATE.build(b"anything") is None
    assert NoControllerState() == NO_CONTROLLER_STATE


def test_a_constant_controller_state_ignores_the_patch() -> None:
    constant = ConstantControllerState(b"fixed")
    assert constant.build(b"one patch") == b"fixed"
    assert constant.build(b"another") == b"fixed"


@pytest.mark.parametrize("plugin", ["Pro-R", "Pro-L 2"])
def test_the_fabfilter_constant_is_what_a_set_holds_for_those_products(plugin: str) -> None:
    """The oracle is a set Live wrote: twelve bytes, the same twelve for each of them."""
    assert controller_states("10.1.3")[plugin] == FABFILTER_CONTROLLER_TRAILER
    assert FABFILTER_CONSTANT_CONTROLLER.build(b"any patch") == FABFILTER_CONTROLLER_TRAILER


def test_a_populated_controller_state_in_a_set_carries_the_preset_name() -> None:
    """Which is the statement this replaces: the editor state is in the file after all."""
    payload = controller_states("10.1.3")["Pro-Q 3"]
    assert payload.endswith(FABFILTER_CONTROLLER_TRAILER)
    assert EditorState.parse(payload).preset_name == "Default Setting"


@pytest.mark.parametrize("skeleton", sorted(path.stem for path in SKELETONS.glob("*.als")))
def test_every_vst3_preset_pairs_one_processor_state_with_one_controller_state(skeleton: str) -> None:
    """Measured over 150 sets and true of every fixture: the second element is never absent."""
    live_set = AbletonSet(SKELETONS / f"{skeleton}.als")
    assert live_set.parse()
    for preset in live_set.root.iter("Vst3Preset"):
        assert len(preset.findall("ProcessorState")) == 1
        assert len(preset.findall("ControllerState")) == 1


# -- and what a config entry may write --------------------------------------


def test_a_config_entry_may_name_a_registered_transform(registry: None) -> None:
    register_custom_state("shout", _shout)
    parsed = parse_config_targets({"Old Thing": {"name": "New Thing", "state": "custom:shout"}})
    assert parsed == {"Old Thing": NamedTarget(PluginKind.VST3, "New Thing", CustomState("shout"))}


def test_a_config_entry_naming_an_unregistered_transform_fails_to_load(registry: None) -> None:
    with pytest.raises(pydantic.ValidationError, match="analog_lab"):
        parse_config_targets({"Analog Lab 1": {"name": "Analog Lab 4", "state": "custom:analog_lab"}})


def test_a_custom_transform_reaches_a_translated_device(registry: None) -> None:
    """End to end: the entry names it, and the device's patch comes out rewritten."""
    register_custom_state("shout", _shout)
    live_set = AbletonSet(SKELETONS / "11.3.42.als")
    assert live_set.parse()
    (info,) = [
        element
        for element in live_set.root.iter("VstPluginInfo")
        if get_element(element, "PlugName", attribute="Value") == "Serum_x64"
    ]
    preset = get_element(info, "Preset.VstPreset")
    buffer = get_element(preset, "Buffer")
    buffer.text = decode_encode.string_to_xml(b"patch".hex().upper(), levels=(preset.text or "").count("\t") + 1)

    target = TranslationTarget(PluginKind.VST3, "New Thing", (1, 2, 3, 4), CustomState("shout"))
    translate_device(info, target)

    state = get_element(info, "Preset.Vst3Preset.ProcessorState")
    assert state.text is not None
    assert bytes.fromhex(decode_encode.xml_to_string(state.text)[0]) == b"LOUDpatch"


def test_a_record_is_frozen() -> None:
    """The table is knowledge, not scratch space."""
    record = MeasuredState(StateRung.VERBATIM, (StateEvidence.EAR,), datetime.date(2026, 8, 10))
    with pytest.raises(AttributeError):
        record.rung = StateRung.REFRAME  # type: ignore[misc]


# -- naming a ControllerState from a config entry ---------------------------
# The shapes existed before anything could ask for one. A config entry names one
# now, and the failure that matters is the quiet one: a plugin that keeps a
# controller state getting the empty element would load at its defaults and
# sound right doing it.


def test_the_measured_fabfilter_shapes_are_reachable_by_name() -> None:
    assert FABFILTER_FABF_CONTROLLER in registered_controller_states()
    assert parse_controller_state(FABFILTER_FABF_CONTROLLER) is FABFILTER_CONSTANT_CONTROLLER
    assert parse_controller_state(FABFILTER_FABF_CONTROLLER).build(b"anything") == FABFILTER_CONTROLLER_TRAILER


def test_a_controller_state_nothing_registered_is_refused_loudly() -> None:
    with pytest.raises(StateTransformError, match="serum-editor"):
        parse_controller_state("serum-editor")


def test_a_config_entry_may_name_one() -> None:
    parsed = parse_config_targets({"FabFilter Pro-C.64": {"name": "Pro-C 2", "controller": FABFILTER_FABF_CONTROLLER}})
    (target,) = parsed.values()
    assert target.controller_state is FABFILTER_CONSTANT_CONTROLLER


def test_a_config_entry_naming_an_unregistered_one_fails_to_load() -> None:
    with pytest.raises(pydantic.ValidationError, match="serum-editor"):
        parse_config_targets({"Serum_x64": {"name": "Serum", "controller": "serum-editor"}})


def test_an_entry_that_says_nothing_still_means_the_empty_element() -> None:
    """The behaviour every target had before a name could be written."""
    parsed = parse_config_targets({"Serum_x64": {"name": "Serum"}})
    (target,) = parsed.values()
    assert target.controller_state == NO_CONTROLLER_STATE


def test_a_named_controller_state_reaches_a_translated_device() -> None:
    """End to end: the entry names it and the device comes out carrying it."""
    live_set = AbletonSet(SKELETONS / "11.3.42.als")
    assert live_set.parse()
    (info,) = [
        element
        for element in live_set.root.iter("VstPluginInfo")
        if get_element(element, "PlugName", attribute="Value") == "Serum_x64"
    ]
    preset = get_element(info, "Preset.VstPreset")
    buffer = get_element(preset, "Buffer")
    buffer.text = decode_encode.string_to_xml(b"patch".hex().upper(), levels=(preset.text or "").count("\t") + 1)

    target = TranslationTarget(
        PluginKind.VST3, "Pro-C 2", (1, 2, 3, 4), StateTransform.VERBATIM, FABFILTER_CONSTANT_CONTROLLER
    )
    translate_device(info, target)

    controller = get_element(info, "Preset.Vst3Preset.ControllerState")
    assert controller.text is not None
    assert bytes.fromhex(decode_encode.xml_to_string(controller.text)[0]) == FABFILTER_CONTROLLER_TRAILER
