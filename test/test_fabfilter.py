"""The first re-encode: a Pro-Q 1 bank becoming a Pro-Q 3 processor state.

Nothing here reads a set. Every bank is synthesized from the layout the module
documents, and the arithmetic is checked against values that can be reasoned
about -- 1 kHz, 0 dB, a named shape -- rather than against a blob harvested from
somebody's project. The one number that does come from outside is Pro-Q 3's own
default state, read off the installed plugin, and the test that matters most
here is the one that catches it drifting: Pro-Q 1's default band and Pro-Q 3's
default band are the same 1 kHz bell, reached through two unrelated formulas.

How an FFBS plugin's VST2 chunk splits into its VST3 halves is in
``test_plugin_maps``, with the other crossings between one plugin's formats.
"""

from __future__ import annotations

import json
import math
import pathlib
import struct
from collections.abc import Callable

import pytest
from conftest import parse_editor_state, pro_q2_bank, skeleton_set

from abletoolz.elements import get_element
from abletoolz.formats import hex_text
from abletoolz.plugins.container.device import PluginDevice
from abletoolz.plugins.conversion import as_plugin, write
from abletoolz.plugins.converters.fabfilter import (
    PRO_Q3_MAX_FREQUENCY_LOG2,
    pro_q1_to_pro_q3,
    pro_q1_to_pro_q3_parameters,
)
from abletoolz.plugins.identity.ids import PluginKind, Vst2Id
from abletoolz.plugins.identity.vst3_parameters import no_parameters
from abletoolz.plugins.plan import known_state
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.derived import (
    DERIVED_TABLES,
    FABF_MAGIC,
    FabfState,
    LinearTransfer,
    StepTransfer,
    read_derived_table,
)
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.fabfilter import (
    EDITOR_STATE_VERSION,
    FABFILTER_CONTROLLER_TRAILER,
    FABFILTER_PROCESSOR_TRAILER,
    FFBS_MAGIC,
    NO_INSTANCE,
    PRO_Q1_BANDS,
    PRO_Q1_DEFAULT_GLOBALS,
    PRO_Q1_GLOBALS,
    PRO_Q1_PARAMETERS,
    PRO_Q3_BAND_SLOTS,
    PRO_Q3_BANDS,
    PRO_Q3_DEFAULT_BAND,
    PRO_Q3_DEFAULT_PARAMETERS,
    PRO_Q3_EDITOR_MAGIC,
    PRO_Q3_PARAMETERS,
    PRO_Q3_STATE_VERSION,
    TIMELESS_3_EDITOR_MAGIC,
    EditorState,
    FfbsState,
    Q1BandField,
    Q3BandField,
)
from abletoolz.plugins.state.fxbk import LEGACY_NAME_BYTES, LegacyBank
from abletoolz.plugins.state.measured import StateEvidence, StateRung, measured_state
from abletoolz.plugins.state.plugin_map import FormatState

# Pro-Q 1's own default band, as its bank stores it: 1 kHz, 0 dB, a bell at
# 24 dB/oct, stereo, on. Every synthesized bank starts from this.
Q1_DEFAULT_BAND = (0.57519, 0.5, 0.5, 0.0, 2 / 3, 1.0, 1.0)


def q1_bank(*bands: tuple[float, ...]) -> tuple[float, ...]:
    """A Pro-Q 1 parameter bank using ``bands`` and defaulting the rest."""
    used = (len(bands) / PRO_Q1_BANDS,)
    rest = (Q1_DEFAULT_BAND,) * (PRO_Q1_BANDS - len(bands))
    return used + tuple(value for band in (*bands, *rest) for value in band) + PRO_Q1_DEFAULT_GLOBALS


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


