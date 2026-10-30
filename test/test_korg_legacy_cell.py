"""Legacy Cell's bank: which synth sits in each slot of the working cell, its effects, and the MS-20 inside.

The banks built here follow the measured layout with made-up names and empty
effect and mixer bodies; their MS-20 programs are real sets' working programs from
``state_fixtures/korg_ms20/ms20_hosted.json``.
"""

from __future__ import annotations

import struct

import pytest
from test_korg_ms20 import CORPUS, HOSTED, program

from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.korg_legacy_cell import CELLS, LEGACY_CELL_VST2, LegacyCellBank
from abletoolz.plugins.state.korg_ms20 import PROGRAM_BYTES, Ms20Program

type Place = tuple[bytes, bytes] | None


def record(place: Place) -> bytes:
    if place is None:
        return struct.pack("<III4s", 16, 0, 0, bytes(4))
    tag, data = place
    return struct.pack("<III4s", 16, 1, len(data), tag) + data


def effect(name: str) -> Place:
    return b"VFLK", name.encode().ljust(0x128, b"\0")


def ms20(entry: int = 0) -> Place:
    return b"VMLK", program(HOSTED["programs"][entry])


POLYSIX: Place = (b"VPLK", b"prog".ljust(PROGRAM_BYTES, b"\0"))


def cell(name: str | bytes, a: Place, b: Place = None, effects: tuple[str, str] = ("No Effect", "No Effect")) -> bytes:
    saved = name if isinstance(name, bytes) else name.encode()
    header = struct.pack("<I", 0x40) + saved.ljust(28, b"\0") + bytes(0x40 - 32)
    places = [a, *([effect(e) for e in effects] if a else [None, None])]
    places += [b, *([effect("4 Band EQ"), effect("No Effect")] if b else [None, None])]
    places += [effect("Reverb Hall"), effect("No Effect")]
    places += [(b"1MLK", bytes(0x38)), (b"2MLK", bytes(0x14)), (b"3MLK", bytes(0x10)), (b"tlFM", bytes(0x1000))]
    return header + b"".join(record(place) for place in places)


def bank(working: bytes, *, selected: int = 3) -> bytes:
    head = struct.pack("<8I", 32, 0x201, CELLS + 1, 6, selected, 0, 0xFFFFFFFF, 0xFFFFFFFF)
    return head + cell("Factory", POLYSIX) * CELLS + working


def test_the_working_cell_names_its_synths_effects_and_reads_its_ms20() -> None:
    parsed = LegacyCellBank.parse(bank(cell("Bass Cell", ms20(2), POLYSIX, ("OD/HiGain + Wah", "No Effect"))))
    working = parsed.working
    assert (parsed.selected, working.name, working.synths) == (3, "Bass Cell", ("MS-20", "Polysix"))
    assert working.slots[0].effects == ("OD/HiGain + Wah", "No Effect")
    assert working.slots[1].effects == ("4 Band EQ", "No Effect") and working.master_effects == (
        "Reverb Hall",
        "No Effect",
    )
    assert working.slots[0].ms20 == Ms20Program.parse(program(HOSTED["programs"][2]))


@pytest.mark.parametrize(
    ("saved", "shown"),
    [
        ("ベース".encode("cp932"), "ベース"),
        ("音色".encode("cp932"), "音色"),
        ("Crème".encode("cp1252"), "Crème"),
        (b"Bass\x81", "Bass\x81"),
    ],
    ids=["kana", "kanji", "accent", "no code page"],
)
def test_a_cell_name_in_any_code_page_reads(saved: bytes, shown: str) -> None:
    """A byte Windows-1252 has no character for used to refuse the whole bank."""
    assert LegacyCellBank.parse(bank(cell(saved, ms20()))).working.name == shown


def test_an_ms20_edit_lands_in_the_working_cell_only() -> None:
    raw = bank(cell("Bass Cell", ms20()))
    parsed = LegacyCellBank.parse(raw)
    assert parsed.working.slots[0].ms20 is not None
    edited = parsed.with_ms20(0, parsed.working.slots[0].ms20.with_values({"LPF Cutoff": 2.5}))
    program_ = edited.working.slots[0].ms20
    assert program_ is not None and program_.values()["LPF Cutoff"] == pytest.approx(2.5)
    assert edited.raw[: len(raw) - len(cell("Bass Cell", ms20()))] == raw[: len(raw) - len(cell("Bass Cell", ms20()))]
    with pytest.raises(StateTransformError, match="no MS-20 in slot B"):
        parsed.with_ms20(1, program_)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (bank(cell("x", ms20()))[:-1], "ends inside|runs past"),
        (bank(cell("x", ms20())) + b"\0", "bytes after its cells"),
        (struct.pack("<I", 33) + bank(cell("x", ms20()))[4:], "header"),
        (bank(cell("x", (b"ABCD", bytes(8))), selected=3), "synth place"),
        (bank(cell("x", ms20()), selected=200), "selects cell"),
    ],
    ids=["cut short", "trailing byte", "header", "unknown synth", "selected cell"],
)
def test_a_bank_off_the_measured_layout_is_refused(raw: bytes, message: str) -> None:
    with pytest.raises(StateTransformError, match=message):
        LegacyCellBank.parse(raw)


def test_a_legacy_cell_device_and_its_jbridge_wrapper_find_the_map_by_uniqueid() -> None:
    found = CATALOG.find(LEGACY_CELL_VST2)
    assert found is not None and found.id == "korg.legacy-cell"


@pytest.mark.skipif(
    not CORPUS.is_dir(), reason="the Legacy Cell banks from real sets live only on the machine that holds them"
)
def test_every_cell_bank_from_real_sets_reads_to_its_last_byte() -> None:
    banks = [path for path in sorted(CORPUS.glob("*.state")) if path.stat().st_size > 1_000_000]
    assert banks
    synths = {LegacyCellBank.parse(path.read_bytes()).working.synths for path in banks}
    assert ("MS-20",) in synths and ("Polysix",) in synths
