"""Analog Lab 3/4 presets into Analog Lab V: the instrument id, the part entries, what is refused, both formats."""

from __future__ import annotations

import struct

import pytest

from abletoolz.plugins.converters import analog_lab as conversion
from abletoolz.plugins.identity.ids import PluginKind
from abletoolz.plugins.state import analog_lab
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.measured import StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import FormatState

type Entries = list[tuple[str, str]]


def text(value: str) -> str:
    return f"{len(value.encode())} {value}"


def named(entries: Entries, *, with_class: bool) -> list[str]:
    if not entries:
        return ["0", "0"]
    return [str(len(entries)), "0", *(["0", "0"] if with_class else []), *(text(s) for entry in entries for s in entry)]


def record(name: str, instrument: int, version: str, *, outer: bool) -> list[str]:
    head = [text(name), text("Factory"), str(instrument), text("Someone"), text("Pad")]
    tags = ["1", "0", text("Bright"), "1", "0", text("A soft pad."), "1546617756", text(version)]
    # seven numbers and an empty string follow the version; the outer record has two more numbers
    return head + (["0", "0"] if outer else []) + tags + ["0"] * 7 + ["0 "] + (["0", "0"] if outer else [])


def archive(root: Entries = (), child: Entries = (), *, instrument: int = 1, version: str = "4.2.3.3897") -> bytes:
    """A synthetic preset archive: its record, the synth patch's record, then a few controls."""
    root, child = list(root), list(child)
    tokens = [
        *record("Reversed", instrument, version, outer=True),
        *named(root, with_class=True),
        *["0", "0", "2", "7"],
        *record("Reversed", 6, "3.6.1.3854", outer=False),
        *named(child, with_class=not root),
        *["0", "2", "0", "0", "0", text("_Slot1_CUT_OFF"), "0.25", text("Part1_Param1"), "0.5"],
    ]
    return conversion.HEADER + " ".join(tokens).encode()


def twice(first: bytes, second: bytes | None = None) -> bytes:
    second = first if second is None else second
    return struct.pack("<QQ", len(first), len(second)) + first + second


PART = [("InstrumentPart1", "6"), ("InstrumentPart1VariationIdx", "0")]
TAGS = [("Tags", "Reversed")]


def test_the_converter_joins_analog_lab_4_to_analog_lab_v_and_analog_lab_v_does_the_work() -> None:
    found = CATALOG.converter("arturia.analog-lab-4", "arturia.analog-lab-v")
    assert found is not None
    assert found.measured.rung is StateRung.VENDOR_COMPAT and found.measured.evidence == (StateEvidence.HOSTED,)
    source = CATALOG.by_map_id("arturia.analog-lab-4")
    assert CATALOG.find(analog_lab.ANALOG_LAB_4_VST2) is source is CATALOG.find(analog_lab.ANALOG_LAB_4_VST3)
    assert CATALOG.find(analog_lab.ANALOG_LAB_V_VST2) is CATALOG.by_map_id("arturia.analog-lab-v")


def test_only_the_instrument_id_changes_when_the_record_names_no_part() -> None:
    first, second = archive(TAGS, version="4.1.1.3554"), archive(TAGS, TAGS, version="4.1.1.3554")
    converted = conversion.convert(twice(first, second))
    assert converted.notices == (conversion.NOTE,)
    expected = [a.replace(b" 7 Factory 1 ", b" 7 Factory 60 ", 1) for a in (first, second)]
    assert converted.patch == twice(*expected)
    assert expected[0].count(b" 7 Factory 6 ") == 1  # the synth patch's own id is left as it is


def test_an_analog_lab_3_preset_converts_the_same_way() -> None:
    source = archive(version="3.2.3.2000")
    assert conversion.as_analog_lab_v(source) == source.replace(b" 7 Factory 1 ", b" 7 Factory 60 ", 1)


@pytest.mark.parametrize(
    ("root", "child", "kept"),
    [
        (PART + TAGS, TAGS, TAGS),
        ([("Characteristics", "Styles,Bright;"), *PART, ("Type", "Pad")], [("Type", "Pad")], None),
        (PART, [], []),
        (PART, TAGS, []),
    ],
)
def test_the_part_entries_are_left_out_and_every_other_entry_stays(
    root: Entries, child: Entries, kept: Entries | None
) -> None:
    kept = [entry for entry in root if entry not in PART] if kept is None else kept
    converted = conversion.as_analog_lab_v(archive(root, child))
    assert converted == archive(kept, child, instrument=60)
    assert b"InstrumentPart1" not in converted


def test_analog_lab_4_s_vst3_holds_the_archive_as_both_halves() -> None:
    source = CATALOG.by_map_id("arturia.analog-lab-4")
    assert source is not None
    state = archive(PART + TAGS, TAGS)
    patch = source.lift(FormatState(state, state), PluginKind.VST3)
    assert patch == twice(state)
    assert not source.can_write(PluginKind.VST3)


def test_analog_lab_v_is_written_as_a_vst2_only() -> None:
    target = CATALOG.by_map_id("arturia.analog-lab-v")
    assert target is not None
    patch = conversion.convert(twice(archive())).patch
    assert target.lower(patch, PluginKind.VST).state == patch
    assert not target.can_write(PluginKind.VST3)


@pytest.mark.parametrize(
    "patch",
    [
        twice(archive(instrument=60, version="5.11.2.6235")),
        twice(archive(version="5.11.2.6235")),
        twice(archive(version="")),
        twice(b"not an archive"),
        twice(archive())[:-1],
        b"",
    ],
)
def test_anything_but_an_analog_lab_3_or_4_preset_is_refused(patch: bytes) -> None:
    with pytest.raises(StateTransformError):
        conversion.convert(patch)


def test_a_record_cut_short_is_refused() -> None:
    whole = archive(PART + TAGS, TAGS)
    with pytest.raises(StateTransformError):
        conversion.as_analog_lab_v(whole[: whole.index(b"InstrumentPart1VariationIdx")])