@pytest.mark.parametrize(
    "state",
    [
        editor_state("Smashing Compression", instance_index=2, label="Drums"),
        # Timeless 3 keeps its XY pads and envelope followers here, named, an empty name among them.
        editor_state(
            "Flutter Machine MdB",
            magic=TIMELESS_3_EDITOR_MAGIC,
            controllers=(("XY1", "Ducking"), ("EF2", ""), ("XLFO1", "Random")),
        ),
    ],
    ids=["named and labelled", "with named controllers"],
)
def test_an_editor_state_round_trips(state: EditorState) -> None:
    assert parse_editor_state(state.encode()) == state


# -- the defaults the remap builds on ---------------------------------------


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


# (Pro-Q 1 slot, what its bank stores, Pro-Q 3 slot, what Pro-Q 3 stores for it)
BAND_SLOTS = [
    # A gain is centred on zero and reaches thirty decibels either way.
    pytest.param(Q1BandField.GAIN, 0.0, Q3BandField.GAIN, -30.0, id="gain -30 dB"),
    pytest.param(Q1BandField.GAIN, 0.5, Q3BandField.GAIN, 0.0, id="gain 0 dB"),
    pytest.param(Q1BandField.GAIN, 1.0, Q3BandField.GAIN, 30.0, id="gain +30 dB"),
    # The two slots that needed no arithmetic, which is itself a measurement.
    pytest.param(Q1BandField.Q, 0.75, Q3BandField.Q, 0.75, id="Q unchanged"),
    pytest.param(Q1BandField.ENABLED, 0.0, Q3BandField.ENABLED, 0.0, id="a band switched off stays off"),
    pytest.param(Q1BandField.PLACEMENT, 1.0, Q3BandField.PLACEMENT, 2.0, id="stereo placement"),
    # Pro-Q 3 added shapes and reordered none, so the five it inherited map straight across.
    *(pytest.param(Q1BandField.SHAPE, shape / 4, Q3BandField.SHAPE, shape, id=f"shape {shape}") for shape in range(5)),
    # The edge that would go unnoticed: Pro-Q 1's 24 dB/oct is Pro-Q 3's slope 3, not its slope 2.
    # These are the slopes both plugins print.
    *(
        pytest.param(Q1BandField.SLOPE, slope / 3, Q3BandField.SLOPE, q3, id=f"slope {slope} is Pro-Q 3's {q3}")
        for slope, q3 in enumerate((0, 1, 3, 6))
    ),
]


@pytest.mark.parametrize(("source", "stored", "target", "expected"), BAND_SLOTS)
def test_a_band_slot_lands_on_what_pro_q3_prints(
    source: Q1BandField, stored: float, target: Q3BandField, expected: float
) -> None:
    assert converted(source, stored)[target] == pytest.approx(expected)


@pytest.mark.parametrize("placement", [0.0, 0.25, 0.5, 0.75])
def test_pro_q1_unmapped_placement_refuses_conversion(placement: float) -> None:
    source = LegacyBank("Non-stereo", q1_bank(with_field(Q1_DEFAULT_BAND, Q1BandField.PLACEMENT, placement)))
    raw = source.encode()
    with pytest.raises(StateTransformError, match="non-stereo band placement"):
        pro_q1_to_pro_q3(raw)
    assert source.encode() == raw


@pytest.mark.parametrize("field", [Q1BandField.SHAPE, Q1BandField.SLOPE])
def test_an_enum_slot_outside_its_range_is_refused(field: Q1BandField) -> None:
    with pytest.raises(StateTransformError, match="normalized"):
        pro_q1_to_pro_q3_parameters(q1_bank(with_field(Q1_DEFAULT_BAND, field, 4.0)))


# -- and what the rest of the array does ------------------------------------


def test_bands_past_the_used_count_keep_pro_q3_s_defaults() -> None:
    """A leftover band would be an EQ curve nobody drew."""
    parameters = pro_q1_to_pro_q3_parameters(q1_bank(with_field(Q1_DEFAULT_BAND, Q1BandField.GAIN, 1.0)))
    assert band(parameters, 0)[Q3BandField.GAIN] == pytest.approx(30.0)
    for index in range(1, PRO_Q3_BANDS):
        assert band(parameters, index) == PRO_Q3_DEFAULT_BAND


