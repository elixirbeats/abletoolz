"""Serum's chunk, and its patch rebuilt as Serum 2's.

The fixture is Serum 1.3's own init patch, read headless off the installed VST3
(nothing but its factory names in it); test patches are that init with the
measured fields edited, so no one's sound goes into the tests.
"""

from __future__ import annotations

import pathlib
import struct
import zlib
from typing import Any

import cbor2
import pytest

from abletoolz.plugins.converters import serum as conversion
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.xfer import SerumChunk, XferJson

INIT = (pathlib.Path(__file__).parent / "state_fixtures" / "serum" / "serum-init.chunk").read_bytes()
A_VOL = 1  # Serum's host index for oscillator A's level, which a mod destination names


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
    ],
    ids=["length cut", "stream cut", "not zlib", "earliest layout"],
)
def test_a_chunk_off_the_measured_layouts_is_refused(chunk: bytes, message: str) -> None:
    with pytest.raises(StateTransformError, match=message):
        SerumChunk.parse(chunk)


# -- the conversion ------------------------------------------------------------------


def test_the_init_patch_becomes_a_serum_2_patch_marked_as_imported() -> None:
    doc, notices = converted(*patch_of(INIT))
    assert doc["Global0"]["plainParams"]["kParamS1Compatibility"] == 1.0
    assert notices[0] == conversion.UNFINISHED_NOTE
    # The init embeds its tables rather than naming them, which is said rather than carried.
    assert any("oscillator A's wavetable, embedded" in note for note in notices)


def test_a_control_lands_where_serum_2_displays_the_same() -> None:
    patch, tables = patch_of(INIT)
    semi = control("A Semi")
    # The measured position nearest Serum's +5 semitones; both plugins store the knob continuously.
    table = semi["table"]
    index = min(range(len(table)), key=lambda k: abs((table[k] if table[k] is not None else 0.0) - 5.0))
    struct.pack_into("<f", patch, semi["offset"], index / (len(table) - 1))
    doc, _ = converted(patch, tables)
    assert doc["Oscillator0"]["plainParams"]["kParamPitch"] == pytest.approx(table[index], abs=1e-4)
    assert round(doc["Oscillator0"]["plainParams"]["kParamPitch"]) == 5


def test_a_mod_routing_keeps_its_source_and_names_its_destination() -> None:
    patch, tables = patch_of(INIT)
    struct.pack_into("<IHH", patch, 20, 1, 0, A_VOL)  # slot 1: the mod wheel to oscillator A's level
    doc, _ = converted(patch, tables)
    slot = doc["ModSlot0"]
    assert slot["source"] == [1, 0]
    assert (slot["destModuleTypeString"], slot["destModuleID"], slot["destModuleParamName"]) == (
        "Oscillator",
        0,
        "kParamVolume",
    )


def test_wavetables_and_noise_cross_by_their_library_path() -> None:
    patch, tables = patch_of(INIT)
    layout = conversion._data()["wavetables"]
    patch[layout["a"] : layout["a"] + 40] = b"\\Analog\\Basic Shapes.wav".ljust(40, b"\0")
    patch[layout["noise"] : layout["noise"] + 40] = b"Organics\\Air Can.wav".ljust(40, b"\0")
    doc, _ = converted(patch, tables)
    assert doc["Oscillator0"]["WTOsc0"]["relativePathToWT"] == "/Analog/Basic Shapes.wav"
    assert "numFrames" not in doc["Oscillator0"]["WTOsc0"]
    assert doc["Oscillator3"]["NoiseOsc3"]["relativePathToNoiseSample"] == "Organics/Air Can.wav"


def fx_module(name: str) -> tuple[int, dict[str, Any]]:
    modules = conversion._data()["fx"]["modules"]
    return next((k, m) for k, m in enumerate(modules) if m["serum1"] == name)


def switch_on(patch: bytearray, *names: str) -> None:
    for name in names:
        struct.pack_into("<f", patch, fx_module(name)[1]["enable"], 1.0)


def test_the_fx_switched_on_cross_in_serums_order() -> None:
    patch, tables = patch_of(INIT)
    switch_on(patch, "Dist", "Fil")
    order = list(range(10))
    dist, fil = fx_module("Dist")[0], fx_module("Fil")[0]
    order[dist], order[fil] = order[fil], order[dist]  # Serum holds each module's position
    struct.pack_into("<10I", patch, conversion._data()["fx"]["order"], *order)
    doc, _ = converted(patch, tables)
    assert [fx["type"] for fx in doc["FXRack0"]["FX"]] == [8, 0]  # the filter, then the distortion


def test_a_mod_routing_into_an_fx_names_its_place_in_the_rack() -> None:
    patch, tables = patch_of(INIT)
    switch_on(patch, "Dist", "Fil")
    fx = conversion._data()["fx"]
    fil = fx_module("Fil")[0]
    destination = next(
        int(k) for k, t in fx["destinations"].items() if t["module"] == fil and t["param"] == "kParamFreq"
    )
    struct.pack_into("<IHH", patch, 20, 1, 0, destination)
    doc, _ = converted(patch, tables)
    slot = doc["ModSlot0"]
    assert (slot["destModuleTypeString"], slot["destModuleID"], slot["destModuleParamName"]) == (
        "FXFilter",
        1,
        "kParamFreq",
    )


def test_what_is_not_carried_yet_is_named() -> None:
    patch, tables = patch_of(INIT)
    switch_on(patch, "Dly")
    delay_time = fx_module("Dly")[1]["not_carried"][0]
    struct.pack_into("<f", patch, delay_time["offset"], delay_time["init"] + 0.25)
    struct.pack_into("<IHH", patch, 20, 1, 0, 9999)  # a destination no Serum 2 control was joined to
    _, notices = converted(patch, tables)
    (not_carried,) = [note for note in notices if note.startswith("Not carried yet")]
    assert "the delay's times" in not_carried
    assert "1 mod routing(s) into a control not carried" in not_carried


def test_called_strictly_what_is_not_carried_is_refused() -> None:
    with pytest.raises(StateTransformError, match="Not carried yet"):
        conversion.convert(chunk_of(*patch_of(INIT)))


def test_a_plan_reaches_serum_2_through_the_converter() -> None:
    found = CATALOG.converter("xfer.serum", "xfer.serum-2")
    assert found is not None and found.measured.annotation == "state: re-encode (hosted 2026-10-26)"


def test_free_running_lfos_cross_in_hz_and_seconds() -> None:
    patch, tables = patch_of(INIT)
    patch[conversion._LFOS_FREE] = 1
    struct.pack_into("<f", patch, control("LFO3 Rate")["offset"], 0.5)
    struct.pack_into("<f", patch, control("LFO3 Delay")["offset"], 0.25)
    doc, _ = converted(patch, tables)
    lfo = doc["LFO2"]["plainParams"]
    # Serum shows 6.2 Hz and 1.0 s there.
    assert (lfo["kParamBeatSync"], lfo["kParamRate"], lfo["kParamDelay"]) == (0.0, pytest.approx(6.25), 1.0)
