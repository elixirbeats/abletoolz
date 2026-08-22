"""The first re-encode: a Pro-Q 1 bank becoming a Pro-Q 3 processor state.

Nothing here reads a set. Every bank is synthesized from the layout the module
documents, and the arithmetic is checked against values that can be reasoned
about -- 1 kHz, 0 dB, a named shape -- rather than against a blob harvested from
somebody's project. The one number that does come from outside is Pro-Q 3's own
default state, read off the installed plugin, and the test that matters most
here is the one that catches it drifting: Pro-Q 1's default band and Pro-Q 3's
default band are the same 1 kHz bell, reached through two unrelated formulas.
"""

from __future__ import annotations

import math
import pathlib
import struct

import pytest

from abletoolz import decode_encode
from abletoolz.live_set import AbletonSet
from abletoolz.misc import get_element
from abletoolz.plugin_parsers import PluginKind
from abletoolz.plugin_parsers.format_translation import TranslationTarget, translate_device
from abletoolz.plugin_parsers.state import (
    _FABFILTER_EDITOR_MAGIC,
    FABFILTER_CONTROLLER_TRAILER,
    FABFILTER_PROCESSOR_TRAILER,
    CustomState,
    StateEvidence,
    StateRung,
    StateTransformError,
    custom_state,
    measured_state,
    parse_state,
    registered_custom_states,
)
from abletoolz.plugin_parsers.state.derived import (
    DERIVED_TABLES,
    FABF_MAGIC,
    DerivedTable,
    FabfState,
    LinearTransfer,
    StepTransfer,
    TableTransfer,
    _read_transfer,
    read_derived_table,
)
from abletoolz.plugin_parsers.state.fabfilter import (
    _Q1_TO_Q3_SHAPE,
    _Q1_TO_Q3_SLOPE,
    EDITOR_STATE_VERSION,
    FFBS_MAGIC,
    NO_INSTANCE,
    PRO_C_1_DROPPED,
    PRO_C_1_ENUM_WIDENING,
    PRO_C_1_FILTER_SWITCHES,
    PRO_C_1_PARAMETERS,
    PRO_C_1_TO_PRO_C_2,
    PRO_C_1_TO_PRO_C_2_STATE,
    PRO_C_2,
    PRO_C_2_TABLE,
    PRO_Q1_BAND_SLOTS,
    PRO_Q1_BANDS,
    PRO_Q1_PARAMETERS,
    PRO_Q1_TO_PRO_Q3,
    PRO_Q3_BAND_SLOTS,
    PRO_Q3_BANDS,
    PRO_Q3_DEFAULT_BAND,
    PRO_Q3_DEFAULT_PARAMETERS,
    PRO_Q3_EDITOR_MAGIC,
    PRO_Q3_MAX_FREQUENCY_LOG2,
    PRO_Q3_PARAMETERS,
    PRO_Q3_STATE_VERSION,
    SATURN_2_EDITOR_MAGIC,
    TIMELESS_3_EDITOR_MAGIC,
    EditorState,
    FfbsControllerState,
    FfbsState,
    Q1BandField,
    Q3BandField,
    pro_q1_to_pro_q3,
    pro_q1_to_pro_q3_parameters,
)
from abletoolz.plugin_parsers.state.fxbk import LEGACY_NAME_BYTES, LegacyBank

SKELETONS = pathlib.Path(__file__).parent / "version_fixtures" / "skeletons"

# Pro-Q 1's own default band, as its bank stores it: 1 kHz, 0 dB, a bell at
# 24 dB/oct, stereo, on. Every synthesized bank starts from this.
Q1_DEFAULT_BAND = (0.57519, 0.5, 0.5, 0.0, 2 / 3, 1.0, 1.0)


def q1_bank(*bands: tuple[float, ...]) -> tuple[float, ...]:
    """A Pro-Q 1 parameter bank using ``bands`` and defaulting the rest."""
    used = (len(bands) / PRO_Q1_BANDS,)
    rest = (Q1_DEFAULT_BAND,) * (PRO_Q1_BANDS - len(bands))
    globals_ = (0.0,) * (PRO_Q1_PARAMETERS - 1 - PRO_Q1_BANDS * PRO_Q1_BAND_SLOTS)
    return used + tuple(value for band in (*bands, *rest) for value in band) + globals_


def band(parameters: tuple[float, ...], index: int) -> tuple[float, ...]:
    """One Pro-Q 3 band out of a full parameter array."""
    start = index * PRO_Q3_BAND_SLOTS
    return parameters[start : start + PRO_Q3_BAND_SLOTS]


def with_field(source: tuple[float, ...], field: Q1BandField, value: float) -> tuple[float, ...]:
    """A Pro-Q 1 band with one slot changed."""
    changed = list(source)
    changed[field] = value
    return tuple(changed)


def converted(field: Q1BandField, value: float) -> tuple[float, ...]:
    """Band 0 of a one-band bank whose ``field`` says ``value``, as Pro-Q 3 stores it."""
    return band(pro_q1_to_pro_q3_parameters(q1_bank(with_field(Q1_DEFAULT_BAND, field, value))), 0)


