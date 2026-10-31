"""Timeless 2 into Timeless 3, as FabFilter's own importer writes Timeless 2's presets.

The factory presets the importer turned into Timeless 3 states, each fed in as the knob positions a Live set holds,
come out as the importer wrote them on all but a few stored floats: the stereo width of some presets that modulate
their pans, and the last digit of the feedback curves. Converted real patches load in Timeless 3, which keeps every
float.
"""

from __future__ import annotations

import pytest

from abletoolz.plugins.converters import fabfilter as conversion

TABLE = conversion._timeless_table()
NAMES: list[str] = TABLE["source_controls"]
TARGETS: list[str] = TABLE["target_controls"]
SOURCES: list[str] = TABLE["sources"]
OLD_TARGETS: list[str] = TABLE["targets"]
NEW_SOURCES: list[str] = TABLE["new_sources"]
NEW_TARGETS: list[str] = TABLE["new_targets"]


def bank(**units: float) -> list[float]:
    """A bank holding each named control at ``units``, in Timeless 2's own units; everything else at the bottom."""
    values = [0.0] * len(NAMES)
    for name, value in units.items():
        name = name.replace("_", " ")
        low, high, _ = TABLE["ranges"][name]
        values[NAMES.index(name)] = (value - low) / (high - low)
    return values


def converted(notices: list[str] | None = None, **units: float) -> dict[str, float]:
    parameters = conversion.timeless2_to_timeless3_parameters(bank(**units), [] if notices is None else notices)
    return dict(zip(TARGETS, parameters, strict=True))


def slot(n: int, source: str, target: str, depth: float) -> dict[str, float]:
    return {
        f"Slot_{n}_Source": SOURCES.index(source),
        f"Slot_{n}_Target": OLD_TARGETS.index(target),
        f"Slot_{n}_Level": depth,
    }


def test_two_synced_delays_become_the_shorter_one_with_a_time_pan() -> None:
    """A quarter note (sync 4) and a half (sync 3): the quarter is the base, the other side one octave of time away.

    Which side gets the longer one is the importer's: delay 1 a quarter writes a time pan of -1, as its presets show.
    """
    out = converted(Delay_1_Sync=4, Delay_2_Sync=3, Delay_1_Time=0.3)
    assert (out["Delay Sync"], out["Delay Time Pan"], out["Delay Time"]) == (4.0, -1.0, pytest.approx(0.3))


def test_two_free_delays_become_the_shorter_one_and_their_ratio() -> None:
    out = converted(Delay_1_Time=0.5, Delay_2_Time=0.3)
    assert out["Delay Time"] == pytest.approx(0.3)
    log2_ms = TABLE["delay_log2_ms"]
    expected = conversion._printed(log2_ms, 0.3) - conversion._printed(log2_ms, 0.5)
    assert out["Delay Time Pan"] == pytest.approx(expected)


@pytest.mark.parametrize("changes", [{"Delay_Link": 1}, {"Delay_1_Sync": 2}])
def test_linked_delays_or_one_synced_and_one_free_keep_delay_1(changes: dict[str, float]) -> None:
    out = converted(Delay_1_Time=0.4, Delay_2_Time=0.2, Delay_1_Offset=0.25, **changes)
    assert (out["Delay Time"], out["Delay Offset"], out["Delay Time Pan"]) == (pytest.approx(0.4), 0.25, 0.0)


def test_feedback_is_their_own_capped_at_100_percent_plus_their_cross_feedback() -> None:
    out = converted(Delay_1_Feedback=1, Delay_2_Feedback=1, Delay_1_Cross_Feedback=1, Delay_2_Cross_Feedback=1)
    assert out["Feedback"] == pytest.approx(2.0)
    assert out["Feedback Cross Mix"] == pytest.approx(0.5)


def test_the_dry_level_is_the_mix_and_the_input_gain_moves_to_the_wet_level() -> None:
    out = converted(Dry_Level=1, Dry_Enabled=1, Wet_Level=0.5, Input_Level=9)
    assert (out["Mix"], out["Input Level"], out["Output Level"]) == (pytest.approx(0.0), 9.0, -9.0)
    level_db = TABLE["level_db"]
    assert conversion._printed(level_db, out["Wet Level"]) == pytest.approx(conversion._printed(level_db, 0.5) + 9)


def test_a_slot_on_delay_2_is_dropped_and_its_xlfo_with_it() -> None:
    """XLFO 1 only drove delay 2's time, which Timeless 3 has no home for, so XLFO 2 becomes XLFO 1."""
    out = converted(
        Num_Active_Slots=2,
        Num_Active_XLFOs=2,
        XLFO_2_Balance=0.75,
        **slot(1, "XLFO 1", "Delay 2 Time", 0.5),
        **slot(2, "XLFO 2", "Filter 1 Freq", 0.25),
    )
    assert (out["Num Active Slots"], out["Num Active XLFOs"], out["XLFO 1 Balance"]) == (1.0, 1.0, 0.75)
    assert NEW_SOURCES[int(out["Slot 1 Source"])] == "XLFO 1"
    assert (NEW_TARGETS[int(out["Slot 1 Target"])], out["Slot 1 Level"]) == ("Filter 1 Freq", 0.25)


def test_a_slot_on_another_slot_s_depth_follows_it_to_where_it_is_sorted() -> None:
    out = converted(
        Num_Active_Slots=2,
        **slot(1, "MIDI 1", "Filter 1 Freq", 0.5),
        **slot(2, "XY 1 Hor", "Slot 1 Level", 0.5),
    )
    assert [NEW_SOURCES[int(out[f"Slot {n} Source"])] for n in (1, 2)] == ["XY 1 Hor", "MIDI 1"]
    assert NEW_TARGETS[int(out["Slot 1 Target"])] == "Slot 2 Level"


def test_a_depth_on_the_input_level_is_rescaled_as_the_importer_does_keeping_the_level() -> None:
    """What FabFilter's importer wrote for a synthetic preset: an envelope follower at 0.3 on an input level of 0 dB."""
    out = converted(Num_Active_Slots=1, Num_Active_EFs=1, Input_Level=0, **slot(1, "EF 1", "Input Level", 0.3))
    assert (out["Input Level"], out["Slot 1 Level"]) == (0.0, pytest.approx(0.5323, abs=1e-3))


def test_an_inverted_swing_through_silence_lowers_the_input_level_as_the_importer_does_and_is_said() -> None:
    notices: list[str] = []
    changes = slot(1, "EG 1", "Input Level", 0.2) | {"Slot_1_Inverted": 1}
    out = converted(notices, Num_Active_Slots=1, Num_Active_EGs=1, Input_Level=0, **changes)
    assert (out["Input Level"], out["Slot 1 Level"]) == (pytest.approx(-12.588, abs=0.02), 0.0)
    assert "through silence" in notices[0]
