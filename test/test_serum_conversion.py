"""Serum's chunk, and its patch rebuilt as Serum 2's own Serum 1 importer writes it.

The fixtures are Serum 1.3's own init patch, read headless off the installed VST3
(nothing but its factory names in it), and two Serum 1 factory presets with
Serum 2's own import of each. Test patches are the init with the measured
fields edited, so no one's sound goes into the tests.
"""

from __future__ import annotations

import pathlib
import re
import struct
import zlib
from typing import Any

import cbor2
import pytest

from abletoolz.plugins.converters import serum as conversion
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.xfer import SerumChunk, XferJson

FIXTURES = pathlib.Path(__file__).parent / "state_fixtures"
INIT = (FIXTURES / "serum" / "serum-init.chunk").read_bytes()
A_VOL = 1  # Serum's destination id for oscillator A's level


def patch_of(chunk: bytes) -> tuple[bytearray, bytes]:
    parsed = SerumChunk.parse(chunk)
    return bytearray(parsed.patch), parsed.tables


def chunk_of(patch: bytes | bytearray, tables: bytes = b"") -> bytes:
    first = zlib.compress(patch)
    return first + (zlib.compress(tables) if tables else b"") + struct.pack("<I", len(first))


def control(name: str) -> dict[str, Any]:
    return next(c for c in conversion._data()["controls"] if c["serum1"] == name)


def converted(patch: bytes | bytearray, tables: bytes = b"") -> tuple[dict[str, Any], tuple[str, ...]]:
    result = conversion.convert(chunk_of(patch, tables), lossy=True)
    return cbor2.loads(XferJson.parse(result.patch).decoded()), result.notices


def put(patch: bytearray, offset: int, value: float) -> None:
    struct.pack_into("<f", patch, offset, value)


# -- the chunk -----------------------------------------------------------------------


def test_the_init_chunk_holds_its_patch_and_one_embedded_frame_per_oscillator() -> None:
    parsed = SerumChunk.parse(INIT)
    assert len(parsed.patch) == 172736
    assert len(parsed.tables) == 2 * 2048 * 4


@pytest.mark.parametrize(
    ("chunk", "message"),
    [
        (INIT[:-1], "length"),
        (INIT[:100], "ends inside"),
        (b"not zlib at all", "zlib"),
        (chunk_of(bytes(20704)), "20704 byte Serum patch"),
        (chunk_of(bytes(1 << 20)), "inflates past any Serum patch"),
        (chunk_of(bytes(172736), bytes(16 * 1024 * 1024 + 1)), "inflates past any two tables"),
    ],
    ids=["length cut", "stream cut", "not zlib", "earliest layout", "patch too large", "tables too large"],
)
def test_a_chunk_off_the_measured_layouts_is_refused(chunk: bytes, message: str) -> None:
    with pytest.raises(StateTransformError, match=message):
        SerumChunk.parse(chunk)


def test_a_patch_version_not_measured_is_refused() -> None:
    patch, tables = patch_of(INIT)
    put(patch, conversion._data()["versions"]["offset"], 0.1700)
    with pytest.raises(StateTransformError, match="patch version 0.1700"):
        conversion.convert(chunk_of(patch, tables), lossy=True)


@pytest.mark.parametrize("cut", [8192, 8000], ids=["one oscillator's frames", "inside a frame"])
def test_embedded_frames_the_tables_cannot_hold_are_refused(cut: int) -> None:
    patch, tables = patch_of(INIT)
    with pytest.raises(StateTransformError, match="embedded frames"):
        conversion.convert(chunk_of(patch, tables[:cut]), lossy=True)


@pytest.mark.parametrize("size", [33872, 28232])
def test_a_short_patch_claiming_a_version_with_lfo_blocks_is_refused(size: int) -> None:
    patch, tables = patch_of(INIT)
    with pytest.raises(StateTransformError, match="too short for patch version 0.1631"):
        conversion.convert(chunk_of(patch[:size], tables), lossy=True)


# -- against Serum 2's own import --------------------------------------------------------

