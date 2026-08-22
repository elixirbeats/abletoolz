"""The local plugin database: what this machine has, written down once.

Hermetic and synthetic. Live's plugin database is built row by row by
``test_repair.write_database`` in the schema measured on Live 12.4.5b, the
plugin folders are tmp_path, and every plugin named here is either invented or a
product name -- nothing reads the machine running the tests.
"""

from __future__ import annotations

import datetime
import functools
import json
import pathlib

import pytest
from test_read_plugin_files import ARM64, X86_64, fat, make_bundle
from test_repair import OTHER_CID, OTHER_FIELDS, SERUM_CID, SERUM_FIELDS, installed, write_database, write_uid_db

from abletoolz.plugin_parsers import plugin_db
from abletoolz.plugin_parsers.base import PluginKind
from abletoolz.plugin_parsers.config import AbletoolzConfig
from abletoolz.plugin_parsers.plugin_db import (
    PluginDatabase,
    PluginEntry,
    PluginSource,
    build_plugin_db,
    create_or_update_db,
    load_plugin_db,
    read_plugin_db,
    write_plugin_db,
)
from abletoolz.plugin_parsers.read_plugin_files import scan_plugin_dirs

BUILT = datetime.datetime(2026, 8, 12, 9, 30, tzinfo=datetime.UTC)


@pytest.fixture(autouse=True)
def no_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing falls back to the machine running the tests, however it is called."""
    monkeypatch.setattr(plugin_db, "default_vst_dirs", lambda: [])
    monkeypatch.setattr(plugin_db, "default_live_database_dir", lambda: None)


def make_db(*entries: PluginEntry) -> PluginDatabase:
    return PluginDatabase(built=BUILT, plugins=entries)


def live_database(tmp_path: pathlib.Path) -> pathlib.Path:
    """A Live database holding Serum in both formats, the VST2 folder switched off."""
    database_dir = tmp_path / "Live Database"
    database_dir.mkdir()
    write_database(
        database_dir / "Live-plugins-1.db",
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
    return database_dir


# -- what a build reads -----------------------------------------------------


def test_live_database_rows_become_records_of_both_formats(tmp_path: pathlib.Path) -> None:
    database = build_plugin_db(database_dir=live_database(tmp_path), vst_dirs=[], built=BUILT)
    by_name = {entry.name: entry for entry in database.plugins}

    assert by_name["Serum"].kind is PluginKind.VST3
    assert by_name["Serum"].uid_fields == SERUM_FIELDS
    assert by_name["Serum"].unique_id is None
    assert by_name["Serum"].source is PluginSource.LIVE_DATABASE
    assert by_name["Serum"].vendor == "Vendor"
    # The VST2 identifies itself by UniqueId; there is no class id to have.
    assert by_name["Serum_x64"].kind is PluginKind.VST
    assert by_name["Serum_x64"].unique_id == 1483109208
    assert by_name["Serum_x64"].uid_fields is None
    assert by_name["Serum_x64"].module_path == tmp_path / "Serum_x64.dll"


def test_a_folder_scan_finds_what_live_never_scanned(tmp_path: pathlib.Path) -> None:
    folder = tmp_path / "VstPlugins"
    folder.mkdir()
    (folder / "Neverscanned.dll").write_bytes(b"")
    database = build_plugin_db(database_dir=live_database(tmp_path), vst_dirs=[folder], built=BUILT)

    scanned = [entry for entry in database.plugins if entry.source is PluginSource.FOLDER_SCAN]
    assert [entry.name for entry in scanned] == ["Neverscanned"]
    assert scanned[0].kind is PluginKind.VST


def test_a_file_live_already_scanned_is_not_recorded_under_its_file_name(tmp_path: pathlib.Path) -> None:
    """Live's name for a .dll is not always the file's stem, and Live's name wins.

    Measured 2026-08-12: iZOzone9.dll is "Ozone 9" to Live, and "Ozone 9" is what
    a set stores. Recording the stem too would put the same plugin in twice, the
    second time under a name nothing uses.
    """
    database_dir = tmp_path / "Live Database"
    database_dir.mkdir()
    folder = tmp_path / "VstPlugins"
    folder.mkdir()
    module = folder / "iZOzone9.dll"
    module.write_bytes(b"")
    write_database(
        database_dir / "Live-plugins-1.db",
        [("Ozone 9", "device:vst:audiofx:1514294578?n=Ozone%209", module, "custom", False)],
    )

    database = build_plugin_db(database_dir=database_dir, vst_dirs=[folder], built=BUILT)
    assert [entry.name for entry in database.plugins] == ["Ozone 9"]


def test_a_scanned_mac_bundle_becomes_a_record_of_its_own_format(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mac plugin reaches the database the same way a Windows one does, minus a vendor.

    Nothing in an Info.plist names who made the plugin, so the company Windows
    reads off a version resource is simply absent rather than guessed at.
    """
    monkeypatch.setattr(plugin_db, "scan_plugin_dirs", functools.partial(scan_plugin_dirs, platform="darwin"))
    folder = tmp_path / "VST3"
    folder.mkdir()
    bundle = make_bundle(
        folder,
        "Pro-Q 3.vst3",
        plist={"CFBundleName": "Pro-Q 3", "CFBundleExecutable": "Pro-Q 3", "CFBundleShortVersionString": "3.24"},
        binary=fat(X86_64, ARM64),
        executable="Pro-Q 3",
    )
    database = build_plugin_db(database_dir=None, vst_dirs=[folder], built=BUILT)

    (entry,) = database.plugins
    assert entry.name == "Pro-Q 3"
    assert entry.kind is PluginKind.VST3
    assert entry.source is PluginSource.FOLDER_SCAN
    assert entry.module_path == bundle
    assert entry.arch == "universal"
    assert entry.vendor is None