# -- the two chunk generations ----------------------------------------------


def test_a_legacy_bank_round_trips() -> None:
    bank = LegacyBank(preset_name="Bright", parameters=(0.0, 0.25, 1.0))
    assert LegacyBank.parse(bank.encode()) == bank
    assert len(bank.encode()) == LEGACY_NAME_BYTES + 3 * 4


def test_a_legacy_name_is_read_out_of_its_padded_field() -> None:
    """Live pads the name with NULs, and everything after the first one is padding."""
    payload = b"Vocal Bus".ljust(LEGACY_NAME_BYTES, b"\x00") + struct.pack("<f", 0.5)
    assert LegacyBank.parse(payload).preset_name == "Vocal Bus"


@pytest.mark.parametrize("magic", [b"FFBS", b"FabF"])
def test_a_chunk_is_refused_rather_than_read_as_a_bank(magic: bytes) -> None:
    """A newer FabFilter's chunk decodes as plausible floats, which is the danger."""
    with pytest.raises(StateTransformError, match="not a Live stored-parameter bank"):
        LegacyBank.parse(magic + bytes(64))


@pytest.mark.parametrize("size", [3, LEGACY_NAME_BYTES + 3, LEGACY_NAME_BYTES + 6])
def test_a_bank_that_is_not_a_whole_number_of_floats_is_refused(size: int) -> None:
    with pytest.raises(StateTransformError, match="stored-parameter bank"):
        LegacyBank.parse(bytes(size))


def test_an_ffbs_state_round_trips_with_either_tail() -> None:
    """The tail says which side of the format boundary the state came from."""
    state = FfbsState(version=1, parameters=(1.0, 2.0), tail=FABFILTER_PROCESSOR_TRAILER)
    assert FfbsState.parse(state.encode()) == state
    editor = FfbsState(version=1, parameters=(1.0, 2.0), tail=b"FQ3peditor")
    assert FfbsState.parse(editor.encode()) == editor


def test_an_ffbs_state_refuses_what_is_not_one() -> None:
    with pytest.raises(StateTransformError, match="too short"):
        FfbsState.parse(b"FFBS")
    with pytest.raises(StateTransformError, match="is not FFBS"):
        FfbsState.parse(b"FabF" + struct.pack("<II", 2, 0))
    with pytest.raises(StateTransformError, match="overruns"):
        FfbsState.parse(FFBS_MAGIC + struct.pack("<II", 1, 99))


# -- the editor state on the other side of the chunk ------------------------


def editor_state(
    preset_name: str = "Default Setting",
    *,
    magic: bytes = PRO_Q3_EDITOR_MAGIC,
    instance_index: int = NO_INSTANCE,
    label: str = "",
    controllers: tuple[tuple[str, str], ...] = (),
) -> EditorState:
    """An editor state shaped the way the corpus writes one."""
    return EditorState(
        magic=magic,
        version=EDITOR_STATE_VERSION,
        preset_name=preset_name,
        instance_index=instance_index,
        label=label,
        controllers=controllers,
    )


def test_an_editor_state_round_trips() -> None:
    state = editor_state("Smashing Compression", instance_index=2, label="Drums")
    assert EditorState.parse(state.encode()) == state


def test_an_editor_state_round_trips_its_named_controllers() -> None:
    """Timeless 3 keeps its XY pads and envelope followers here, named."""
    state = editor_state(
        "Flutter Machine MdB",
        magic=TIMELESS_3_EDITOR_MAGIC,
        controllers=(("XY1", "Ducking"), ("EF2", ""), ("XLFO1", "Random")),
    )
    parsed = EditorState.parse(state.encode())
    assert parsed == state
    assert parsed.controllers[1] == ("EF2", "")


@pytest.mark.parametrize("magic", [PRO_Q3_EDITOR_MAGIC, SATURN_2_EDITOR_MAGIC, TIMELESS_3_EDITOR_MAGIC])
def test_every_product_s_editor_magic_is_one_state_py_cuts_a_chunk_at(magic: bytes) -> None:
    """The half state.py cuts off a VST2 chunk is the half this module writes back."""
    assert magic in _FABFILTER_EDITOR_MAGIC


def test_an_editor_state_without_the_controller_landmark_is_refused() -> None:
    """It is the one fixed field past the two variable-length strings."""
    payload = bytearray(editor_state().encode())
    payload[-12:-8] = b"junk"
    with pytest.raises(StateTransformError, match="not an editor state"):
        EditorState.parse(bytes(payload))


@pytest.mark.parametrize("size", [0, 8, 20])
def test_a_truncated_editor_state_is_refused(size: int) -> None:
    with pytest.raises(StateTransformError, match="end before"):
        EditorState.parse(editor_state("A Long Preset Name").encode()[:size])