# What the importer writes that a chunk cannot tell: each library file's own frame count and rate, and the tags its
# preset browser files the patch under.
_FROM_THE_LIBRARY = re.compile(r"/(numFrames|numChannels|sampleRate|detuneFactor)$|^/tags")


def _flat(doc: Any, path: str = "") -> dict[str, Any]:
    if isinstance(doc, dict):
        return {k: v for key, value in doc.items() for k, v in _flat(value, f"{path}/{key}").items()}
    if isinstance(doc, list):
        return {k: v for i, value in enumerate(doc) for k, v in _flat(value, f"{path}[{i}]").items()}
    return {path: doc}


@pytest.mark.parametrize("preset", ["serum1-fx-uplifter", "serum1-nothing-special"])
def test_a_factory_preset_becomes_what_serum_2s_own_importer_writes(preset: str) -> None:
    chunk = (FIXTURES / "serum2" / f"{preset}.chunk").read_bytes()
    theirs = _flat(
        cbor2.loads(XferJson.parse((FIXTURES / "serum2" / f"{preset}.imported.state").read_bytes()).decoded())
    )
    ours = _flat(cbor2.loads(XferJson.parse(conversion.convert(chunk, lossy=True).patch).decoded()))
    for key in sorted(set(theirs) | set(ours)):
        if "FXHyperD/lfo" in key or _FROM_THE_LIBRARY.search(key):
            continue  # the Hyper/Dimension's LFO phases are its running state
        a, b = theirs.get(key), ours.get(key)
        if isinstance(a, float) and isinstance(b, (int, float)):
            assert b == pytest.approx(a, rel=5e-3, abs=5e-3), key
        else:
            assert b == a, key


# -- the conversion ------------------------------------------------------------------


def test_the_init_patch_becomes_serum_2s_import_of_it() -> None:
    doc, notices = converted(*patch_of(INIT))
    assert doc["Global0"]["plainParams"]["kParamS1Compatibility"] == 1.0
    assert notices[0] == conversion.UNFINISHED_NOTE
    # The init embeds one frame per oscillator, which crosses as the importer writes it.
    assert len(doc["Oscillator0"]["WTOsc0"]["embeddedWTData"]) == 2048
    assert not any(note.startswith("Not carried yet") for note in notices)


def test_a_control_lands_where_serum_2s_importer_puts_it() -> None:
    patch, tables = patch_of(INIT)
    put(patch, control("A Semi")["offset"], 17 / 24)  # Serum's +5 semitones
    doc, _ = converted(patch, tables)
    assert doc["Oscillator0"]["plainParams"]["kParamPitch"] == 5.0


def test_main_volume_follows_the_importers_law() -> None:
    # Serum's main volume is a cubic gain of its knob; the importer stores 0.8541468 x^3, not the same percentage.
    patch, tables = patch_of(INIT)
    put(patch, control("MasterVol")["offset"], 0.7)
    doc, _ = converted(patch, tables)
    assert doc["Global0"]["plainParams"]["kParamMasterVolume"] == pytest.approx(0.8541468 * 0.7**3, rel=1e-4)


def test_a_mod_envelopes_sustain_crosses_as_its_knob() -> None:
    patch, tables = patch_of(INIT)
    put(patch, control("Env2 Sus")["offset"], 0.2456)
    doc, _ = converted(patch, tables)
    assert doc["Env1"]["plainParams"]["kParamSustain"] == pytest.approx(0.2456, abs=1e-4)


def test_oscillator_a_switched_off_stays_off() -> None:
    patch, tables = patch_of(INIT)
    put(patch, control("Osc A On")["offset"], 0.0)
    doc, _ = converted(patch, tables)
    assert doc["Oscillator0"]["plainParams"]["kParamEnable"] == 0.0


def routing(doc: dict[str, Any], slot: int) -> str | None:
    value: str | None = doc.get(f"RoutingSlot{slot}", {}).get("plainParams", {}).get("kParamRoutingDest")
    return value


