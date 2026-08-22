"""xfadelooper parser: registry dispatch, fixed-width framing, DB-driven relink.

Every buffer here is synthesized from the measured layout. Nothing in this file
comes out of a real set.
"""

from __future__ import annotations

import pathlib
import struct
from xml.etree import ElementTree as ET

from abletoolz.decode_encode import string_to_xml
from abletoolz.plugin_parsers.base import PluginData
from abletoolz.plugin_parsers.registry import fix_plugin, get_parser_for_plugin
from abletoolz.plugin_parsers.state.xfadelooper import (
    LAYOUT_VERSION,
    MAGIC,
    PATH_END,
    PATH_FIELD_SIZE,
    PATH_OFFSET,
    XfadeLooperParser,
)

XFADE_UNIQUE_ID = 1163098214
NAME_FIELD_SIZE = 32
PARAM_COUNT = 88
STATE_SIZE = PATH_END + PARAM_COUNT * 4

# Stand-in parameter block: distinct values so a stray write shows up.
PARAMS = tuple(float(i) / 4.0 for i in range(PARAM_COUNT))


def make_state(
    sample_path: str,
    *,
    preset: str = "Defaults",
    residue: bytes = b"",
    magic: bytes = MAGIC,
    version: bytes = LAYOUT_VERSION,
) -> bytes:
    """Build an xfadelooper state in the measured layout.

    ``residue`` is the stale tail a longer previous path leaves behind the
    terminator, which real states are full of.
    """
    name_field = preset.encode("latin-1").ljust(NAME_FIELD_SIZE, b"\x00")
    encoded = sample_path.encode("latin-1")
    path_field = bytearray(PATH_FIELD_SIZE)
    path_field[: len(encoded)] = encoded
    if residue:
        start = len(encoded) + 1
        path_field[start : start + len(residue)] = residue
    params = struct.pack(f"<{PARAM_COUNT}f", *PARAMS)
    state = magic + version + name_field + bytes(path_field) + params
    assert len(state) == STATE_SIZE
    return state


def make_plugin(
    state: bytes,
    *,
    plug_name: str = "xfadelooper",
    unique_id: int | None = XFADE_UNIQUE_ID,
) -> PluginData:
    """Wrap a state in a minimal xfadelooper PluginDesc."""
    buffer_text = string_to_xml(state.hex().upper(), levels=2)
    unique_id_line = f'    <UniqueId Value="{unique_id}" />\n' if unique_id is not None else ""
    xml = (
        "<PluginDesc>\n"
        '  <VstPluginInfo Id="0">\n'
        '    <Path Value="C:/Vst64/xfadelooper.dll" />\n'
        f'    <PlugName Value="{plug_name}" />\n'
        f"{unique_id_line}"
        "    <Preset>\n"
        '      <VstPreset Id="0">\n'
        f"        <Buffer>{buffer_text}</Buffer>\n"
        "      </VstPreset>\n"
        "    </Preset>\n"
        "  </VstPluginInfo>\n"
        "</PluginDesc>\n"
    )
    return PluginData.from_element(ET.fromstring(xml))


def db_entry(path: pathlib.Path) -> dict[str, str | int | float]:
    return {"name": path.name, "size": 0, "last_modified": 0}


def test_registry_dispatches_by_unique_id() -> None:
    plugin = make_plugin(make_state("Z:\\loops\\one.wav"))
    assert isinstance(get_parser_for_plugin(plugin), XfadeLooperParser)


def test_registry_dispatches_by_name_when_id_missing() -> None:
    plugin = make_plugin(make_state("Z:\\loops\\one.wav"), unique_id=None)
    assert isinstance(get_parser_for_plugin(plugin), XfadeLooperParser)


def test_registry_dispatches_the_64_bit_spelling() -> None:
    """The plugin ships as both "xfadelooper" and "xfadelooper.64"."""
    plugin = make_plugin(make_state("Z:\\loops\\one.wav"), plug_name="xfadelooper.64", unique_id=None)
    assert isinstance(get_parser_for_plugin(plugin), XfadeLooperParser)


def test_reads_sample_path_from_state() -> None:
    plugin = make_plugin(make_state("Z:\\loops\\one.wav"))
    assert XfadeLooperParser().get_sample_paths(plugin) == [pathlib.Path("Z:/loops/one.wav")]


def test_reads_path_past_stale_residue() -> None:
    """Real states keep the tail of a longer previous path after the terminator."""
    state = make_state("Z:\\loops\\one.wav", residue=b"much longer previous name.wav\x00")
    plugin = make_plugin(state)
    assert XfadeLooperParser().get_sample_paths(plugin) == [pathlib.Path("Z:/loops/one.wav")]


def test_empty_path_field_means_no_sample_loaded() -> None:
    plugin = make_plugin(make_state(""))
    parser = XfadeLooperParser()
    assert parser.get_sample_paths(plugin) == []
    assert parser.analyze(plugin).issues == []


def test_unknown_layout_version_is_left_alone() -> None:
    """A version this parser has not measured gets no claims made about it."""
    state = make_state("Z:\\loops\\one.wav", version=b"\x00\x09\x09\x00")
    plugin = make_plugin(state)
    parser = XfadeLooperParser()
    assert parser.get_sample_paths(plugin) == []
    assert parser.set_sample_path(plugin, pathlib.Path("a"), pathlib.Path("b")) is False