def test_a_bank_using_no_bands_keeps_pro_q3_s_bands() -> None:
    parameters = pro_q1_to_pro_q3_parameters(q1_bank())
    used = PRO_Q3_BANDS * PRO_Q3_BAND_SLOTS
    assert parameters[:used] == PRO_Q3_DEFAULT_PARAMETERS[:used]


def test_a_bank_using_every_band_fills_every_band() -> None:
    parameters = pro_q1_to_pro_q3_parameters(q1_bank(*[Q1_DEFAULT_BAND] * PRO_Q1_BANDS))
    assert all(band(parameters, index)[Q3BandField.USED] == 1.0 for index in range(PRO_Q3_BANDS))


def with_global(bank: tuple[float, ...], index: int, value: float) -> tuple[float, ...]:
    """A Pro-Q 1 bank with one global, counted from the first, changed."""
    changed = list(bank)
    changed[PRO_Q1_PARAMETERS - PRO_Q1_GLOBALS + index] = value
    return tuple(changed)


def converted_globals(index: int, value: float) -> tuple[float, ...]:
    """Pro-Q 3's globals for a one-band bank whose global ``index`` says ``value``."""
    parameters = pro_q1_to_pro_q3_parameters(with_global(q1_bank(Q1_DEFAULT_BAND), index, value))
    return parameters[PRO_Q3_BANDS * PRO_Q3_BAND_SLOTS :]


# Pro-Q 3's global slots, counted from the first, as the Pro-Q 2 map writes them.
Q3_MODE, Q3_RESOLUTION, Q3_OUTPUT_LEVEL, Q3_PAN, Q3_PAN_MODE, Q3_BYPASS = 0, 1, 3, 4, 5, 6
Q3_ANALYZER_PRE, Q3_ANALYZER_POST, Q3_DISPLAY_RANGE, Q3_RECEIVE_MIDI = 9, 10, 19, 20

# (Pro-Q 1 global, what its bank stores, {Pro-Q 3 global: what Pro-Q 3 stores}),
# each case a setting the original Pro-Q 1 printed when swept on 2026-10-28.
GLOBALS = [
    pytest.param(0, 0.25, {Q3_OUTPUT_LEVEL: -0.5}, id="output gain -18 dB"),
    pytest.param(0, 1.0, {Q3_OUTPUT_LEVEL: 1.0}, id="output gain +36 dB"),
    pytest.param(1, 0.0, {Q3_PAN: -1.0}, id="output pan -1"),
    pytest.param(1, 0.75, {Q3_PAN: 0.5}, id="output pan 0.5"),
    pytest.param(2, 0.0, {Q3_DISPLAY_RANGE: 1.0}, id="display range 6 dB"),
    pytest.param(2, 1.0, {Q3_DISPLAY_RANGE: 3.0}, id="display range 30 dB"),
    pytest.param(3, 0.0, {Q3_MODE: 0.0, Q3_RESOLUTION: 1.0}, id="zero latency"),
    pytest.param(3, 0.25, {Q3_MODE: 2.0, Q3_RESOLUTION: 0.0}, id="linear phase low"),
    pytest.param(3, 0.75, {Q3_MODE: 2.0, Q3_RESOLUTION: 2.0}, id="linear phase high"),
    # Pro-Q 3 has a Very High that Pro-Q 1 never offered.
    pytest.param(3, 1.0, {Q3_MODE: 2.0, Q3_RESOLUTION: 4.0}, id="linear phase maximum"),
    pytest.param(4, 1.0, {Q3_PAN_MODE: 1.0}, id="mid/side"),
    pytest.param(5, 1.0, {Q3_BYPASS: 1.0}, id="bypassed"),
    pytest.param(6, 1.0, {Q3_RECEIVE_MIDI: 1.0}, id="midi disabled"),
    pytest.param(7, 0.0, {Q3_ANALYZER_PRE: 0.0, Q3_ANALYZER_POST: 0.0}, id="analyzer off"),
    pytest.param(7, 0.5, {Q3_ANALYZER_PRE: 1.0, Q3_ANALYZER_POST: 0.0}, id="analyzer pre-eq"),
    pytest.param(7, 1.0, {Q3_ANALYZER_PRE: 0.0, Q3_ANALYZER_POST: 1.0}, id="analyzer post-eq"),
]