def test_a_controller_state_carries_a_chunk_s_editor_half_and_the_trailer() -> None:
    """Same product both sides: the VST2 chunk already holds this state, after the processor half."""
    editor = editor_state("Smear bM", label="Bass")
    chunk = FfbsState(version=1, parameters=(1.0, 2.0), tail=editor.encode()).encode()
    built = FfbsControllerState(PRO_Q3_EDITOR_MAGIC).build(chunk)
    assert built == editor.encode() + FABFILTER_CONTROLLER_TRAILER
    assert EditorState.parse(built).preset_name == "Smear bM"


def test_a_controller_state_built_from_a_bank_takes_the_bank_s_preset_name() -> None:
    """The older products expose no chunk, so the name is all there is to carry."""
    bank = LegacyBank(preset_name="Smashing Compression", parameters=(0.5, 0.5))
    built = FfbsControllerState(PRO_Q3_EDITOR_MAGIC).build(bank.encode())
    assert built == editor_state("Smashing Compression").encode() + FABFILTER_CONTROLLER_TRAILER
    assert EditorState.parse(built) == editor_state("Smashing Compression")


def test_a_controller_state_built_from_a_bank_says_it_belongs_to_no_instance() -> None:
    """What the int32 after the name indexes is unknown, so nothing here claims one."""
    built = FfbsControllerState(PRO_Q3_EDITOR_MAGIC).build(LegacyBank("Default Setting", (0.5,)).encode())
    parsed = EditorState.parse(built)
    assert parsed.instance_index == NO_INSTANCE
    assert parsed.label == ""
    assert parsed.controllers == ()


def test_a_controller_state_refuses_a_source_that_is_neither() -> None:
    """A blob that is not this product's chunk and not a bank has no name to carry."""
    with pytest.raises(StateTransformError, match="stored-parameter bank"):
        FfbsControllerState(PRO_Q3_EDITOR_MAGIC).build(b"FFBS" + bytes(9))


# -- the defaults the remap builds on ---------------------------------------


def test_pro_q3_s_defaults_are_the_shape_its_state_declares() -> None:
    assert len(PRO_Q3_DEFAULT_PARAMETERS) == PRO_Q3_PARAMETERS == 358
    assert PRO_Q3_DEFAULT_PARAMETERS[:PRO_Q3_BAND_SLOTS] == PRO_Q3_DEFAULT_BAND


def test_both_versions_default_the_same_band_to_the_same_kilohertz() -> None:
    """Two unrelated formulas, one answer, which is what makes either believable.

    Pro-Q 1 stores 0.57519 and Pro-Q 3 stores log2 1000. If the frequency
    arithmetic here is wrong, these stop agreeing.
    """
    default = band(pro_q1_to_pro_q3_parameters(q1_bank(Q1_DEFAULT_BAND)), 0)
    assert 2 ** default[Q3BandField.FREQUENCY] == pytest.approx(1000.0, rel=1e-4)
    assert 2 ** PRO_Q3_DEFAULT_BAND[Q3BandField.FREQUENCY] == pytest.approx(1000.0, rel=1e-6)


# -- what one band becomes --------------------------------------------------


@pytest.mark.parametrize(
    ("normalized", "hertz"),
    [(0.0, 10.0), (0.5, 10.0 * math.sqrt(3000.0)), (1.0, 30000.0)],
)
def test_a_frequency_spans_ten_hertz_to_thirty_kilohertz_logarithmically(normalized: float, hertz: float) -> None:
    assert 2 ** converted(Q1BandField.FREQUENCY, normalized)[Q3BandField.FREQUENCY] == pytest.approx(hertz, rel=1e-4)


def test_the_top_of_the_range_lands_on_pro_q3_s_own_ceiling() -> None:
    """The two versions agree on 30 kHz to within 0.02 Hz, and Pro-Q 3 wins.

    Writing the arithmetically correct number instead costs an exact readback on
    every patch with a band at the top of the range -- three of the collection's
    138 -- for a difference nothing could hear.
    """
    top = converted(Q1BandField.FREQUENCY, 1.0)[Q3BandField.FREQUENCY]
    assert top == PRO_Q3_MAX_FREQUENCY_LOG2
    assert top < math.log2(30000.0)
    assert 2**top == pytest.approx(30000.0, abs=0.05)


@pytest.mark.parametrize(("normalized", "decibels"), [(0.0, -30.0), (0.5, 0.0), (1.0, 30.0)])
def test_a_gain_is_centred_on_zero_and_reaches_thirty_either_way(normalized: float, decibels: float) -> None:
    assert converted(Q1BandField.GAIN, normalized)[Q3BandField.GAIN] == pytest.approx(decibels)


def test_q_and_enabled_cross_unchanged() -> None:
    """The two slots that needed no arithmetic, which is itself a measurement."""
    source = with_field(with_field(Q1_DEFAULT_BAND, Q1BandField.Q, 0.75), Q1BandField.ENABLED, 0.0)
    quiet = band(pro_q1_to_pro_q3_parameters(q1_bank(source)), 0)
    assert quiet[Q3BandField.Q] == pytest.approx(0.75)
    assert quiet[Q3BandField.ENABLED] == 0.0
    assert quiet[Q3BandField.USED] == 1.0