def test_each_source_goes_where_serums_filter_and_direct_switches_send_it() -> None:
    rules = {rule["slot"]: rule for rule in conversion._data()["routing"]}
    settings = conversion._data()["versions"]["layouts"]["0.1631"]["settings"]
    patch, tables = patch_of(INIT)
    put(patch, rules[0]["filter"], 0.0)  # A past the filter
    put(patch, rules[1]["filter"], 1.0)  # B through it
    put(patch, rules[4]["filter"], 1.0)
    put(patch, settings + rules[4]["direct"], 1.0)  # the sub straight out, whatever its filter switch says
    doc, _ = converted(patch, tables)
    assert [routing(doc, slot) for slot in (0, 1, 3, 4)] == [
        "kRoutingDestMaster",
        "kRoutingDestFilter",
        None,
        "kRoutingDestDirect",
    ]


@pytest.mark.parametrize(("version", "start"), [(0.1631, 19524), (0.1621, 19520), (0.1561, 19352)])
def test_the_settings_block_is_read_where_the_patchs_version_keeps_it(version: float, start: int) -> None:
    patch, tables = patch_of(INIT)
    put(patch, conversion._data()["versions"]["offset"], version)
    put(patch, 19544, 0.0)  # Serum's init has mono off where 0.1631 keeps it
    put(patch, start + 20, 1.0)  # mono
    put(patch, start + 24, 1.0)  # legato
    doc, _ = converted(patch, tables)
    voicing = doc["Global0"]["plainParams"]
    assert (voicing["kParamMonoToggle"], voicing["kParamLegato"], voicing["kParamVelocityLegato"]) == (1.0, 1.0, 0.0)


def test_warp_modes_cross_as_the_importer_names_them() -> None:
    patch, tables = patch_of(INIT)
    put(patch, control("WarpOscA")["offset"], 17 / 22)  # Serum's FM (from B)
    put(patch, control("WarpOscB")["offset"], 3 / 22)  # a sync window
    doc, _ = converted(patch, tables)
    assert doc["Oscillator0"]["WTOsc0"]["plainParams"]["kParamWarpMenu"] == "kPD_OSC"
    b = doc["Oscillator1"]["WTOsc1"]["plainParams"]
    assert (b["kParamWarpMenu"], b["kParamWarpVar"]) == ("kSync", 1.0)


def slot_record(patch: bytearray, slot: int, source: int, destination: int, amount: float, out: float = 1.0) -> int:
    record = conversion._slot_offset(slot)
    struct.pack_into("<2f", patch, record + conversion._SLOT_AMOUNT, amount, out)
    struct.pack_into("<IHH", patch, record + conversion._SLOT_SOURCE, source, 0, destination)
    return record


def test_a_mod_slot_crosses_from_its_own_record() -> None:
    patch, tables = patch_of(INIT)
    record = slot_record(patch, 0, 1, A_VOL, -0.5, 0.25)  # the mod wheel to oscillator A's level
    patch[record + conversion._SLOT_BIPOLAR] = 1
    patch[record + conversion._SLOT_SWITCHES] = 2  # bypassed
    patch[record + conversion._SLOT_CURVES] = 0  # the curve fully one way
    doc, _ = converted(patch, tables)
    slot = doc["ModSlot0"]
    assert slot["source"] == [conversion._data()["sources"]["1"], 0]
    assert (slot["destModuleTypeString"], slot["destModuleID"], slot["destModuleParamName"]) == (
        "Oscillator",
        0,
        "kParamVolume",
    )
    assert slot["plainParams"] == {
        "kParamAmount": -50.0,
        "kParamOut": 25.0,
        "kParamBipolar": 1.0,
        "kParamBypass": 1.0,
        "kParamCurveIn": -100.0,
    }


@pytest.mark.parametrize(("byte", "curve"), [(0, -100.0), (64, -50.0), (128, None), (255, 99.6078431)])
def test_a_mod_curve_byte_crosses_as_the_importer_scales_it(byte: int, curve: float | None) -> None:
    assert conversion._curve(byte) == (curve if curve is None else pytest.approx(curve))


def fx_module(name: str) -> tuple[int, dict[str, Any]]:
    modules = conversion._data()["fx"]["modules"]
    return next((k, m) for k, m in enumerate(modules) if m["serum1"] == name)


