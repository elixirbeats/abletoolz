"""Maschine 2 parser: registry dispatch, length-prefixed string framing, detection only.

Every buffer here is synthesized from the measured framing. Nothing in this file
comes out of a real set.
"""

from __future__ import annotations

import pathlib
import struct
from collections.abc import Iterable
from xml.etree import ElementTree as ET

from abletoolz.decode_encode import string_to_xml
from abletoolz.plugin_parsers.base import PluginData
from abletoolz.plugin_parsers.registry import fix_plugin, get_parser_for_plugin
from abletoolz.plugin_parsers.state.maschine import CONTAINER_MAGIC, Maschine2Parser

MASCHINE_UNIQUE_ID = 1315523890


def ni_string(text: str) -> bytes:
    """Frame a string the way the container does: u32 character count, then UTF-16LE."""
    return struct.pack("<I", len(text)) + text.encode("utf-16-le")


def make_state(strings: Iterable[str], *, magic: bytes = CONTAINER_MAGIC) -> bytes:
    """Build a chunk container holding the given strings, in order."""
    body = b"".join(ni_string(text) for text in strings)
    # Chunk header: total size, type, magic. The size at offset 0 covers the lot.
    total = 8 + 4 + 4 + len(body)
    return struct.pack("<QI", total, 1) + magic + body


def make_plugin(
    state: bytes,
    *,
    plug_name: str = "Maschine 2",
    unique_id: int | None = MASCHINE_UNIQUE_ID,
) -> PluginData:
    """Wrap a state in a minimal Maschine 2 PluginDesc."""
    buffer_text = string_to_xml(state.hex().upper(), levels=2)
    unique_id_line = f'    <UniqueId Value="{unique_id}" />\n' if unique_id is not None else ""
    xml = (
        "<PluginDesc>\n"
        '  <VstPluginInfo Id="0">\n'
        '    <Path Value="C:/Vst64/Maschine 2.dll" />\n'
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


def test_registry_dispatches_by_unique_id() -> None:
    plugin = make_plugin(make_state(["Z:\\Kits\\Some Kit.mxgrp"]))
    assert isinstance(get_parser_for_plugin(plugin), Maschine2Parser)


def test_registry_dispatches_by_name_when_id_missing() -> None:
    plugin = make_plugin(make_state([]), unique_id=None)
    assert isinstance(get_parser_for_plugin(plugin), Maschine2Parser)


def test_registry_dispatches_the_effect_build() -> None:
    plugin = make_plugin(make_state([]), plug_name="Maschine 2 FX", unique_id=None)
    assert isinstance(get_parser_for_plugin(plugin), Maschine2Parser)


def test_reads_kit_and_audio_references_in_order() -> None:
    state = make_state(
        [
            "Group",
            "Z:\\Kits\\Some Kit.mxgrp",
            "Sample",
            "Z:\\One Shots\\A Snare.wav",
        ]
    )
    assert Maschine2Parser().get_sample_paths(make_plugin(state)) == [
        pathlib.Path("Z:/Kits/Some Kit.mxgrp"),
        pathlib.Path("Z:/One Shots/A Snare.wav"),
    ]


def test_repeated_references_are_reported_once() -> None:
    """Maschine writes the same kit path several times over."""
    kit = "Z:\\Kits\\Some Kit.mxgrp"
    assert Maschine2Parser().get_sample_paths(make_plugin(make_state([kit, kit, kit]))) == [
        pathlib.Path("Z:/Kits/Some Kit.mxgrp")
    ]


def test_ignores_strings_that_are_not_media() -> None:
    state = make_state(["Group-ni", "All...", "Z:\\Program Files\\Something\\plugin.dll"])
    assert Maschine2Parser().get_sample_paths(make_plugin(state)) == []


def test_framing_decides_a_candidate_not_the_text() -> None:
    """A path whose length prefix does not match it is not a reference."""
    path = "Z:\\Kits\\Some Kit.mxgrp"
    good = ni_string(path)
    bad = struct.pack("<I", len(path) - 3) + path.encode("utf-16-le")
    body = good + bad
    total = 8 + 4 + 4 + len(body)
    state = struct.pack("<QI", total, 1) + CONTAINER_MAGIC + body

    # Only the correctly framed one is picked up, and the mis-framed copy of the
    # same text does not sneak in behind it.
    assert Maschine2Parser().get_sample_paths(make_plugin(state)) == [pathlib.Path("Z:/Kits/Some Kit.mxgrp")]


def test_non_container_buffer_is_left_alone() -> None:
    plugin = make_plugin(make_state(["Z:\\Kits\\Some Kit.mxgrp"], magic=b"JUNK"))
    parser = Maschine2Parser()
    assert parser.get_sample_paths(plugin) == []
    assert parser.analyze(plugin).issues == []


def test_analyze_names_missing_kits_and_samples() -> None:
    state = make_state(["Z:\\Kits\\Gone Kit.mxgrp", "Z:\\One Shots\\Gone Snare.wav"])
    analysis = Maschine2Parser().analyze(make_plugin(state))

    assert analysis.issues[0] == "Missing kit: " + str(pathlib.Path("Z:/Kits/Gone Kit.mxgrp"))
    assert analysis.issues[1] == "Missing sample: " + str(pathlib.Path("Z:/One Shots/Gone Snare.wav"))
    assert "cannot be relinked" in analysis.issues[2]
    assert analysis.metadata["kit_references"] == 1
    assert analysis.metadata["audio_references"] == 1
    assert analysis.metadata["relink_supported"] is False
    assert analysis.can_fix is False


def test_analyze_is_quiet_when_everything_is_there(tmp_path: pathlib.Path) -> None:
    present = tmp_path / "A Snare.wav"
    present.write_bytes(b"")
    analysis = Maschine2Parser().analyze(make_plugin(make_state([str(present)])))

    assert analysis.issues == []
    assert analysis.metadata["missing_references"] == []
    assert analysis.metadata["references"] == [str(present)]


def test_never_rewrites_the_buffer() -> None:
    """Detection only: every enclosing chunk length would have to move with the path."""
    state = make_state(["Z:\\Kits\\Gone Kit.mxgrp"])
    plugin = make_plugin(state)
    parser = Maschine2Parser()

    assert parser.set_sample_path(plugin, pathlib.Path("Z:/Kits/Gone Kit.mxgrp"), pathlib.Path("Z:/b.mxgrp")) is False
    assert plugin.get_buffer_raw_bytes() == state


def test_fix_declines_even_with_a_candidate_in_the_database(tmp_path: pathlib.Path) -> None:
    replacement = tmp_path / "moved" / "Gone Snare.wav"
    db: dict[str, dict[str, str | int | float]] = {
        str(replacement): {"name": "Gone Snare.wav", "size": 0, "last_modified": 0}
    }
    state = make_state(["Z:\\One Shots\\Gone Snare.wav"])
    plugin = make_plugin(state)

    assert fix_plugin(plugin, db) is False
    assert plugin.get_buffer_raw_bytes() == state