@pytest.mark.parametrize("shape", range(len(_Q1_TO_Q3_SHAPE)))
def test_every_shape_pro_q1_offers_lands_on_the_same_shape_in_pro_q3(shape: int) -> None:
    """Pro-Q 3 added shapes and reordered none, so the five it inherited map straight across."""
    normalized = shape / (len(_Q1_TO_Q3_SHAPE) - 1)
    assert converted(Q1BandField.SHAPE, normalized)[Q3BandField.SHAPE] == shape
    assert _Q1_TO_Q3_SHAPE[shape] == shape


@pytest.mark.parametrize(("slope", "expected"), list(enumerate(_Q1_TO_Q3_SLOPE)))
def test_every_slope_pro_q1_offers_finds_its_own_number_of_decibels(slope: int, expected: int) -> None:
    """The edge that would go unnoticed: Pro-Q 1's 24 dB/oct is Pro-Q 3's slope 3, not its slope 2."""
    normalized = slope / (len(_Q1_TO_Q3_SLOPE) - 1)
    assert converted(Q1BandField.SLOPE, normalized)[Q3BandField.SLOPE] == expected


def test_the_slope_map_is_the_one_the_plugins_print() -> None:
    assert _Q1_TO_Q3_SLOPE == (0, 1, 3, 6)


@pytest.mark.parametrize("placement", [0.0, 0.25, 0.5, 0.75, 1.0])
def test_every_placement_collapses_to_stereo(placement: float) -> None:
    """Pro-Q 1's non-stereo encoding is unresolved; a guess would silently move a band to one channel."""
    stereo = PRO_Q3_DEFAULT_BAND[Q3BandField.PLACEMENT]
    assert converted(Q1BandField.PLACEMENT, placement)[Q3BandField.PLACEMENT] == stereo == 2.0


@pytest.mark.parametrize("field", [Q1BandField.SHAPE, Q1BandField.SLOPE])
def test_an_enum_slot_outside_its_range_is_refused(field: Q1BandField) -> None:
    with pytest.raises(StateTransformError, match="settings"):
        pro_q1_to_pro_q3_parameters(q1_bank(with_field(Q1_DEFAULT_BAND, field, 4.0)))


# -- and what the rest of the array does ------------------------------------


def test_bands_past_the_used_count_keep_pro_q3_s_defaults() -> None:
    """A leftover band would be an EQ curve nobody drew."""
    parameters = pro_q1_to_pro_q3_parameters(q1_bank(with_field(Q1_DEFAULT_BAND, Q1BandField.GAIN, 1.0)))
    assert band(parameters, 0)[Q3BandField.GAIN] == pytest.approx(30.0)
    for index in range(1, PRO_Q3_BANDS):
        assert band(parameters, index) == PRO_Q3_DEFAULT_BAND


def test_a_bank_using_no_bands_is_pro_q3_untouched() -> None:
    assert pro_q1_to_pro_q3_parameters(q1_bank()) == PRO_Q3_DEFAULT_PARAMETERS


def test_a_bank_using_every_band_fills_every_band() -> None:
    parameters = pro_q1_to_pro_q3_parameters(q1_bank(*[Q1_DEFAULT_BAND] * PRO_Q1_BANDS))
    assert all(band(parameters, index)[Q3BandField.USED] == 1.0 for index in range(PRO_Q3_BANDS))


def test_pro_q3_s_globals_survive_a_conversion() -> None:
    """Pro-Q 1 has no counterpart for any of them, so they stay as the plugin ships them."""
    parameters = pro_q1_to_pro_q3_parameters(q1_bank(Q1_DEFAULT_BAND))
    used = PRO_Q3_BANDS * PRO_Q3_BAND_SLOTS
    assert parameters[used:] == PRO_Q3_DEFAULT_PARAMETERS[used:]


@pytest.mark.parametrize("count", [PRO_Q1_PARAMETERS - 1, PRO_Q1_PARAMETERS + 1, 46])
def test_a_bank_of_the_wrong_size_is_refused(count: int) -> None:
    """46 is Pro-C 2's bank. Reading it as Pro-Q 1's would land on a patch nobody chose."""
    with pytest.raises(StateTransformError, match=f"{PRO_Q1_PARAMETERS} parameters"):
        pro_q1_to_pro_q3_parameters((0.0,) * count)


# -- the transform a config entry reaches -----------------------------------


def test_the_transform_writes_what_a_vst3_processor_state_holds() -> None:
    """Header, float count and trailer, which is what makes it loadable at all."""
    payload = pro_q1_to_pro_q3(LegacyBank("Default Setting", q1_bank(Q1_DEFAULT_BAND)).encode())
    assert len(payload) == 1456
    state = FfbsState.parse(payload)
    assert state.version == PRO_Q3_STATE_VERSION
    assert len(state.parameters) == PRO_Q3_PARAMETERS
    assert state.tail == FABFILTER_PROCESSOR_TRAILER