def load_fx(patch: bytearray, *names: str) -> None:
    for name in names:
        module = fx_module(name)[1]
        patch[module["present"]] = 1
        put(patch, module["enable"], 1.0)


def test_the_fx_loaded_cross_in_serums_order() -> None:
    patch, tables = patch_of(INIT)
    load_fx(patch, "Dist", "Fil")
    order = list(range(10))
    dist, fil = fx_module("Dist")[0], fx_module("Fil")[0]
    order[dist], order[fil] = order[fil], order[dist]  # Serum holds each module's position
    struct.pack_into("<10I", patch, conversion._data()["fx"]["order"], *order)
    doc, _ = converted(patch, tables)
    assert [fx["type"] for fx in doc["FXRack0"]["FX"]] == [8, 0]  # the filter, then the distortion


@pytest.mark.parametrize(
    ("order", "places"),
    [
        ([0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]),
        # Two modules at one place: the earlier one takes the place nobody holds.
        ([0, 0, 1, 2, 3, 4, 9, 6, 7, 8], [5, 0, 1, 2, 3, 4, 9, 6, 7, 8]),
        # A position past the rack, likewise.
        ([2466971648, 1, 9, 2, 5, 6, 4, 8, 7, 3], [0, 1, 9, 2, 5, 6, 4, 8, 7, 3]),
    ],
    ids=["in order", "a shared place", "past the rack"],
)
def test_fx_positions_are_repaired_as_the_importer_repairs_them(order: list[int], places: list[int]) -> None:
    assert conversion._positions(order) == places


def test_an_fx_module_serum_switched_off_crosses_switched_off() -> None:
    patch, tables = patch_of(INIT)
    load_fx(patch, "Dist")
    patch[fx_module("Dist")[1]["off"]] = 1
    doc, _ = converted(patch, tables)
    (dist,) = doc["FXRack0"]["FX"]
    assert dist["FXDistortion"]["plainParams"]["kParamEnable"] == 0.0


def test_a_mod_routing_into_an_fx_names_its_place_and_scales_its_output() -> None:
    patch, tables = patch_of(INIT)
    load_fx(patch, "Dist", "Fil")
    fx = conversion._data()["fx"]
    fil = fx_module("Fil")[0]
    destination = next(
        int(k) for k, t in fx["destinations"].items() if t["module"] == fil and t["param"] == "kParamFreq"
    )
    slot_record(patch, 0, 1, destination, 0.5)
    doc, _ = converted(patch, tables)
    slot = doc["ModSlot0"]
    assert (slot["destModuleTypeString"], slot["destModuleID"], slot["destModuleParamName"]) == (
        "FXFilter",
        1,
        "kParamFreq",
    )
    # The two filters' frequency ranges differ: the importer scales the slot's output by 160/171.
    assert slot["plainParams"]["kParamOut"] == pytest.approx(100 * 160 / 171)


def test_a_mod_routing_into_an_fx_not_loaded_still_loads_it() -> None:
    patch, tables = patch_of(INIT)
    fil = fx_module("Fil")[0]
    destination = next(int(k) for k, t in conversion._data()["fx"]["destinations"].items() if t["module"] == fil)
    slot_record(patch, 0, 1, destination, 0.5)
    doc, _ = converted(patch, tables)
    (module,) = doc["FXRack0"]["FX"]
    assert module["type"] == 8 and module["FXFilter"]["plainParams"]["kParamEnable"] == 0.0


def test_the_reverb_in_plate_mode_takes_its_decay_as_pre_delay() -> None:
    patch, tables = patch_of(INIT)
    load_fx(patch, "Rev")
    rev = fx_module("Rev")[1]
    put(patch, conversion._data()["versions"]["layouts"]["0.1631"]["settings"] + rev["mode"]["settings_at"], 0.0)
    doc, _ = converted(patch, tables)
    (module,) = doc["FXRack0"]["FX"]
    params = module["FXReverb"]["plainParams"]
    assert "kParamType" not in params and params["kParamPreDelay"] > 0


def lfo_block(k: int) -> int:
    blocks = conversion._data()["lfo_switches"]["blocks"]
    return int(blocks["first"]) + k * int(blocks["stride"])