def write_pe(path: pathlib.Path, *, sixty_four_bit: bool = True) -> pathlib.Path:
    """The smallest file the PE header reader will call a 64-bit (or 32-bit) module."""
    image = bytearray(0x80)
    image[0:2] = b"MZ"
    image[0x3C:0x40] = (0x40).to_bytes(4, "little")
    image[0x40:0x44] = b"PE\x00\x00"
    image[0x58:0x5A] = (0x20B if sixty_four_bit else 0x10B).to_bytes(2, "little")
    path.write_bytes(bytes(image))
    return path


@pytest.mark.parametrize(("sixty_four_bit", "arch"), [(True, "x64"), (False, "x86")])
def test_a_scanned_module_records_its_architecture(tmp_path: pathlib.Path, sixty_four_bit: bool, arch: str) -> None:
    folder = tmp_path / "VstPlugins"
    folder.mkdir()
    write_pe(folder / "Thing.dll", sixty_four_bit=sixty_four_bit)
    database = build_plugin_db(database_dir=None, vst_dirs=[folder], built=BUILT)
    assert [entry.arch for entry in database.plugins] == [arch]


def test_a_live_database_record_borrows_the_architecture_of_its_own_module(tmp_path: pathlib.Path) -> None:
    """Architecture is a fact about the file, so the record naming that file may have it.

    Live's database says nothing usable about bitness; the PE header of the very
    module it points at does.
    """
    database_dir = tmp_path / "Live Database"
    database_dir.mkdir()
    folder = tmp_path / "VstPlugins"
    folder.mkdir()
    module = write_pe(folder / "Thing.dll")
    write_database(
        database_dir / "Live-plugins-1.db",
        [("Thing", "device:vst:audiofx:1?n=Thing", module, "custom", False)],
    )
    database = build_plugin_db(database_dir=database_dir, vst_dirs=[folder], built=BUILT)
    (entry,) = database.plugins
    assert entry.source is PluginSource.LIVE_DATABASE
    assert entry.arch == "x64"


def test_a_probed_uid_file_and_a_bundle_both_contribute_class_ids(tmp_path: pathlib.Path) -> None:
    uid_db = write_uid_db(tmp_path / "probed.json", {"Massive X": [1, 2, 3, 4]})
    bundle = tmp_path / "vst3" / "Thing.vst3" / "Contents"
    bundle.mkdir(parents=True)
    (bundle / "moduleinfo.json").write_text(
        json.dumps({"Classes": [{"CID": "00" * 15 + "63", "Category": "Audio Module Class", "Name": "In Bundle"}]}),
        encoding="utf-8",
    )
    database = build_plugin_db(database_dir=None, vst_dirs=[tmp_path / "vst3"], uid_db=uid_db, built=BUILT)

    by_name = {entry.name: entry for entry in database.plugins}
    assert by_name["Massive X"].source is PluginSource.UID_DB
    assert by_name["Massive X"].uid_fields == (1, 2, 3, 4)
    assert by_name["In Bundle"].source is PluginSource.MODULEINFO
    assert by_name["In Bundle"].uid_fields == (0, 0, 0, 99)