def test_the_transform_is_registered_under_the_name_a_config_entry_writes() -> None:
    assert PRO_Q1_TO_PRO_Q3 in registered_custom_states()
    assert parse_state(f"custom:{PRO_Q1_TO_PRO_Q3}") == CustomState(PRO_Q1_TO_PRO_Q3)
    assert custom_state(PRO_Q1_TO_PRO_Q3) is pro_q1_to_pro_q3


@pytest.mark.parametrize("spelling", ["FabFilter Pro-Q", "FabFilter Pro-Q.64"])
def test_a_re_encoded_conversion_is_the_one_kind_of_re_encode_that_is_predictable(spelling: str) -> None:
    """The rung says the bytes cannot cross; the ear says the transform carries them.

    Every other re-encode row is an experiment, because nothing is written for
    it. This one was heard in Live, so a repair run stops printing it as one.
    """
    record = measured_state(spelling)
    assert record.rung is StateRung.RE_ENCODE
    assert record.evidence == (StateEvidence.EAR, StateEvidence.HOSTED)
    assert record.predictable
    assert measured_state("FabFilter Pro-Q 2 x64").rung is StateRung.RE_ENCODE
    assert not measured_state("FabFilter Pro-Q 2 x64").predictable


def test_a_pro_q1_device_comes_out_of_a_translation_as_a_pro_q3_patch() -> None:
    """End to end through the container rewrite, which is where this has to work."""
    live_set = AbletonSet(SKELETONS / "11.3.42.als")
    assert live_set.parse()
    (info,) = [
        element
        for element in live_set.root.iter("VstPluginInfo")
        if get_element(element, "PlugName", attribute="Value") == "Serum_x64"
    ]
    preset = get_element(info, "Preset.VstPreset")
    bank = LegacyBank("Default Setting", q1_bank(with_field(Q1_DEFAULT_BAND, Q1BandField.GAIN, 1.0)))
    get_element(preset, "Buffer").text = decode_encode.string_to_xml(
        bank.encode().hex().upper(), levels=(preset.text or "").count("\t") + 1
    )

    target = TranslationTarget(PluginKind.VST3, "Pro-Q 3", (1, 2, 3, 4), CustomState(PRO_Q1_TO_PRO_Q3))
    translate_device(info, target)

    state = get_element(info, "Preset.Vst3Preset.ProcessorState")
    assert state.text is not None
    converted = FfbsState.parse(bytes.fromhex(decode_encode.xml_to_string(state.text)[0]))
    assert converted.parameters[Q3BandField.GAIN] == pytest.approx(30.0)
    assert converted.tail == FABFILTER_PROCESSOR_TRAILER


# -- the derived side: Pro-C 2 ----------------------------------------------
# Nothing below was reasoned out either. The table is what the derivation rig
# measured off the two installed Pro-C 2 binaries, and these tests check that
# the library evaluates it the way the rig did -- the rig's verification is what
# says the table is right, and this is what says the reading of it is.


def pro_c_2() -> DerivedTable:
    return read_derived_table(DERIVED_TABLES / "pro-c-2.json")


def test_a_fabf_state_round_trips() -> None:
    state = FabfState(
        version=2, preset_name="Bus Glue", leading=0, parameters=(1.0, -18.0), trailing=(1, 1)
    )
    assert FabfState.parse(state.encode()) == state


def test_a_fabf_state_refuses_what_is_not_one() -> None:
    with pytest.raises(StateTransformError, match="is not FabF"):
        FabfState.parse(b"FFBS" + bytes(20))
    with pytest.raises(StateTransformError, match="runs past"):
        FabfState.parse(FABF_MAGIC + struct.pack("<II", 2, 999) + bytes(12))
    with pytest.raises(StateTransformError, match="overruns"):
        FabfState.parse(FABF_MAGIC + struct.pack("<II", 2, 0) + struct.pack("<II", 0, 99))


@pytest.mark.parametrize(("normalized", "expected"), [(0.0, -60.0), (0.5, -30.0), (1.0, 0.0)])
def test_a_linear_transfer_is_the_line_through_what_the_plugin_wrote(normalized: float, expected: float) -> None:
    assert LinearTransfer(intercept=-60.0, slope=60.0)(normalized) == pytest.approx(expected)


def test_a_step_transfer_rounds_half_up_rather_than_to_even() -> None:
    """Measured: a two-way switch at exactly 0.5 reads as on.

    Python's round would call it off, and 22 of Pro-C 2's parameters would stop
    reproducing the plugin's own sweep.
    """
    switch = StepTransfer(values=(0.0, 1.0))
    assert switch(0.5) == 1.0
    assert switch(0.49) == 0.0
    assert switch(0.0) == 0.0
    assert switch(1.0) == 1.0


def test_a_step_transfer_clamps_outside_its_range() -> None:
    assert StepTransfer(values=(0.0, 1.0, 2.0))(2.0) == 2.0
    assert StepTransfer(values=(0.0, 1.0, 2.0))(-1.0) == 0.0