@pytest.mark.parametrize(("source", "stored", "expected"), GLOBALS)
def test_a_pro_q1_global_lands_on_its_pro_q3_counterpart(
    source: int, stored: float, expected: dict[int, float]
) -> None:
    globals_ = converted_globals(source, stored)
    assert {slot: globals_[slot] for slot in expected} == pytest.approx(expected)


def test_pro_q1_s_default_globals_are_pro_q3_s_but_for_the_analyzer() -> None:
    """Pro-Q 1 shows only the post-EQ spectrum by default; Pro-Q 3 shows both."""
    globals_ = list(converted_globals(0, PRO_Q1_DEFAULT_GLOBALS[0]))
    assert globals_[Q3_ANALYZER_PRE] == 0.0
    globals_[Q3_ANALYZER_PRE] = 1.0
    assert tuple(globals_) == PRO_Q3_DEFAULT_PARAMETERS[PRO_Q3_BANDS * PRO_Q3_BAND_SLOTS :]


@pytest.mark.parametrize("value", [-0.1, 1.5, math.nan])
def test_a_pro_q1_global_outside_zero_to_one_is_refused(value: float) -> None:
    with pytest.raises(StateTransformError, match="normalized"):
        pro_q1_to_pro_q3_parameters(with_global(q1_bank(Q1_DEFAULT_BAND), 3, value))


@pytest.mark.parametrize("count", [PRO_Q1_PARAMETERS - 1, PRO_Q1_PARAMETERS + 1, 46])
def test_a_bank_of_the_wrong_size_is_refused(count: int) -> None:
    """46 is Pro-C 2's bank. Reading it as Pro-Q 1's would land on a patch nobody chose."""
    with pytest.raises(StateTransformError, match=f"{PRO_Q1_PARAMETERS} parameters"):
        pro_q1_to_pro_q3_parameters((0.0,) * count)


@pytest.mark.parametrize(
    ("field", "value"),
    [(Q1BandField.GAIN, 3.0), (Q1BandField.FREQUENCY, math.nan), (Q1BandField.FREQUENCY, -1e30)],
)
def test_a_pro_q1_float_outside_zero_to_one_is_refused(field: Q1BandField, value: float) -> None:
    """Gain 3.0 would be +150 dB and a NaN frequency a band no host can draw."""
    with pytest.raises(StateTransformError, match="normalized"):
        pro_q1_to_pro_q3_parameters(q1_bank(with_field(Q1_DEFAULT_BAND, field, value)))


def test_a_pro_q1_band_count_that_is_not_a_number_is_refused() -> None:
    bank = list(q1_bank(Q1_DEFAULT_BAND))
    bank[0] = math.nan
    with pytest.raises(StateTransformError, match="normalized"):
        pro_q1_to_pro_q3_parameters(bank)


# -- the converters into Pro-Q 3 --------------------------------------------


@pytest.mark.parametrize(
    ("source", "bank"),
    [
        ("fabfilter.pro-q-1", LegacyBank("Smashing Compression", q1_bank(Q1_DEFAULT_BAND))),
        ("fabfilter.pro-q-2", LegacyBank("Recovered patch", tuple(pro_q2_bank()))),
    ],
    ids=["Pro-Q 1", "Pro-Q 2"],
)
def test_a_translation_writes_both_halves_of_pro_q3_s_vst3(source: str, bank: LegacyBank) -> None:
    """Header, float count and trailer, which is what makes it loadable at all.

    The controller half carries the bank's preset name and nothing invented: what
    the int32 after the name indexes is unknown, so nothing here claims one.
    """
    converter = CATALOG.converter(source, "fabfilter.pro-q-3")
    assert converter is not None
    (pro_q_3,) = CATALOG.named("Pro-Q 3")
    written = pro_q_3.lower(converter.convert(bank.encode(), None).patch, PluginKind.VST3)
    assert len(written.state) == 1456
    state = FfbsState.parse(written.state)
    assert state.version == PRO_Q3_STATE_VERSION
    assert len(state.parameters) == PRO_Q3_PARAMETERS
    assert state.tail == FABFILTER_PROCESSOR_TRAILER
    assert parse_editor_state(written.controller) == editor_state(bank.preset_name)