def test_one_plugin_in_two_places_is_two_records(tmp_path: pathlib.Path) -> None:
    """Kept apart on purpose: a merged record could not report a disagreement."""
    uid_db = write_uid_db(tmp_path / "probed.json", {"Serum": [9, 9, 9, 9]})
    database = build_plugin_db(database_dir=live_database(tmp_path), vst_dirs=[], uid_db=uid_db, built=BUILT)

    serum = [entry for entry in database.plugins if entry.name == "Serum"]
    assert [entry.source for entry in serum] == [PluginSource.LIVE_DATABASE, PluginSource.UID_DB]
    # The collapsed view is what a consumer pairs names against.
    assert [entry.source for entry in database.installed(PluginKind.VST3)] == [PluginSource.LIVE_DATABASE]


def test_records_are_counted_per_format_and_source(tmp_path: pathlib.Path) -> None:
    folder = tmp_path / "VstPlugins"
    folder.mkdir()
    (folder / "Neverscanned.dll").write_bytes(b"")
    database = build_plugin_db(database_dir=live_database(tmp_path), vst_dirs=[folder], built=BUILT)

    counts = {(count.kind, count.source): count.count for count in database.counts()}
    assert counts[(PluginKind.VST, PluginSource.LIVE_DATABASE)] == 1
    assert counts[(PluginKind.VST3, PluginSource.LIVE_DATABASE)] == 1
    assert counts[(PluginKind.VST, PluginSource.FOLDER_SCAN)] == 1


# -- the file ---------------------------------------------------------------


def test_a_database_round_trips_through_json(tmp_path: pathlib.Path) -> None:
    database = make_db(
        PluginEntry(
            name="Serum",
            kind=PluginKind.VST3,
            source=PluginSource.LIVE_DATABASE,
            vendor="Xfer Records",
            uid_fields=SERUM_FIELDS,
            module_path=tmp_path / "Serum.vst3",
            arch="x64",
        ),
        PluginEntry(
            name="Serum_x64",
            kind=PluginKind.VST,
            source=PluginSource.LIVE_DATABASE,
            vendor="Xfer Records",
            unique_id=1483109208,
            module_path=tmp_path / "Serum_x64.dll",
            arch="x64",
        ),
    )
    path = write_plugin_db(database, tmp_path / "plugin_db.json")
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


def test_the_parent_directory_is_created_on_a_fresh_machine(tmp_path: pathlib.Path) -> None:
    path = write_plugin_db(make_db(), tmp_path / "fresh" / "config" / "plugin_db.json")
    assert path.exists()