def test_a_table_transfer_interpolates_between_measured_points() -> None:
    curve = TableTransfer(positions=(0.0, 0.5, 1.0), values=(0.0, 10.0, 12.0))
    assert curve(0.25) == pytest.approx(5.0)
    assert curve(0.75) == pytest.approx(11.0)
    assert curve(-1.0) == 0.0
    assert curve(2.0) == 12.0


def test_a_transfer_model_nothing_here_evaluates_is_refused() -> None:
    with pytest.raises(StateTransformError, match="not a transfer model"):
        _read_transfer({"kind": "polynomial"})


def test_the_derived_table_accounts_for_every_slot_and_every_bank_float() -> None:
    """A slot two parameters own, or one nobody owns, is a hole in the derivation."""
    table = pro_c_2()
    assert table.product == "Pro-C 2"
    assert len(table.parameters) == 46
    assert sorted(parameter.slot for parameter in table.parameters) == list(range(46))
    assert sorted(parameter.bank_index for parameter in table.parameters) == list(range(46))
    assert len(table.defaults) == 46
    assert table.trailing == (1, 1)


def test_a_pro_c_2_bank_becomes_the_state_its_vst3_holds() -> None:
    """212 bytes of normalized floats in, the 227 byte FabF container out."""
    bank = LegacyBank("Default Setting", tuple(0.5 for _ in range(46)))
    payload = custom_state(PRO_C_2)(bank.encode())
    assert len(payload) == 227
    state = FabfState.parse(payload)
    assert state.version == 2
    assert state.preset_name == "Default Setting"
    assert len(state.parameters) == 46
    assert state.trailing == (1, 1)


def test_a_converted_patch_keeps_its_preset_name() -> None:
    """Unlike Pro-Q 3, this container has somewhere to put it."""
    bank = LegacyBank("Snare Glue", tuple(0.5 for _ in range(46)))
    assert FabfState.parse(custom_state(PRO_C_2)(bank.encode())).preset_name == "Snare Glue"


def test_a_named_parameter_lands_on_the_number_the_plugin_prints() -> None:
    """Threshold sweeps -60 dB to 0 dB, and the state holds decibels."""
    table = pro_c_2()
    (threshold,) = [parameter for parameter in table.parameters if parameter.name == "Threshold"]
    quiet = LegacyBank("x", tuple(0.0 if index == threshold.bank_index else 0.5 for index in range(46)))
    state = FabfState.parse(table.convert(quiet.encode()))
    assert state.parameters[threshold.slot] == pytest.approx(-60.0)


def test_a_bank_too_short_for_the_table_is_refused() -> None:
    """Another product's bank read as this one would land on a patch nobody chose."""
    with pytest.raises(StateTransformError, match="not 9"):
        custom_state(PRO_C_2)(LegacyBank("x", tuple(0.5 for _ in range(9))).encode())


def test_the_derived_transform_is_registered_too() -> None:
    assert PRO_C_2 in registered_custom_states()
    assert parse_state(f"custom:{PRO_C_2}") == CustomState(PRO_C_2)


def test_pro_c_2_stays_an_experiment_until_somebody_listens() -> None:
    """The rig proved the plugin reads it back; nothing has heard it.

    That is the whole point of MODEL.md's split between hosted and ear.
    """
    record = measured_state("FabFilter Pro-C 2")
    assert record.rung is StateRung.RE_ENCODE
    assert StateEvidence.HOSTED in record.evidence
    assert not record.predictable


# -- the cross-version join: Pro-C 1 to Pro-C 2 -----------------------------
# The source side of this one came out of Live's own ParameterList records
# rather than out of the plugin, because Pro-C 1's VST2 is 32-bit and no host
# here can open it. What that means for these tests is that the layout claim --
# 31 parameters in the order the names are listed -- is the thing worth pinning
# down, so every bank below is built from that order by name.

# One Pro-C 1 bank with something in every path the join has: a characteristic
# off its first setting, both side-chain filters moved in, a dropped switch
# turned on, and a meter scale that has to be re-indexed.
PRO_C_1_SAMPLE: dict[str, float] = {
    "Characteristic": 0.5,
    "Threshold": 0.25,
    "Ratio": 0.75,
    "Knee Shape": 1.0,
    "Attack": 0.125,
    "Release": 0.375,
    "Input Level": 0.5,
    "Input Panning": 0.5,
    "Left Side Chain Level": 0.5,
    "Left Side Chain Mix": 0.5,
    "Right Side Chain Level": 0.5,
    "Right Side Chain Mix": 0.5,
    "Output Level": 0.5,
    "Output Panning": 0.5,
    "Dry Level": 0.0,
    "Dry Panning": 0.5,
    "Auto Gain": 1.0,
    "Auto Release": 0.0,
    "Auto Release Speed": 0.5,
    "Input signal": 0.0,
    "Low Pass Frequency": 0.75,
    "High Pass Frequency": 0.25,
    "Audition Side Chain": 0.0,
    "Channel Processing": 0.0,
    "Receive Midi": 0.0,
    "Expert Mode": 1.0,
    "Interface: Opacity Input": 0.5,
    "Interface: Opacity Output": 0.5,
    "Interface: Opacity Gain Change": 1.0,
    "Interface: Meter Scale": 2 / 3,
    "Interface: Display Enabled": 1.0,
}