@pytest.mark.parametrize("spelling", ["FabFilter Pro-Q", "FabFilter Pro-Q.64"])
def test_the_pro_q1_translation_is_the_one_re_encode_that_is_predictable(spelling: str) -> None:
    """The rung says the bytes cannot cross; the ear says the converter carries them.

    Every other re-encode is an experiment. This one was heard in Live, so a
    repair run stops printing it as one.
    """
    record = known_state(spelling, "Pro-Q 3", PluginKind.VST3)
    assert record.rung is StateRung.RE_ENCODE
    assert record.evidence == (StateEvidence.EAR, StateEvidence.HOSTED)
    assert record.predictable
    pro_q_2 = known_state("FabFilter Pro-Q 2 x64", "Pro-Q 3", PluginKind.VST3)
    assert pro_q_2.rung is StateRung.RE_ENCODE
    assert not pro_q_2.predictable


def test_a_pro_q1_device_comes_out_of_a_translation_as_a_pro_q3_patch() -> None:
    """End to end through the container rewrite, which is where this has to work."""
    live_set = skeleton_set("11.3.42")
    (info,) = [
        element
        for element in live_set.root.iter("VstPluginInfo")
        if get_element(element, "PlugName", attribute="Value") == "Serum_x64"
    ]
    get_element(info, "UniqueId").set("Value", "1179668850")
    preset = get_element(info, "Preset.VstPreset")
    bank = LegacyBank("Default Setting", q1_bank(with_field(Q1_DEFAULT_BAND, Q1BandField.GAIN, 1.0)))
    get_element(preset, "Buffer").text = hex_text.encode(bank.encode(), (preset.text or "").count("\t") + 1)

    (pro_q_1,) = CATALOG.named("FabFilter Pro-Q")
    (pro_q_3,) = CATALOG.named("Pro-Q 3")
    converter = CATALOG.converter(pro_q_1.id, pro_q_3.id)
    assert converter is not None
    device = PluginDevice(info)
    write(device, as_plugin(device, converter, pro_q_1, pro_q_3, PluginKind.VST3, parameters=no_parameters))

    state = get_element(info, "Preset.Vst3Preset.ProcessorState")
    assert state.text is not None
    converted = FfbsState.parse(hex_text.decode(state.text))
    assert converted.parameters[Q3BandField.GAIN] == pytest.approx(30.0)
    assert converted.tail == FABFILTER_PROCESSOR_TRAILER


# -- the derived side: Pro-C 2, Pro-L 2, Pro-MB, Pro-G ---------------------------
# Nothing below was reasoned out either. Each table is what the derivation rig
# measured off the plugin's two installed binaries, and these tests check that
# the library evaluates it the way the rig did -- the rig's verification is what
# says the table is right, and this is what says the reading of it is.


def test_a_fabf_state_round_trips() -> None:
    state = FabfState(version=2, preset_name="Bus Glue", leading=0, parameters=(1.0, -18.0), trailing=(1, 1))
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


def test_a_linear_transfer_computes_in_float32_like_the_plugin() -> None:
    """Pro-L 2 saves -14.0 for Loudness Meter Target at 0.76666665; double arithmetic says -14.000001."""
    assert LinearTransfer(intercept=-60.0, slope=60.0)(0.7666666507720947) == -14.0
    assert LinearTransfer(intercept=-60.0, slope=60.0)(0.36568892002105713) == -38.05866241455078


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


