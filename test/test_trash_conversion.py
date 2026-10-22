from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from abletoolz.plugins.converters.izotope_trash import NoticeCode, convert_single_band_distortion
from abletoolz.plugins.state import StateTransformError, registered_custom_states
from abletoolz.plugins.state.izotope_trash import Bank
from abletoolz.plugins.state.izotope_trash2 import Trash2State
from abletoolz.plugins.state.model import FieldKey, StateValue

FIXTURES = Path(__file__).parent / "state_fixtures/trash"


def source() -> Bank:
    return Bank.parse((FIXTURES / "trash-conversion-01-source.state").read_bytes())


def test_packaged_default_is_the_independent_native_template() -> None:
    assert Trash2State.default().encode() == (FIXTURES / "trash2-default.state").read_bytes()


def test_explicit_converter_reproduces_five_complete_native_reloads() -> None:
    before = registered_custom_states()
    manifest = json.loads((FIXTURES / "trash-conversions.json").read_bytes())
    for row in manifest["examples"]:
        raw, native = (FIXTURES / row["source"]).read_bytes(), (FIXTURES / row["target"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == row["source_sha256"]
        assert hashlib.sha256(native).hexdigest() == row["target_sha256"]
        bank = Bank.parse(raw)
        converted = convert_single_band_distortion(bank, program=row["program"])
        assert converted.state.encode() == native
        assert converted.program == row["program"]
        assert converted.notices[0].code is NoticeCode.EXPERIMENTAL
        assert bank.encode() == raw
    assert registered_custom_states() == before


def test_bank_selection_is_required_and_never_implicitly_program_zero() -> None:
    bank = source()
    modified = bank.program(0).with_values({"Trash: First Overdrive": 8.75})
    programs = list(bank.programs)
    programs[3] = modified
    selected = convert_single_band_distortion(replace(bank, programs=tuple(programs)), program=3)
    assert selected.state.settings.value("TRASH: Stg1 Drive") == 8.75
    assert convert_single_band_distortion(bank, program=0).state != selected.state
    with pytest.raises(TypeError):
        convert_single_band_distortion(bank)  # type: ignore[call-arg]  # Exercise an untyped caller omitting selection.
    for index in (-1, 16, True):
        with pytest.raises(ValueError):
            convert_single_band_distortion(bank, program=index)


@pytest.mark.parametrize("module", ["PreFilter", "PostFilter", "BoxModel", "Squash", "Buzz"])
def test_active_unmapped_modules_are_refused_without_dropping_them(module: str) -> None:
    bank = source().with_values(0, {f"{module}: Bypass": False})
    raw = bank.encode()
    with pytest.raises(StateTransformError, match=f"Active {module}"):
        convert_single_band_distortion(bank, program=0)
    assert bank.encode() == raw


@pytest.mark.parametrize("mode", ["Multi-Band", "Multi-Rate", "Solo Mute"])
def test_unmapped_modes_are_refused(mode: str) -> None:
    with pytest.raises(StateTransformError, match=mode):
        convert_single_band_distortion(source().with_values(0, {f"Trash: {mode}": True}), program=0)


def test_unknown_correspondence_ranges_and_revision_stay_explicit() -> None:
    cases: tuple[tuple[dict[FieldKey, StateValue], str], ...] = (
        ({"Trash: First Algorithm": 36}, "No Trash 2 counterpart"),
        ({"Gain: Input Left": 11.0}, "target range"),
        ({"Bypass Gain": -3.0}, "bypass gain"),
    )
    for changes, reason in cases:
        with pytest.raises(StateTransformError, match=reason):
            convert_single_band_distortion(source().with_values(0, changes), program=0)
    mixed = Bank.parse((FIXTURES / "trash1-mixed-revisions.state").read_bytes())
    index = next(i for i, program in enumerate(mixed.programs) if program.revision == 22)
    with pytest.raises(StateTransformError, match="unmapped"):
        convert_single_band_distortion(mixed, program=index)


def test_known_tonal_differences_are_returned_alongside_an_usable_state() -> None:
    bank = Bank.parse((FIXTURES / "trash-conversion-05-source.state").read_bytes())
    converted = convert_single_band_distortion(bank, program=0)
    assert any(notice.code is NoticeCode.ALGORITHM_DIFFERENCE and notice.stage == 1 for notice in converted.notices)
    unlocked = source().with_values(
        0, {"Input Gain Slider Lock": False, "Gain: Input Left": -5.0, "Gain: Input Right": 2.0}
    )
    channels = convert_single_band_distortion(unlocked, program=0)
    assert channels.state.settings.value("GAIN: Input Lock") is False
    assert channels.state.settings.value("GAIN: Input R (unlocked)") == 2.0
    assert any(notice.code is NoticeCode.CHANNEL_GAINS_UNCALIBRATED for notice in channels.notices)