# What the library makes of it, byte for byte. Written down so that a change to
# the join, to a curve or to the container fails here rather than in somebody's
# set -- these are the bytes the installed Pro-C 2 has to hand back unchanged.
PRO_C_1_SAMPLE_STATE = (
    "46616246020000000800000042757320476C7565000000002E0000000000803F000034C20000403F00009041"
    "000070420000003E0000C03E0000000000000000000000000000000000000000000080BF000000000000803F"
    "0000803F00000000000000000000003F000000000000803F2907DA40000040400000803F0000803FDA731F41"
    "000000000000003F000000000000803F82BD3C410000404000000000000000000000803F0000000000000000"
    "000000000000000000000000000000000000000000000000000000400000803F0000803F0100000001000000"
)


def pro_c_1_bank(name: str = "Bus Glue", **changed: float) -> LegacyBank:
    """A Pro-C 1 bank in the parameter order Live recorded for it."""
    values = PRO_C_1_SAMPLE | changed
    return LegacyBank(name, tuple(values[parameter] for parameter in PRO_C_1_PARAMETERS))


def converted_pro_c_1(**changed: float) -> dict[str, float]:
    """One converted bank, read back as {Pro-C 2 parameter: what the state holds}."""
    state = FabfState.parse(custom_state(PRO_C_1_TO_PRO_C_2_STATE)(pro_c_1_bank(**changed).encode()))
    return {parameter.name: state.parameters[parameter.slot] for parameter in PRO_C_2_TABLE.parameters}


def test_a_pro_c_1_bank_is_thirty_one_floats_behind_a_name() -> None:
    """152 bytes in a set: the 28 byte name field and one float per parameter."""
    payload = pro_c_1_bank().encode()
    assert len(payload) == LEGACY_NAME_BYTES + 31 * 4
    assert len(PRO_C_1_PARAMETERS) == 31
    bank = LegacyBank.parse(payload)
    assert bank.preset_name == "Bus Glue"
    assert bank.parameters[PRO_C_1_PARAMETERS.index("Threshold")] == pytest.approx(0.25)
    assert bank.parameters[PRO_C_1_PARAMETERS.index("Interface: Display Enabled")] == 1.0


def test_every_pro_c_1_parameter_is_either_joined_or_listed_as_dropped() -> None:
    """The whole point of writing the drops down: nothing may fall out silently."""
    joined = [source for source, _ in PRO_C_1_TO_PRO_C_2]
    dropped = [source for source, _ in PRO_C_1_DROPPED]
    assert sorted(joined + dropped) == sorted(PRO_C_1_PARAMETERS)
    assert not set(joined) & set(dropped)
    assert all(reason for _, reason in PRO_C_1_DROPPED)


def test_no_pro_c_2_parameter_is_claimed_twice() -> None:
    """Two sources on one target is one of them silently losing."""
    targets = [target for _, target in PRO_C_1_TO_PRO_C_2]
    flags = [flag for _, flag, _ in PRO_C_1_FILTER_SWITCHES]
    assert len(set(targets + flags)) == len(targets) + len(flags)


def test_every_name_the_join_uses_exists_on_the_product_it_names() -> None:
    """A typo either side would write a knob nobody asked for, or none at all."""
    for source, target in PRO_C_1_TO_PRO_C_2:
        assert source in PRO_C_1_PARAMETERS
        assert PRO_C_2_TABLE.parameter(target).name == target
    for source, flag, _ in PRO_C_1_FILTER_SWITCHES:
        assert source in PRO_C_1_PARAMETERS
        assert PRO_C_2_TABLE.parameter(flag).name == flag
    for source, _, _ in PRO_C_1_ENUM_WIDENING:
        assert source in PRO_C_1_PARAMETERS


def test_a_parameter_the_product_does_not_have_is_refused() -> None:
    with pytest.raises(StateTransformError, match="no parameter named"):
        PRO_C_2_TABLE.parameter("Wobble")


def test_a_converted_bank_is_a_state_the_library_reads_back() -> None:
    """Round trip through the container, which is what the plugin is handed."""
    payload = custom_state(PRO_C_1_TO_PRO_C_2_STATE)(pro_c_1_bank().encode())
    state = FabfState.parse(payload)
    assert state.encode() == payload
    assert payload[:4] == FABF_MAGIC
    assert state.version == 2
    assert len(state.parameters) == 46
    assert state.trailing == (1, 1)


def test_a_converted_bank_keeps_the_name_pro_c_1_gave_it() -> None:
    """The FabF container has a field for it, so this one needs no editor state."""
    payload = custom_state(PRO_C_1_TO_PRO_C_2_STATE)(pro_c_1_bank("Snare Glue").encode())
    assert FabfState.parse(payload).preset_name == "Snare Glue"