def edited_table(tmp_path: pathlib.Path, key: str, edit: Callable[[dict[str, object]], object]) -> pathlib.Path:
    """A copy of ``key``'s derived table with ``edit`` made to every parameter entry."""
    document = json.loads((DERIVED_TABLES / f"{key}.json").read_text(encoding="utf-8"))
    for entry in document["parameters"]:
        edit(entry)
    path = tmp_path / f"{key}.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_a_table_with_a_transfer_model_nothing_here_evaluates_is_refused(tmp_path: pathlib.Path) -> None:
    path = edited_table(tmp_path, "pro-c-2", lambda entry: entry.update(transfer={"kind": "polynomial"}))
    with pytest.raises(StateTransformError, match="not a transfer model"):
        read_derived_table(path)


def test_a_table_where_two_parameters_own_one_slot_is_refused(tmp_path: pathlib.Path) -> None:
    """Pro-G's Ex Style is an overlay on Style's slot; read without that, the two collide."""
    path = edited_table(tmp_path, "pro-g", lambda entry: entry.pop("overlay", None))
    with pytest.raises(StateTransformError, match="both own slot 5"):
        read_derived_table(path)


DERIVED_DEFAULTS = json.loads(
    (pathlib.Path(__file__).parent / "state_fixtures" / "fabfilter_derived_defaults.json").read_text(encoding="utf-8")
)

# (VST2 id, bank floats, state slots)
DERIVED_PRODUCTS = {
    "pro-c-2": (1178808944, 46, 46),
    "pro-l-2": (1179398768, 32, 32),
    "pro-mb": (1179667810, 151, 151),
    "pro-g": (1179666290, 35, 34),
}


def derived_vst3(key: str, bank: LegacyBank) -> FormatState:
    """A host bank written as the product's VST3 holds it, through the product's plugin map."""
    plugin_map = CATALOG.find(Vst2Id(DERIVED_PRODUCTS[key][0]))
    assert plugin_map is not None
    return plugin_map.cross(FormatState(bank.encode()), PluginKind.VST, PluginKind.VST3)


def default_bank(key: str, preset_name: str = "Default Setting") -> LegacyBank:
    return LegacyBank(preset_name, tuple(DERIVED_DEFAULTS[key]["vst2_defaults"]))


@pytest.mark.parametrize("key", DERIVED_PRODUCTS)
def test_a_default_bank_becomes_the_default_state_the_plugin_writes(key: str) -> None:
    """Byte for byte, preset name, every slot and the trailing words, as the rig captured them.

    The controller half is the twelve bytes every one of these products writes, whatever the patch.
    """
    _, floats, _ = DERIVED_PRODUCTS[key]
    assert len(DERIVED_DEFAULTS[key]["vst2_defaults"]) == floats
    written = derived_vst3(key, default_bank(key))
    assert written.state == bytes.fromhex(DERIVED_DEFAULTS[key]["vst3_default_state_hex"])
    assert written.controller == FABFILTER_CONTROLLER_TRAILER


@pytest.mark.parametrize("key", DERIVED_PRODUCTS)
def test_a_converted_patch_keeps_its_preset_name(key: str) -> None:
    """Unlike Pro-Q 3, this container has somewhere to put it."""
    assert FabfState.parse(derived_vst3(key, default_bank(key, "Snare Glue")).state).preset_name == "Snare Glue"


@pytest.mark.parametrize("key", DERIVED_PRODUCTS)
def test_every_bank_float_has_one_route_and_every_slot_an_owner(key: str) -> None:
    """A slot two parameters own, or one nobody owns, is a hole in the derivation."""
    _, floats, slots = DERIVED_PRODUCTS[key]
    table = read_derived_table(DERIVED_TABLES / f"{key}.json")
    assert sorted(parameter.bank_index for parameter in table.parameters) == list(range(floats))
    owners = [parameter.slot for parameter in table.parameters if not parameter.overlay]
    assert sorted(owners) == list(range(slots))
    assert len(table.defaults) == slots


