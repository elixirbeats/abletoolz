"""The local plugin database: what this machine has, written down once.

Hermetic and synthetic. Live's plugin database is built row by row in the
schema measured on Live 12.4.5b, the plugin folders are tmp_path, and every
plugin named here is either invented or a product name -- nothing reads the
machine running the tests.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import pathlib

import pydantic
import pytest
from conftest import OTHER_CID, OTHER_FIELDS, SERUM_CID, SERUM_FIELDS, installed, live_database

from abletoolz.machine import Locations
from abletoolz.plugins import config as config_module
from abletoolz.plugins.config import AbletoolzConfig
from abletoolz.plugins.identity.ids import PluginKind, Vst3Id
from abletoolz.plugins.identity.plugin_db import (
    PluginDatabase,
    PluginEntry,
    PluginSource,
    build_plugin_db,
    create_or_update_db,
    load_plugin_db,
    read_plugin_db,
    write_plugin_db,
)
from abletoolz.plugins.state.all_plugins import CATALOG

BUILT = datetime.datetime(2026, 8, 12, 9, 30, tzinfo=datetime.UTC)


def make_db(*entries: PluginEntry) -> PluginDatabase:
    return PluginDatabase(built=BUILT, plugins=entries)


def serum_database(tmp_path: pathlib.Path) -> pathlib.Path:
    """A Live database holding Serum in both formats, the VST2 folder switched off."""
    return live_database(
        tmp_path,
        [
            ("Serum", f"device:vst3:instr:{SERUM_CID}", installed(tmp_path, "Serum.vst3"), "global", True),
            (
                "Serum_x64",
                "device:vst:instr:1483109208?n=Serum_x64",
                installed(tmp_path, "Serum_x64.dll"),
                "custom",
                False,
            ),
        ],
    )


def write_uid_db(path: pathlib.Path, entries: dict[str, list[int]]) -> pathlib.Path:
    """The class id probe's own JSON shape, extra keys and all."""
    document = {
        name: {
            "name": name,
            "fields": fields,
            "cid_canonical_hex": "00" * 16,
            "vendor": "Vendor",
            "module": f"plugins/{name}.vst3",
            "category": "Audio Module Class",
        }
        for name, fields in entries.items()
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def write_moduleinfo(root: pathlib.Path, bundle: str, body: str) -> None:
    """An installed VST3 bundle's moduleinfo.json, written as the vendor shipped it."""
    contents = root / f"{bundle}.vst3" / "Contents"
    contents.mkdir(parents=True)
    (contents / "moduleinfo.json").write_text(body, encoding="utf-8")


def audio_module(cid: str, name: str) -> str:
    return json.dumps({"Classes": [{"CID": cid, "Category": "Audio Module Class", "Name": name}]})


# -- what a build reads -----------------------------------------------------


def test_live_database_rows_become_records_of_both_formats(tmp_path: pathlib.Path) -> None:
    database = build_plugin_db(database_dir=serum_database(tmp_path), vst_dirs=[], built=BUILT)
    by_name = {entry.name: entry for entry in database.plugins}

    assert by_name["Serum"].kind is PluginKind.VST3
    assert by_name["Serum"].uid_fields == SERUM_FIELDS
    assert by_name["Serum"].unique_id is None
    assert by_name["Serum"].source is PluginSource.LIVE_DATABASE
    assert by_name["Serum"].vendor == "Vendor"
    # The dashed class id Live's database spells is the one the catalog knows as Serum.
    serum = CATALOG.find(Vst3Id(SERUM_FIELDS))
    assert serum is not None and serum.name == "Serum"
    # The VST2 identifies itself by UniqueId; there is no class id to have.
    assert by_name["Serum_x64"].kind is PluginKind.VST
    assert by_name["Serum_x64"].unique_id == 1483109208
    assert by_name["Serum_x64"].uid_fields is None
    assert by_name["Serum_x64"].module_path == tmp_path / "Serum_x64.dll"
    counts = {(count.kind, count.source): count.count for count in database.counts()}
    assert counts == {(PluginKind.VST, PluginSource.LIVE_DATABASE): 1, (PluginKind.VST3, PluginSource.LIVE_DATABASE): 1}


def test_an_unreadable_dev_identifier_is_skipped_rather_than_guessed(tmp_path: pathlib.Path) -> None:
    database_dir = live_database(
        tmp_path,
        [
            ("Short", "device:vst3:audiofx:abc", installed(tmp_path, "Short.vst3"), "global", True),
            ("Odd", "something else entirely", installed(tmp_path, "Odd.vst3"), "global", True),
            ("Good", f"device:vst3:audiofx:{OTHER_CID}", installed(tmp_path, "Good.vst3"), "global", True),
        ],
    )
    database = build_plugin_db(database_dir=database_dir, vst_dirs=[], built=BUILT)
    assert [(entry.name, entry.uid_fields) for entry in database.plugins] == [("Good", OTHER_FIELDS)]


def test_a_probed_uid_file_and_a_bundle_both_contribute_class_ids(tmp_path: pathlib.Path) -> None:
    """The probe's file keeps only each plugin's fields, whatever else it wrote down."""
    uid_db = write_uid_db(tmp_path / "probed.json", {"Massive X": [1, 2, 3, 4]})
    write_moduleinfo(tmp_path / "vst3", "Thing", audio_module("00" * 15 + "63", "In Bundle"))
    database = build_plugin_db(database_dir=None, vst_dirs=[tmp_path / "vst3"], uid_db=uid_db, built=BUILT)

    by_name = {entry.name: entry for entry in database.plugins}
    assert by_name["Massive X"].source is PluginSource.UID_DB
    assert by_name["Massive X"].uid_fields == (1, 2, 3, 4)
    assert by_name["In Bundle"].source is PluginSource.MODULEINFO
    assert by_name["In Bundle"].uid_fields == (0, 0, 0, 99)


def test_a_malformed_probed_uid_file_stops_the_build(tmp_path: pathlib.Path) -> None:
    """Three fields is not a class id, and pretending otherwise loads the wrong plugin."""
    uid_db = tmp_path / "bad.json"
    uid_db.write_text(json.dumps({"Thing": {"fields": [1, 2, 3]}}), encoding="utf-8")
    with pytest.raises(pydantic.ValidationError):
        build_plugin_db(database_dir=None, vst_dirs=[], uid_db=uid_db, built=BUILT)


def test_moduleinfo_gives_audio_modules_only_and_forgives_trailing_commas(tmp_path: pathlib.Path) -> None:
    """Measured: some vendors ship moduleinfo.json that plain json refuses.

    A controller class is not something a set stores, and a Windows-style
    single-file .vst3 carries no moduleinfo.json to read.
    """
    folder = tmp_path / "VST3"
    write_moduleinfo(
        folder,
        "Thing",
        """
        {
          "Name": "Thing",
          "Classes": [
            {"CID": "56535458667358736572756D00000000", "Category": "Audio Module Class", "Name": "Thing"},
            {"CID": "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF", "Category": "Component Controller Class",
             "Name": "Thing Controller"}
          ]
        }
        """,
    )
    write_moduleinfo(
        folder,
        "Loose",
        """
        {
          "Name": "Loose",
          "Classes": [
            {
              "CID": "0000000A0000000B0000000C0000000D",
              "Category": "Audio Module Class",
              "Name": "Loose",
            },
          ],
        }
        """,
    )
    installed(folder, "Old.vst3")
    database = build_plugin_db(database_dir=None, vst_dirs=[folder], built=BUILT)

    from_bundles = {
        entry.name: entry.uid_fields for entry in database.plugins if entry.source is PluginSource.MODULEINFO
    }
    assert from_bundles == {"Thing": SERUM_FIELDS, "Loose": OTHER_FIELDS}


# -- the file ---------------------------------------------------------------


def test_a_database_round_trips_through_json_on_a_fresh_machine(tmp_path: pathlib.Path) -> None:
    """The config folder may not exist yet; writing makes it."""
    database = make_db(
        PluginEntry(
            name="Serum",
            kind=PluginKind.VST3,
            source=PluginSource.LIVE_DATABASE,
            vendor="Xfer Records",
            uid_fields=SERUM_FIELDS,
            module_path=tmp_path / "Serum.vst3",
        ),
        PluginEntry(
            name="Serum_x64",
            kind=PluginKind.VST,
            source=PluginSource.LIVE_DATABASE,
            vendor="Xfer Records",
            unique_id=1483109208,
            module_path=tmp_path / "Serum_x64.dll",
        ),
    )
    path = write_plugin_db(database, tmp_path / "fresh" / "config" / "plugin_db.json")
    assert read_plugin_db(path) == database


def test_a_written_database_is_readable_json_a_human_can_check(tmp_path: pathlib.Path) -> None:
    database = make_db(
        PluginEntry(name="Serum", kind=PluginKind.VST3, source=PluginSource.LIVE_DATABASE, uid_fields=SERUM_FIELDS)
    )
    path = write_plugin_db(database, tmp_path / "plugin_db.json")
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["plugins"][0]["uid_fields"] == list(SERUM_FIELDS)
    assert document["plugins"][0]["source"] == "live-database"
    assert document["built"].startswith("2026-08-12")


def test_a_missing_file_says_how_to_make_one(tmp_path: pathlib.Path) -> None:
    with pytest.raises(FileNotFoundError, match="--plugin-db"):
        read_plugin_db(tmp_path / "not there.json")


def test_building_replaces_rather_than_merges(tmp_path: pathlib.Path, locations: Locations) -> None:
    """A plugin uninstalled since the last run has to leave the database."""
    path = tmp_path / "plugin_db.json"
    write_plugin_db(make_db(PluginEntry(name="Gone", kind=PluginKind.VST3, source=PluginSource.LIVE_DATABASE)), path)
    rebuilt = create_or_update_db(AbletoolzConfig(), locations, path)
    assert rebuilt.plugins == ()
    assert read_plugin_db(path).plugins == ()


def test_a_command_builds_in_memory_when_no_file_exists_yet(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture, locations: Locations
) -> None:
    """Nothing refuses to run because nobody has built the database yet."""
    write_moduleinfo(tmp_path / "VST3", "Thing", audio_module("00" * 15 + "63", "Thing"))
    path = tmp_path / "plugin_db.json"
    with caplog.at_level("INFO"):
        database = load_plugin_db(AbletoolzConfig(plugin_paths=[tmp_path / "VST3"]), locations, path)
    assert [entry.name for entry in database.plugins] == ["Thing"]
    assert not path.exists()
    assert "--plugin-db" in caplog.text


def test_a_file_an_older_version_wrote_is_read_past_rather_than_failing_the_command(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture, locations: Locations
) -> None:
    """Older builds wrote folder-scan records and architectures; a plan must still run, and say to rebuild."""
    path = tmp_path / "plugin_db.json"
    record = {"name": "Thing", "kind": "vst", "source": "folder-scan", "arch": "x64"}
    path.write_text(json.dumps({"built": BUILT.isoformat(), "plugins": [record]}), encoding="utf-8")
    database = load_plugin_db(AbletoolzConfig(), locations, path)
    assert database.plugins == ()
    assert "--plugin-db" in caplog.text


def test_config_paths_are_read_alongside_the_standard_locations(tmp_path: pathlib.Path, locations: Locations) -> None:
    """plugin_database.paths does for plugins what sample_database.paths does for samples."""
    write_moduleinfo(tmp_path / "Standard", "Standard", audio_module("00" * 15 + "63", "Standard"))
    write_moduleinfo(tmp_path / "Elsewhere", "Extra", audio_module("00" * 15 + "64", "Extra"))
    here = dataclasses.replace(locations, vst_dirs=(tmp_path / "Standard",))

    database = load_plugin_db(AbletoolzConfig(plugin_paths=[tmp_path / "Elsewhere"]), here, tmp_path / "plugin_db.json")
    assert sorted(entry.name for entry in database.plugins) == ["Extra", "Standard"]


# -- the config that points at a probed file ----------------------------------


@pytest.mark.parametrize("section", ["plugin_database", "plugin_translation"])
def test_the_uid_db_path_comes_out_of_its_section_or_the_old_one(tmp_path: pathlib.Path, section: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(f"{section}:\n  uid_db: probed.json\n", encoding="utf-8")
    assert config_module.load_config(path).plugin_uid_db == pathlib.Path("probed.json")


def test_a_retired_section_is_named_once_and_ignored(tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
plugin_upgrades:
  exact_rules:
    'Old.64.dll': ['New.dll']
plugin_translation:
  targets:
    'kHs Thing':
      name: kHs Thing
""",
        encoding="utf-8",
    )
    assert config_module.load_config(path) == config_module.AbletoolzConfig()
    assert "plugin_upgrades, plugin_translation.targets are retired and ignored" in caplog.text


# -- class ids out of the database ------------------------------------------


def test_class_ids_rank_live_then_the_probed_file_then_moduleinfo(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Three sources and two disagreements, and the higher source wins each.

    A wrong class id does not fail, it loads another plugin. So every disagreement gets said out loud.
    """
    uid_db = write_uid_db(
        tmp_path / "probed.json",
        {"Serum": [9, 9, 9, 9], "Only Probed": list(OTHER_FIELDS), "Probed And Bundled": [1, 2, 3, 4]},
    )
    write_moduleinfo(tmp_path / "vst3", "Thing", audio_module("00" * 15 + "63", "In Bundle"))
    write_moduleinfo(tmp_path / "vst3", "Other", audio_module("00" * 15 + "64", "Probed And Bundled"))
    database = build_plugin_db(
        database_dir=serum_database(tmp_path),
        vst_dirs=[tmp_path / "vst3"],
        uid_db=uid_db,
        built=BUILT,
    )
    with caplog.at_level("ERROR"):
        lookup = database.uid_lookup()

    assert lookup.resolve("Serum") == SERUM_FIELDS
    assert lookup.resolve("Only Probed") == OTHER_FIELDS
    assert lookup.resolve("Probed And Bundled") == (1, 2, 3, 4)
    assert lookup.resolve("In Bundle") == (0, 0, 0, 99)
    # A VST2 record contributes no class id.
    assert lookup.resolve("Serum_x64") is None
    assert lookup.names() == {"Serum", "Only Probed", "Probed And Bundled", "In Bundle"}
    assert {(item.name, item.winner, item.loser) for item in lookup.disagreements()} == {
        ("Probed And Bundled", "uid-db", "moduleinfo"),
        ("Serum", "live-database", "uid-db"),
    }
    assert "Class id sources disagree about Serum" in caplog.text
    assert "Class id sources disagree about Probed And Bundled" in caplog.text


def test_one_plugin_in_two_places_is_two_records(tmp_path: pathlib.Path) -> None:
    """Kept apart on purpose: a merged record could not report a disagreement."""
    uid_db = write_uid_db(tmp_path / "probed.json", {"Serum": [9, 9, 9, 9]})
    database = build_plugin_db(database_dir=serum_database(tmp_path), vst_dirs=[], uid_db=uid_db, built=BUILT)

    serum = [entry for entry in database.plugins if entry.name == "Serum"]
    assert [entry.source for entry in serum] == [PluginSource.LIVE_DATABASE, PluginSource.UID_DB]
    # The collapsed view is what a consumer pairs names against.
    assert [entry.source for entry in database.installed(PluginKind.VST3)] == [PluginSource.LIVE_DATABASE]


def test_a_caller_uid_beats_every_source_in_the_database() -> None:
    database = make_db(
        PluginEntry(name="Thing", kind=PluginKind.VST3, source=PluginSource.LIVE_DATABASE, uid_fields=(1, 2, 3, 4))
    )
    assert database.uid_lookup(extra={"Thing": (4, 4, 4, 4)}).resolve("Thing") == (4, 4, 4, 4)