def test_a_free_running_lfo_crosses_in_hz_and_seconds() -> None:
    blocks = conversion._data()["lfo_switches"]["blocks"]
    patch, tables = patch_of(INIT)
    patch[lfo_block(2) + blocks["free"]] = 1
    put(patch, control("LFO3 Rate")["offset"], 0.5)
    put(patch, control("LFO3 Delay")["offset"], 0.25)
    doc, _ = converted(patch, tables)
    lfo = doc["LFO2"]["plainParams"]
    # Serum shows 6.2 Hz and 1.0 s there.
    assert (lfo["kParamBeatSync"], lfo["kParamRate"], lfo["kParamDelay"]) == (0.0, pytest.approx(6.25), 1.0)
    assert "kParamBeatSync" not in doc["LFO0"]["plainParams"]  # each LFO keeps its own switch


def test_an_lfos_mode_and_shape_cross_from_its_block() -> None:
    blocks = conversion._data()["lfo_switches"]["blocks"]
    patch, tables = patch_of(INIT)
    at = lfo_block(0)
    patch[at + blocks["trig"]] = patch[at + blocks["env"]] = 1
    struct.pack_into("<i", patch, at + blocks["points"], 3)
    struct.pack_into("<4d", patch, at + blocks["x"], 0.0, 0.25, 1.0, 1.0)
    struct.pack_into("<4d", patch, at + blocks["y"], 1.0, 0.0, 0.5, 1.0)
    doc, _ = converted(patch, tables)
    lfo = doc["LFO0"]
    assert lfo["plainParams"]["kParamMode"] == "Envelope"
    assert (lfo["curveData"]["numPoints"], lfo["curveData"]["xVals"], lfo["curveData"]["yVals"]) == (
        3,
        [0.0, 0.25, 1.0, 1.0],
        [1.0, 0.0, 0.5, 1.0],
    )


def test_wavetables_and_noise_cross_by_their_library_path() -> None:
    patch, tables = patch_of(INIT)
    layout = conversion._data()["wavetables"]
    patch[layout["a"] : layout["a"] + 40] = b"\\Analog\\Basic Shapes.wav".ljust(40, b"\0")
    patch[layout["noise"] : layout["noise"] + 40] = b"Organics\\Air Can.wav".ljust(40, b"\0")
    struct.pack_into("<2i", patch, conversion._EMBEDDED_SIZES, 0, 2048 * 4)  # only B's frame embedded now
    doc, _ = converted(patch, tables[2048 * 4 :])
    a = doc["Oscillator0"]["WTOsc0"]
    assert a["relativePathToWT"] == "/Analog/Basic Shapes.wav" and "embeddedWTData" not in a
    assert len(doc["Oscillator1"]["WTOsc1"]["embeddedWTData"]) == 2048
    assert doc["Oscillator3"]["NoiseOsc3"]["relativePathToNoiseSample"] == "Organics/Air Can.wav"


def test_what_is_not_carried_yet_is_named() -> None:
    patch, tables = patch_of(INIT)
    patch[conversion._data()["tuning"]] = 1
    put(patch, control("A Octave")["offset"], 0.55)  # between two of Serum's octave steps
    slot_record(patch, 0, 1, 9999, 0.5)  # a destination no Serum 2 control was joined to
    _, notices = converted(patch, tables)
    (not_carried,) = [note for note in notices if note.startswith("Not carried yet")]
    assert "the custom tuning" in not_carried
    assert "octave or semitone set between steps" in not_carried
    assert "1 mod routing(s) into a control not carried" in not_carried


def test_called_strictly_what_is_not_carried_is_refused() -> None:
    patch, tables = patch_of(INIT)
    patch[conversion._data()["tuning"]] = 1
    with pytest.raises(StateTransformError, match="Not carried yet"):
        conversion.convert(chunk_of(patch, tables))


def test_a_plan_reaches_serum_2_through_the_converter() -> None:
    found = CATALOG.converter("xfer.serum", "xfer.serum-2")
    assert found is not None and found.measured.annotation == "state: re-encode (hosted 2026-10-29)"