def test_foreign_magic_is_left_alone() -> None:
    plugin = make_plugin(make_state("Z:\\loops\\one.wav", magic=b"notxfade"))
    assert XfadeLooperParser().get_sample_paths(plugin) == []


def test_analyze_flags_a_missing_sample() -> None:
    plugin = make_plugin(make_state("Z:\\loops\\gone.wav"))
    analysis = XfadeLooperParser().analyze(plugin)
    assert analysis.issues == ["Missing sample: " + str(pathlib.Path("Z:/loops/gone.wav"))]
    assert analysis.can_fix is True


def test_analyze_is_quiet_when_the_sample_is_there(tmp_path: pathlib.Path) -> None:
    present = tmp_path / "here.wav"
    present.write_bytes(b"")
    plugin = make_plugin(make_state(str(present)))
    analysis = XfadeLooperParser().analyze(plugin)
    assert analysis.issues == []
    assert analysis.metadata["sample_exists"] is True


def test_fix_relinks_missing_sample_to_best_candidate(tmp_path: pathlib.Path) -> None:
    missing = tmp_path / "from" / "old.wav"  # never created
    plugin = make_plugin(make_state(str(missing)))

    # Two candidates share the filename; the one whose folder matches wins.
    right = tmp_path / "backup" / "from" / "old.wav"
    wrong = tmp_path / "other" / "old.wav"
    db = {str(wrong): db_entry(wrong), str(right): db_entry(right)}

    assert fix_plugin(plugin, db) is True
    assert XfadeLooperParser().get_sample_paths(plugin) == [right]


def test_fix_returns_false_without_candidates(tmp_path: pathlib.Path) -> None:
    missing = tmp_path / "from" / "old.wav"
    plugin = make_plugin(make_state(str(missing)))
    other = tmp_path / "unrelated.wav"
    assert fix_plugin(plugin, {str(other): db_entry(other)}) is False


def test_fix_skips_sample_that_exists(tmp_path: pathlib.Path) -> None:
    present = tmp_path / "ok.wav"
    present.write_bytes(b"")
    plugin = make_plugin(make_state(str(present)))
    elsewhere = tmp_path / "elsewhere" / "ok.wav"
    assert fix_plugin(plugin, {str(elsewhere): db_entry(elsewhere)}) is False


def test_write_keeps_the_container_intact() -> None:
    """A longer path may not disturb the size, the header, or the parameters."""
    before = make_state("Z:\\loops\\one.wav", residue=b"stale tail from an older path.wav\x00")
    plugin = make_plugin(before)
    parser = XfadeLooperParser()

    replacement = pathlib.Path("Z:/relinked/much/deeper/renamed-sample.wav")
    assert parser.set_sample_path(plugin, pathlib.Path("Z:/loops/one.wav"), replacement) is True

    after = plugin.get_buffer_raw_bytes()
    assert len(after) == len(before)
    assert after[:PATH_OFFSET] == before[:PATH_OFFSET]
    assert after[PATH_END:] == before[PATH_END:]
    assert struct.unpack(f"<{PARAM_COUNT}f", after[PATH_END:]) == PARAMS


def test_write_clears_residue_and_reads_back() -> None:
    plugin = make_plugin(make_state("Z:\\loops\\a-long-old-name.wav", residue=b"leftovers.wav\x00"))
    parser = XfadeLooperParser()
    replacement = pathlib.Path("Z:/new.wav")
    assert parser.set_sample_path(plugin, pathlib.Path("Z:/loops/a-long-old-name.wav"), replacement) is True

    # Re-parse our own output rather than trusting the write.
    reparsed = make_plugin(plugin.get_buffer_raw_bytes())
    assert parser.get_sample_paths(reparsed) == [replacement]
    field = plugin.get_buffer_raw_bytes()[PATH_OFFSET:PATH_END]
    assert field == str(replacement).encode("latin-1").ljust(PATH_FIELD_SIZE, b"\x00")


def test_write_refuses_a_path_too_long_for_the_field() -> None:
    """Truncating into the fixed field would produce a path pointing nowhere."""
    before = make_state("Z:\\loops\\one.wav")
    plugin = make_plugin(before)
    parser = XfadeLooperParser()

    too_long = pathlib.Path("Z:/" + "x" * PATH_FIELD_SIZE + ".wav")
    assert parser.set_sample_path(plugin, pathlib.Path("Z:/loops/one.wav"), too_long) is False
    assert plugin.get_buffer_raw_bytes() == before


def test_write_takes_the_longest_path_that_fits() -> None:
    plugin = make_plugin(make_state("Z:\\loops\\one.wav"))
    parser = XfadeLooperParser()

    longest = pathlib.Path("Z:/" + "x" * (PATH_FIELD_SIZE - 1 - len("Z:/")))
    assert len(str(longest)) == PATH_FIELD_SIZE - 1
    assert parser.set_sample_path(plugin, pathlib.Path("Z:/loops/one.wav"), longest) is True
    assert parser.get_sample_paths(make_plugin(plugin.get_buffer_raw_bytes())) == [longest]
