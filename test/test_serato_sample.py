"""Serato Sample parser: registry dispatch, buffer decode, DB-driven relink."""

from __future__ import annotations

import pathlib
from xml.etree import ElementTree as ET

from abletoolz.formats import hex_text
from abletoolz.plugin_parsers.base import PluginData
from abletoolz.plugin_parsers.parsers.serato import NO_SAMPLE_ISSUE, SeratoSampleParser
from abletoolz.plugin_parsers.registry import fix_plugin, get_parser_for_plugin
from abletoolz.samples.database import DatabaseT

SERATO_UNIQUE_ID = 1399681132


def make_serato_xml(json_payload: str, *, unique_id: int | None = SERATO_UNIQUE_ID) -> str:
    """Embed a JSON payload into a minimal Serato Sample PluginDesc."""
    buffer_text = hex_text.encode(json_payload.encode("utf-8"), 2)
    unique_id_line = f'    <UniqueId Value="{unique_id}" />\n' if unique_id is not None else ""
    return (
        "<PluginDesc>\n"
        '  <VstPluginInfo Id="0">\n'
        '    <Path Value="C:/Vst64/Serato Sample.dll" />\n'
        '    <PlugName Value="Serato Sample" />\n'
        f"{unique_id_line}"
        "    <Preset>\n"
        '      <VstPreset Id="0">\n'
        f"        <Buffer>{buffer_text}</Buffer>\n"
        "      </VstPreset>\n"
        "    </Preset>\n"
        "  </VstPluginInfo>\n"
        "</PluginDesc>\n"
    )


def make_plugin(source_file: pathlib.Path, *, unique_id: int | None = SERATO_UNIQUE_ID) -> PluginData:
    payload = f'{{"project":{{"sourceSong":{{"File":"{source_file.as_posix()}"}}}}}}'
    root = ET.fromstring(make_serato_xml(payload, unique_id=unique_id))
    return PluginData.from_element(root)


def make_empty_plugin() -> PluginData:
    """The project Serato Sample writes once the sample it pointed at is gone: no sourceSong at all."""
    payload = '{"project":{"Version":35,"VaporwaveWaveformsActive":false,"VelocitySensitive":false,"Quantize":false}}'
    root = ET.fromstring(make_serato_xml(payload))
    return PluginData.from_element(root)


def test_registry_dispatches_by_unique_id(tmp_path: pathlib.Path) -> None:
    plugin = make_plugin(tmp_path / "a.wav")
    parser = get_parser_for_plugin(plugin)
    assert isinstance(parser, SeratoSampleParser)


def test_registry_dispatches_by_name_when_id_missing(tmp_path: pathlib.Path) -> None:
    plugin = make_plugin(tmp_path / "a.wav", unique_id=None)
    parser = get_parser_for_plugin(plugin)
    assert isinstance(parser, SeratoSampleParser)


def test_decodes_sample_path_from_buffer(tmp_path: pathlib.Path) -> None:
    source = tmp_path / "from" / "old.wav"
    plugin = make_plugin(source)
    parser = SeratoSampleParser()
    assert parser.get_sample_paths(plugin) == [source]


def test_fix_relinks_missing_sample_to_best_candidate(tmp_path: pathlib.Path) -> None:
    missing = tmp_path / "from" / "old.wav"  # never created
    plugin = make_plugin(missing)

    # Two candidates share the filename; the one whose folder matches wins.
    right = tmp_path / "backup" / "from" / "old.wav"
    wrong = tmp_path / "other" / "old.wav"
    db: DatabaseT = {
        wrong.as_posix(): {"name": "old.wav", "size": 0, "last_modified": 0},
        right.as_posix(): {"name": "old.wav", "size": 0, "last_modified": 0},
    }

    assert fix_plugin(plugin, db) is True
    parser = SeratoSampleParser()
    assert parser.get_sample_paths(plugin) == [right]


def test_fix_returns_false_without_candidates(tmp_path: pathlib.Path) -> None:
    missing = tmp_path / "from" / "old.wav"
    plugin = make_plugin(missing)
    db: DatabaseT = {
        (tmp_path / "unrelated.wav").as_posix(): {"name": "unrelated.wav", "size": 0, "last_modified": 0},
    }

    assert fix_plugin(plugin, db) is False
    parser = SeratoSampleParser()
    assert parser.get_sample_paths(plugin) == [missing]


def test_fix_skips_sample_that_exists(tmp_path: pathlib.Path) -> None:
    present = tmp_path / "here" / "ok.wav"
    present.parent.mkdir()
    present.write_bytes(b"")
    plugin = make_plugin(present)
    db: DatabaseT = {
        (tmp_path / "elsewhere" / "ok.wav").as_posix(): {"name": "ok.wav", "size": 0, "last_modified": 0},
    }

    assert fix_plugin(plugin, db) is False
    parser = SeratoSampleParser()
    assert parser.get_sample_paths(plugin) == [present]


def test_analyze_flags_missing_sample_as_fixable(tmp_path: pathlib.Path) -> None:
    missing = tmp_path / "gone" / "old.wav"
    analysis = SeratoSampleParser().analyze(make_plugin(missing))
    assert analysis.issues == [f"Missing sample: {missing}"]
    assert analysis.can_fix is True


def test_analyze_reports_project_with_no_sample() -> None:
    analysis = SeratoSampleParser().analyze(make_empty_plugin())
    assert analysis.issues == [NO_SAMPLE_ISSUE]
    assert analysis.can_fix is False
    assert analysis.metadata["sample_path"] is None


def test_fix_refuses_project_with_no_sample(tmp_path: pathlib.Path) -> None:
    plugin = make_empty_plugin()
    assert plugin.buffer_element is not None
    before = plugin.buffer_element.text
    db: DatabaseT = {
        (tmp_path / "any.wav").as_posix(): {"name": "any.wav", "size": 0, "last_modified": 0},
    }
    assert fix_plugin(plugin, db) is False
    assert plugin.buffer_element.text == before