def test_the_conversion_is_the_bytes_the_plugin_echoed() -> None:
    """189 of 189 real patches came back from the installed Pro-C 2 unchanged.

    This is one synthesized patch through the same path, written down. A curve,
    a pairing or a container word that moves fails here first.
    """
    payload = custom_state(PRO_C_1_TO_PRO_C_2_STATE)(pro_c_1_bank().encode())
    assert payload.hex().upper() == PRO_C_1_SAMPLE_STATE


def test_a_bank_of_the_wrong_size_is_not_read_as_pro_c_1() -> None:
    """Pro-C 2's own bank is 46 floats and would decode as a patch nobody chose."""
    with pytest.raises(StateTransformError, match="31 parameters, not 46"):
        custom_state(PRO_C_1_TO_PRO_C_2_STATE)(LegacyBank("x", tuple(0.5 for _ in range(46))).encode())


@pytest.mark.parametrize(("characteristic", "style"), [(0.0, 0.0), (0.5, 1.0), (1.0, 2.0)])
def test_pro_c_1_s_three_characteristics_land_on_pro_c_2_s_first_three_styles(
    characteristic: float, style: float
) -> None:
    """The widening, and the assumption in it.

    Live stores an enum as its index over its top index, so 0.5 of three
    settings is 1 and 0.5 of eight is 3.5. Passing the number through would put
    a Pro-C 1 patch on Pro-C 2's fifth style.
    """
    assert converted_pro_c_1(Characteristic=characteristic)["Style"] == style


@pytest.mark.parametrize("scale", [0.0, 1 / 3, 2 / 3, 1.0])
def test_the_meter_scale_is_re_indexed_rather_than_rescaled(scale: float) -> None:
    """Four settings against five, and the state holds an index either way."""
    expected = float(round(scale * 3))
    assert converted_pro_c_1(**{"Interface: Meter Scale": scale})["Meter Scale"] == expected


def test_a_filter_left_open_converts_to_a_band_pro_c_2_has_switched_off() -> None:
    """Pro-C 1's filters are always in circuit; Pro-C 2's are gated by a flag."""
    values = converted_pro_c_1(**{"High Pass Frequency": 0.0, "Low Pass Frequency": 1.0})
    assert values["Side Chain Low Enabled"] == 0.0
    assert values["Side Chain High Enabled"] == 0.0


def test_a_filter_that_was_doing_something_arrives_switched_on() -> None:
    values = converted_pro_c_1(**{"High Pass Frequency": 0.25, "Low Pass Frequency": 0.75})
    assert values["Side Chain Low Enabled"] == 1.0
    assert values["Side Chain High Enabled"] == 1.0
    # Pro-C 1's high pass is Pro-C 2's low band and its low pass the high band,
    # which is the pairing most likely to be read the wrong way round.
    assert values["Side Chain Low Frequency"] < values["Side Chain High Frequency"]


def test_pro_c_1_s_centres_land_on_the_defaults_pro_c_2_ships() -> None:
    """The one range check the corpus can make, and every one of them passes."""
    values = converted_pro_c_1()
    defaults = {
        parameter.name: PRO_C_2_TABLE.defaults[parameter.slot] for parameter in PRO_C_2_TABLE.parameters
    }
    centred = ("Input Level", "Output Level", "Input Pan", "Output Pan", "Dry Pan", "Side Chain Level")
    for name in centred:
        assert values[name] == pytest.approx(defaults[name]), name
    assert values["Dry Gain"] == pytest.approx(defaults["Dry Gain"])


def test_a_dropped_parameter_leaves_pro_c_2_s_own_default_alone() -> None:
    """Knee Shape is a switch and Pro-C 2's Knee is 0 to 72 dB of width."""
    assert converted_pro_c_1(**{"Knee Shape": 1.0})["Knee"] == pytest.approx(18.0)
    assert converted_pro_c_1(**{"Knee Shape": 0.0})["Knee"] == pytest.approx(18.0)


def test_a_threshold_crosses_through_pro_c_2_s_own_measured_curve() -> None:
    """Assumed to mean the same thing on both sides; the arithmetic is measured."""
    assert converted_pro_c_1(Threshold=0.0)["Threshold"] == pytest.approx(-60.0)
    assert converted_pro_c_1(Threshold=1.0)["Threshold"] == pytest.approx(0.0)


def test_the_join_is_registered_under_the_name_a_config_entry_writes() -> None:
    assert PRO_C_1_TO_PRO_C_2_STATE in registered_custom_states()
    assert parse_state(f"custom:{PRO_C_1_TO_PRO_C_2_STATE}") == CustomState(PRO_C_1_TO_PRO_C_2_STATE)


@pytest.mark.parametrize("spelling", ["FabFilter Pro-C", "FabFilter Pro-C.64"])
def test_both_spellings_of_pro_c_1_are_measured_and_still_want_a_listen(spelling: str) -> None:
    """The plugin read all 189 back; nobody has heard one."""
    record = measured_state(spelling)
    assert record.rung is StateRung.RE_ENCODE
    assert StateEvidence.HOSTED in record.evidence
    assert not record.predictable