def test_building_replaces_rather_than_merges(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A plugin uninstalled since the last run has to leave the database."""
    monkeypatch.setattr(plugin_db, "default_vst_dirs", lambda: [])
    monkeypatch.setattr(plugin_db, "default_live_database_dir", lambda: None)
    path = tmp_path / "plugin_db.json"
    write_plugin_db(make_db(PluginEntry(name="Gone", kind=PluginKind.VST3, source=PluginSource.LIVE_DATABASE)), path)
    rebuilt = create_or_update_db(AbletoolzConfig(), path)
    assert rebuilt.plugins == ()
    assert read_plugin_db(path).plugins == ()


def test_a_command_builds_in_memory_when_no_file_exists_yet(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing refuses to run because nobody has built the database yet."""
    folder = tmp_path / "VstPlugins"
    folder.mkdir()
    (folder / "Thing.dll").write_bytes(b"")
    monkeypatch.setattr(plugin_db, "default_vst_dirs", lambda: [folder])
    monkeypatch.setattr(plugin_db, "default_live_database_dir", lambda: None)

    path = tmp_path / "plugin_db.json"
    with caplog.at_level("INFO"):
        database = load_plugin_db(AbletoolzConfig(), path)
    assert [entry.name for entry in database.plugins] == ["Thing"]
    assert not path.exists()
    assert "--plugin-db" in caplog.text


def test_config_paths_are_scanned_alongside_the_standard_locations(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """plugin_database.paths does for plugins what sample_database.paths does for samples."""
    standard = tmp_path / "VstPlugins"
    standard.mkdir()
    (standard / "Standard.dll").write_bytes(b"")
    extra = tmp_path / "Elsewhere"
    extra.mkdir()
    (extra / "Extra.dll").write_bytes(b"")
    monkeypatch.setattr(plugin_db, "default_vst_dirs", lambda: [standard])
    monkeypatch.setattr(plugin_db, "default_live_database_dir", lambda: None)

    database = load_plugin_db(AbletoolzConfig(plugin_paths=[extra]), tmp_path / "plugin_db.json")
    assert sorted(entry.name for entry in database.plugins) == ["Extra", "Standard"]


# -- class ids out of the database ------------------------------------------


def test_sources_are_consulted_in_the_documented_order() -> None:
    database = make_db(
        PluginEntry(name="Thing", kind=PluginKind.VST3, source=PluginSource.LIVE_DATABASE, uid_fields=(1, 2, 3, 4)),
        PluginEntry(name="Thing", kind=PluginKind.VST3, source=PluginSource.UID_DB, uid_fields=(9, 9, 9, 9)),
        PluginEntry(name="Other", kind=PluginKind.VST3, source=PluginSource.MODULEINFO, uid_fields=(5, 6, 7, 8)),
    )
    lookup = database.uid_lookup()
    assert lookup.resolve("Thing") == (1, 2, 3, 4)
    assert lookup.resolve("Other") == (5, 6, 7, 8)
    assert lookup.resolve("Absent") is None
    assert lookup.names() == {"Thing", "Other"}


def test_a_caller_uid_beats_every_source_in_the_database() -> None:
    database = make_db(
        PluginEntry(name="Thing", kind=PluginKind.VST3, source=PluginSource.LIVE_DATABASE, uid_fields=(1, 2, 3, 4))
    )
    assert database.uid_lookup(extra={"Thing": (4, 4, 4, 4)}).resolve("Thing") == (4, 4, 4, 4)


def test_two_sources_claiming_different_class_ids_are_shouted_about(caplog: pytest.LogCaptureFixture) -> None:
    """A wrong class id does not fail, it loads another plugin. So it gets said out loud."""
    database = make_db(
        PluginEntry(name="Thing", kind=PluginKind.VST3, source=PluginSource.LIVE_DATABASE, uid_fields=OTHER_FIELDS),
        PluginEntry(name="Thing", kind=PluginKind.VST3, source=PluginSource.MODULEINFO, uid_fields=(9, 9, 9, 9)),
    )
    with caplog.at_level("ERROR"):
        lookup = database.uid_lookup()
    assert lookup.resolve("Thing") == OTHER_FIELDS
    assert "Thing" in caplog.text
    assert "load" in caplog.text


def test_a_vst2_record_contributes_no_class_id() -> None:
    database = make_db(PluginEntry(name="Thing", kind=PluginKind.VST, source=PluginSource.LIVE_DATABASE, unique_id=42))
    assert database.uid_lookup().names() == frozenset()


def test_a_build_orders_database_over_probed_file_over_moduleinfo(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """End to end: three sources, one disagreement, and Live's database wins."""
    uid_db = write_uid_db(tmp_path / "probed.json", {"Serum": [9, 9, 9, 9], "Only Probed": list(OTHER_FIELDS)})
    bundle = tmp_path / "vst3" / "Thing.vst3" / "Contents"
    bundle.mkdir(parents=True)
    (bundle / "moduleinfo.json").write_text(
        json.dumps({"Classes": [{"CID": "00" * 15 + "63", "Category": "Audio Module Class", "Name": "In Bundle"}]}),
        encoding="utf-8",
    )
    database = build_plugin_db(
        database_dir=live_database(tmp_path), vst_dirs=[tmp_path / "vst3"], uid_db=uid_db, built=BUILT
    )
    with caplog.at_level("ERROR"):
        lookup = database.uid_lookup()

    assert lookup.resolve("Serum") == SERUM_FIELDS
    assert lookup.resolve("Only Probed") == OTHER_FIELDS
    assert lookup.resolve("In Bundle") == (0, 0, 0, 99)
    assert "Serum" in caplog.text


def test_an_unreadable_class_id_row_is_skipped_rather_than_guessed(tmp_path: pathlib.Path) -> None:
    database_dir = tmp_path / "Live Database"
    database_dir.mkdir()
    write_database(
        database_dir / "Live-plugins-1.db",
        [
            ("Short", "device:vst3:audiofx:abc", installed(tmp_path, "Short.vst3"), "global", True),
            ("Good", f"device:vst3:audiofx:{OTHER_CID}", installed(tmp_path, "Good.vst3"), "global", True),
        ],
    )
    database = build_plugin_db(database_dir=database_dir, vst_dirs=[], built=BUILT)
    assert [entry.name for entry in database.plugins] == ["Good"]