def test_a_named_parameter_lands_on_the_number_the_plugin_prints() -> None:
    """Pro-C 2's Threshold sweeps -60 dB to 0 dB, and the state holds decibels."""
    table = read_derived_table(DERIVED_TABLES / "pro-c-2.json")
    (threshold,) = [parameter for parameter in table.parameters if parameter.name == "Threshold"]
    quiet = LegacyBank("x", tuple(0.0 if index == threshold.bank_index else 0.5 for index in range(46)))
    assert FabfState.parse(derived_vst3("pro-c-2", quiet).state).parameters[threshold.slot] == pytest.approx(-60.0)


@pytest.mark.parametrize(
    ("key", "floats"),
    [("pro-c-2", 9), ("pro-l-2", 31), ("pro-mb", 150)],
    ids=["another product's bank", "an older Pro-L 2 build", "an older Pro-MB build"],
)
def test_a_bank_with_no_recorded_layout_is_refused_not_shifted(key: str, floats: int) -> None:
    """Another product's bank read as this one would land on a patch nobody chose.

    Older Pro-L 2 and Pro-MB builds saved one parameter fewer; which one is not measured.
    """
    with pytest.raises(StateTransformError, match="no recorded layout"):
        derived_vst3(key, LegacyBank("x", tuple(0.5 for _ in range(floats))))


@pytest.mark.parametrize("value", [math.nan, 7.0, 3e38, -1.0])
def test_a_derived_bank_float_outside_zero_to_one_is_refused(value: float) -> None:
    """Every curve was swept over 0 to 1; anything else would be written as a value the plugin never holds."""
    floats = list(DERIVED_DEFAULTS["pro-c-2"]["vst2_defaults"])
    floats[0] = value
    with pytest.raises(StateTransformError, match="normalized"):
        derived_vst3("pro-c-2", LegacyBank("x", tuple(floats)))


PRO_G_STYLE, PRO_G_EX_STYLE, PRO_G_STYLE_SLOT = 5, 34, 5


@pytest.mark.parametrize(
    ("style", "ducking", "stored"),
    [(0.0, 0.0, 0.0), (0.25, 0.0, 1.0), (0.5, 0.0, 2.0), (1.0, 0.0, 4.0), (0.5, 1.0, 5.0), (0.0, 1.0, 5.0)],
)
def test_pro_g_keeps_its_style_unless_ducking_takes_the_slot(style: float, ducking: float, stored: float) -> None:
    """Measured in both plugins: VST3 slot 5 is Style 0 to 4, or 5 for Ex Style's Ducking."""
    floats = list(DERIVED_DEFAULTS["pro-g"]["vst2_defaults"])
    floats[PRO_G_STYLE], floats[PRO_G_EX_STYLE] = style, ducking
    state = FabfState.parse(derived_vst3("pro-g", LegacyBank("x", tuple(floats))).state)
    assert state.parameters[PRO_G_STYLE_SLOT] == stored


@pytest.mark.parametrize("key", DERIVED_PRODUCTS)
def test_a_derived_product_stays_an_experiment_until_somebody_listens(key: str) -> None:
    """The rig proved the plugin reads it back; nothing has heard it.

    That is the whole point of the split between hosted and ear evidence.
    """
    plugin_map = CATALOG.find(Vst2Id(DERIVED_PRODUCTS[key][0]))
    assert plugin_map is not None and plugin_map.vst3 is not None
    record = plugin_map.vst3.measured
    assert record.rung is StateRung.RE_ENCODE
    assert StateEvidence.HOSTED in record.evidence
    assert not record.predictable


@pytest.mark.parametrize("spelling", ["FabFilter Pro-C", "FabFilter Pro-C.64"])
def test_both_spellings_of_pro_c_1_are_measured_and_still_want_a_listen(spelling: str) -> None:
    """The plugin read all 189 back; nobody has heard one."""
    record = measured_state(spelling)
    assert record.rung is StateRung.RE_ENCODE
    assert StateEvidence.HOSTED in record.evidence
    assert not record.predictable
