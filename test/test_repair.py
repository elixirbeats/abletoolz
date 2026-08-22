"""Repairing the plugin devices a machine can no longer load.

Hermetic. The Live plugin database is built here, row by row, in the schema
measured on Live 12.4.5b -- so the tests state what that schema means rather
than depending on whatever this machine happens to have installed. Plugin
folders are tmp_path, and the sets are the same version skeletons the rest of
the suite uses.
"""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
from xml.etree import ElementTree as ET

import pydantic
import pytest

from abletoolz.live_set import AbletonSet, plugins
from abletoolz.misc import get_element
from abletoolz.plugin_parsers import PluginKind, plugin_db, repair, uid_sources
from abletoolz.plugin_parsers import config as config_module
from abletoolz.plugin_parsers.config import AbletoolzConfig
from abletoolz.plugin_parsers.format_translation import (
    IncompleteDevice,
    NamedTarget,
    TranslationTarget,
    is_translatable,
    parse_config_targets,
    read_identity,
    read_uid_fields,
    resolve_target,
    translate_device,
    translate_set,
)
from abletoolz.plugin_parsers.mapping import name_variants, strip_bitness, suggest_target_name
from abletoolz.plugin_parsers.repair import DeviceRepair, RepairStatus, repair_set
from abletoolz.plugin_parsers.state import UNMEASURED, StateEvidence, StateRung, StateTransform
from abletoolz.plugin_parsers.uid_sources import (
    UidLookup,
    UidSource,
    harvest_live_database_uids,
    read_live_database,
    read_uid_db,
)

SKELETONS = pathlib.Path(__file__).parent / "version_fixtures" / "skeletons"
GENERATED = pathlib.Path(__file__).parent / "version_fixtures" / "generated"

# Live 12.4.5b, Live-plugins-1.db, transcribed exactly.
_SCHEMA = """
CREATE TABLE version (version INT, platform INT);
CREATE TABLE plugin_domains (id TEXT, module_id INTEGER DEFAULT 0, relpath TEXT, enabled INTEGER DEFAULT 0);
CREATE TABLE plugin_modules (
    module_id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT, arch INTEGER DEFAULT 0,
    processor INTEGER DEFAULT 0, scanstate INTEGER DEFAULT 0, fingerprint TEXT);
CREATE TABLE plugins (
    plugin_id INTEGER PRIMARY KEY AUTOINCREMENT, module_id INTEGER DEFAULT 0, dev_identifier TEXT,
    name TEXT, vendor TEXT, version TEXT, sdk_version TEXT, flags INTEGER DEFAULT 0,
    scanstate INTEGER DEFAULT 0, subcategories TEXT, enabled INTEGER DEFAULT 0);
"""

# Class ids as the database spells them, dashed. The first is Serum's, measured.
SERUM_CID = "56535458-6673-5873-6572-756d00000000"
SERUM_FIELDS = (1448301656, 1718835315, 1701999981, 0)
OTHER_CID = "0000000a-0000-000b-0000-000c0000000d"
OTHER_FIELDS = (10, 11, 12, 13)


def make_set(key: str) -> AbletonSet:
    ableton_set = AbletonSet(SKELETONS / f"{key}.als")
    assert ableton_set.parse()
    return ableton_set


def generated_set() -> AbletonSet:
    """The one fixture no version of Live wrote. See the section at the bottom."""
    ableton_set = AbletonSet(GENERATED / "set_generator_9_7_7.als")
    assert ableton_set.parse()
    return ableton_set


def write_database(
    path: pathlib.Path,
    rows: list[tuple[str, str, pathlib.Path, str, bool]],
    *,
    module_scanstate: int = 1,
) -> pathlib.Path:
    """A Live plugin database holding ``(name, dev_identifier, module, domain, enabled)``."""
    connection = sqlite3.connect(path)
    connection.executescript(_SCHEMA)
    connection.execute("INSERT INTO version VALUES (1, 1)")
    for index, (name, dev_identifier, module, domain, enabled) in enumerate(rows, start=1):
        connection.execute(
            "INSERT INTO plugin_modules (module_id, path, arch, processor, scanstate, fingerprint)"
            " VALUES (?, ?, 3, 1, ?, '')",
            (index, str(module), module_scanstate),
        )
        connection.execute(
            "INSERT INTO plugin_domains (id, module_id, relpath, enabled) VALUES (?, ?, '', ?)",
            (domain, index, int(enabled)),
        )
        # scanstate and enabled are 1 on every row of the real database.
        connection.execute(
            "INSERT INTO plugins (module_id, dev_identifier, name, vendor, version, sdk_version,"
            " flags, scanstate, subcategories, enabled) VALUES (?, ?, ?, 'Vendor', '1', '1', 0, 1, '', 1)",
            (index, dev_identifier, name),
        )
    connection.commit()
    connection.close()
    return path


def installed(tmp_path: pathlib.Path, name: str) -> pathlib.Path:
    """A plugin module file that exists, so the database's path check passes."""
    module = tmp_path / name
    module.write_bytes(b"")
    return module


# -- reading the database ---------------------------------------------------


def test_database_rows_are_read_into_typed_plugins(tmp_path: pathlib.Path) -> None:
    database = write_database(
        tmp_path / "Live-plugins-1.db",
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
    by_name = {plugin.name: plugin for plugin in read_live_database(database)}
    assert by_name["Serum"].kind is PluginKind.VST3
    assert by_name["Serum"].uid_fields == SERUM_FIELDS
    assert by_name["Serum"].unique_id is None
    assert by_name["Serum"].loadable
    assert by_name["Serum_x64"].kind is PluginKind.VST
    assert by_name["Serum_x64"].unique_id == 1483109208
    assert by_name["Serum_x64"].uid_fields is None
    # The measured state of this machine: the custom VST2 domain is switched off.
    assert by_name["Serum_x64"].domain == "custom"
    assert not by_name["Serum_x64"].loadable


def test_a_disabled_domain_makes_a_present_plugin_unloadable(tmp_path: pathlib.Path) -> None:
    """The measured cause: the .dll is there, Live's custom folder is off."""
    module = installed(tmp_path, "Thing.dll")
    database = write_database(
        tmp_path / "Live-plugins-1.db",
        [("Thing", "device:vst:audiofx:1?n=Thing", module, "custom", False)],
    )
    (plugin,) = read_live_database(database)
    assert module.exists()
    assert not plugin.loadable


def test_a_missing_module_file_makes_an_enabled_plugin_unloadable(tmp_path: pathlib.Path) -> None:
    database = write_database(
        tmp_path / "Live-plugins-1.db",
        [("Gone", f"device:vst3:audiofx:{OTHER_CID}", tmp_path / "Gone.vst3", "global", True)],
    )
    (plugin,) = read_live_database(database)
    assert plugin.domain_enabled
    assert not plugin.loadable


def test_a_database_without_plugin_tables_answers_nothing(tmp_path: pathlib.Path) -> None:
    """Live's file database sits in the same folder and has none of these tables."""
    other = tmp_path / "Live-files-12300.db"
    connection = sqlite3.connect(other)
    connection.execute("CREATE TABLE files (id INT)")
    connection.commit()
    connection.close()
    assert read_live_database(other) == ()


def test_only_the_newest_usable_database_is_read(tmp_path: pathlib.Path) -> None:
    """Measured: a Live 11 database sits beside the Live 12 one and contradicts it.

    Live 11's combined files-and-plugins database has the custom VST2 folder
    enabled, Live 12's has it disabled. Merging the two would answer for the Live
    the user stopped running, so only the newest file that has the tables counts.
    """
    module = installed(tmp_path, "Thing.dll")
    old = write_database(
        tmp_path / "Live-files-53.db",
        [("Thing", "device:vst:audiofx:1?n=Thing", module, "custom", True)],
    )
    new = write_database(
        tmp_path / "Live-plugins-1.db",
        [("Thing", "device:vst:audiofx:1?n=Thing", module, "custom", False)],
    )
    os.utime(old, (1, 1))
    os.utime(new, (2, 2))

    (plugin,) = uid_sources.read_live_databases(tmp_path)
    assert not plugin.loadable
    # The older file on its own still says what it says; it is simply not asked.
    (from_old,) = read_live_database(old)
    assert from_old.loadable


def test_harvest_takes_vst3_class_ids_only(tmp_path: pathlib.Path) -> None:
    database = write_database(
        tmp_path / "Live-plugins-1.db",
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
    assert harvest_live_database_uids(database) == {"Serum": SERUM_FIELDS}


def test_unreadable_dev_identifiers_are_skipped(tmp_path: pathlib.Path) -> None:
    database = write_database(
        tmp_path / "Live-plugins-1.db",
        [
            ("Short", "device:vst3:audiofx:abc", installed(tmp_path, "Short.vst3"), "global", True),
            ("Odd", "something else entirely", installed(tmp_path, "Odd.vst3"), "global", True),
            ("Good", f"device:vst3:audiofx:{OTHER_CID}", installed(tmp_path, "Good.vst3"), "global", True),
        ],
    )
    assert harvest_live_database_uids(database) == {"Good": OTHER_FIELDS}


# -- the probed class id file -----------------------------------------------


def write_uid_db(path: pathlib.Path, entries: dict[str, list[int]]) -> pathlib.Path:
    """The probe's own JSON shape, extra keys and all."""
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


def test_uid_db_keeps_only_the_fields(tmp_path: pathlib.Path) -> None:
    path = write_uid_db(tmp_path / "vst3_cid_db.json", {"Thing": [1, 2, 3, 4]})
    assert read_uid_db(path) == {"Thing": (1, 2, 3, 4)}


def test_uid_db_rejects_a_malformed_entry(tmp_path: pathlib.Path) -> None:
    """Three fields is not a class id, and pretending otherwise loads the wrong plugin."""
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"Thing": {"fields": [1, 2, 3]}}), encoding="utf-8")
    with pytest.raises(pydantic.ValidationError):
        read_uid_db(path)


# -- resolution order -------------------------------------------------------


def test_sources_are_consulted_highest_priority_first() -> None:
    lookup = UidLookup(
        (
            UidSource("live database", {"Thing": (1, 2, 3, 4)}),
            UidSource("uid db", {"Thing": (9, 9, 9, 9), "Other": (5, 6, 7, 8)}),
        )
    )
    assert lookup.resolve("Thing") == (1, 2, 3, 4)
    assert lookup.resolve("Other") == (5, 6, 7, 8)
    assert lookup.resolve("Absent") is None
    assert lookup.names() == {"Thing", "Other"}


def test_disagreeing_sources_are_reported_by_name() -> None:
    lookup = UidLookup(
        (
            UidSource("live database", {"Thing": (1, 2, 3, 4), "Same": (7, 7, 7, 7)}),
            UidSource("uid db", {"Thing": (9, 9, 9, 9), "Same": (7, 7, 7, 7)}),
        )
    )
    (disagreement,) = lookup.disagreements()
    assert disagreement.name == "Thing"
    assert disagreement.winner == "live database"
    assert disagreement.winning_fields == (1, 2, 3, 4)
    assert disagreement.loser == "uid db"
    assert disagreement.losing_fields == (9, 9, 9, 9)
    assert "Thing" in str(disagreement)


# Assembling that order out of every source on a machine belongs to the local
# plugin database; see test_plugin_db.


# -- config entries ---------------------------------------------------------


def test_a_target_without_a_uid_is_left_to_be_looked_up() -> None:
    parsed = parse_config_targets({"Serum_x64": {"name": "Serum"}})
    assert parsed == {"Serum_x64": NamedTarget(PluginKind.VST3, "Serum", StateTransform.VERBATIM)}


def test_a_target_with_a_uid_still_parses_whole() -> None:
    parsed = parse_config_targets({"Old.dll": {"name": "New", "uid": [1, 2, 3, 4]}})
    assert parsed == {"Old.dll": TranslationTarget(PluginKind.VST3, "New", (1, 2, 3, 4))}


def test_a_kilohearts_source_defaults_to_the_kilohearts_state_transform() -> None:
    """Measured vendor rule: every kHs VST3 wraps the payload its VST2 stores raw."""
    parsed = parse_config_targets({"kHs Stereo": {"name": "kHs Stereo"}})
    assert parsed["kHs Stereo"] == NamedTarget(PluginKind.VST3, "kHs Stereo", StateTransform.KILOHEARTS)


def test_an_explicit_state_overrides_the_kilohearts_default() -> None:
    parsed = parse_config_targets({"kHs Stereo": {"name": "kHs Stereo", "state": "verbatim"}})
    assert parsed["kHs Stereo"] == NamedTarget(PluginKind.VST3, "kHs Stereo", StateTransform.VERBATIM)


def test_the_kilohearts_default_follows_the_source_name_not_the_target() -> None:
    parsed = parse_config_targets({"Some Other": {"name": "kHs Looking Name"}})
    assert parsed["Some Other"] == NamedTarget(PluginKind.VST3, "kHs Looking Name", StateTransform.VERBATIM)


def test_a_named_target_resolves_through_the_lookup() -> None:
    lookup = UidLookup((UidSource("test", {"Serum": SERUM_FIELDS}),))
    named = NamedTarget(PluginKind.VST3, "Serum", StateTransform.KILOHEARTS)
    assert resolve_target(named, lookup) == TranslationTarget(
        PluginKind.VST3, "Serum", SERUM_FIELDS, StateTransform.KILOHEARTS
    )
    assert resolve_target(named, UidLookup()) is None


def test_uid_db_path_comes_out_of_config(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "plugin_translation:\n  uid_db: probed.json\n  targets:\n    'kHs Thing':\n      name: kHs Thing\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "get_config_path", lambda: path)
    loaded = config_module.load_config()
    assert loaded.plugin_translation_uid_db == pathlib.Path("probed.json")
    assert loaded.plugin_translation_targets["kHs Thing"] == NamedTarget(
        PluginKind.VST3, "kHs Thing", StateTransform.KILOHEARTS
    )


# -- suggesting a mapping ---------------------------------------------------


@pytest.mark.parametrize(
    ("name", "stripped"),
    [
        ("Serum_x64", "Serum"),
        ("Ohmicide_vstwin.64", "Ohmicide_vstwin"),
        ("Thing x64", "Thing"),
        ("Thing (x64)", "Thing"),
        ("Thing-64bit", "Thing"),
        ("Thing_x64.64", "Thing"),
        ("Thing", "Thing"),
    ],
)
def test_bitness_markers_are_stripped(name: str, stripped: str) -> None:
    assert strip_bitness(name) == stripped


def test_variants_cover_bitness_and_a_vendor_first_word() -> None:
    assert name_variants("FabFilter Pro-Q 3") == ("FabFilter Pro-Q 3", "Pro-Q 3")
    assert name_variants("Serum_x64") == ("Serum_x64", "Serum")


def test_a_suffix_variant_is_suggested() -> None:
    assert suggest_target_name("Serum_x64", ["Serum", "Massive"]) == "Serum"


def test_an_exact_name_is_suggested() -> None:
    assert suggest_target_name("kHs Stereo", ["kHs Stereo"]) == "kHs Stereo"


def test_nothing_is_suggested_when_nothing_looks_close() -> None:
    assert suggest_target_name("Effectrix", ["Serum", "Pro-Q 3"]) is None


def test_a_false_friend_is_only_ever_a_suggestion() -> None:
    """Midnight Compressor reduces to Compressor, a real and unrelated kHs plugin.

    The near miss is worth showing the user and must never be acted on.
    """
    assert suggest_target_name("Midnight Compressor", ["Compressor"]) == "Compressor"

    live_set = make_set("11.3.42")
    info = vst2_info(live_set, "Effectrix")
    get_element(info, "PlugName").set("Value", "Midnight Compressor")
    before = ET.tostring(info)
    report = repair_set(
        live_set,
        targets={},
        uid_lookup=UidLookup((UidSource("test", {"Compressor": (1, 2, 3, 4)}),)),
        loadable=nothing_loads,
    )
    action = only(report, "Midnight Compressor")
    assert action.status is RepairStatus.BROKEN_UNMAPPED
    assert action.suggestion == (
        '"Midnight Compressor": {to: vst3, name: "Compressor"}'
        "  # state: unknown -- experiment, audition before trusting"
    )
    assert ET.tostring(info) == before
    assert info.tag == "VstPluginInfo"


# -- the oracle -------------------------------------------------------------


def vst2_info(live_set: AbletonSet, plug_name: str) -> ET.Element:
    for info in live_set.root.iter("VstPluginInfo"):
        if get_element(info, "PlugName", attribute="Value") == plug_name:
            return info
    raise AssertionError(f"no VST2 device named {plug_name}")


def nothing_loads(_info: ET.Element) -> bool:
    return False


def everything_loads(_info: ET.Element) -> bool:
    return True


def only(report: repair.RepairReport, source_name: str) -> DeviceRepair:
    (action,) = [entry for entry in report.actions if entry.source_name == source_name]
    return action


def test_the_database_oracle_answers_per_format_and_name(tmp_path: pathlib.Path) -> None:
    database = write_database(
        tmp_path / "Live-plugins-1.db",
        [
            (
                "Serum_x64",
                "device:vst:instr:1483109208?n=Serum_x64",
                installed(tmp_path, "Serum_x64.dll"),
                "custom",
                True,
            ),
            (
                "Effectrix",
                "device:vst:audiofx:1935828326?n=Effectrix",
                installed(tmp_path, "Effectrix.dll"),
                "custom",
                False,
            ),
        ],
    )
    oracle = repair.database_oracle(read_live_database(database))
    live_set = make_set("11.3.42")
    assert oracle(vst2_info(live_set, "Serum_x64"))
    assert not oracle(vst2_info(live_set, "Effectrix"))


def test_the_disk_oracle_believes_a_stored_path_that_is_still_there(tmp_path: pathlib.Path) -> None:
    live_set = make_set("11.3.42")
    info = vst2_info(live_set, "Serum_x64")
    real = installed(tmp_path, "Serum_x64.dll")
    get_element(info, "Path").set("Value", real.as_posix())
    oracle = repair.disk_oracle(live_set, vst_dirs=[tmp_path])
    assert oracle(info)


def test_the_disk_oracle_calls_a_dead_path_broken(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugins, "default_vst_dirs", lambda: [tmp_path])
    live_set = make_set("11.3.42")
    info = vst2_info(live_set, "Serum_x64")
    get_element(info, "Path").set("Value", (tmp_path / "not there.dll").as_posix())
    oracle = repair.disk_oracle(live_set, vst_dirs=[tmp_path])
    assert not oracle(info)


def test_the_default_oracle_falls_back_to_disk_without_a_database(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plugins, "default_vst_dirs", lambda: [tmp_path])
    live_set = make_set("11.3.42")
    oracle = repair.default_oracle(live_set, database_dir=tmp_path / "no database here", vst_dirs=[tmp_path])
    assert not oracle(vst2_info(live_set, "Serum_x64"))


def test_the_default_oracle_prefers_the_database(tmp_path: pathlib.Path) -> None:
    database_dir = tmp_path / "db"
    database_dir.mkdir()
    write_database(
        database_dir / "Live-plugins-1.db",
        [
            (
                "Serum_x64",
                "device:vst:instr:1483109208?n=Serum_x64",
                installed(tmp_path, "Serum_x64.dll"),
                "custom",
                True,
            )
        ],
    )
    live_set = make_set("11.3.42")
    oracle = repair.default_oracle(live_set, database_dir=database_dir, vst_dirs=[])
    # Nothing is on disk where the set says, so only the database can say yes.
    assert oracle(vst2_info(live_set, "Serum_x64"))
    assert not oracle(vst2_info(live_set, "Effectrix"))


# -- the status matrix ------------------------------------------------------


def test_a_broken_mapped_device_is_fixed() -> None:
    live_set = make_set("11.3.42")
    report = repair_set(live_set, uid_lookup=UidLookup(), loadable=nothing_loads)
    action = only(report, "Serum_x64")
    assert action.status is RepairStatus.FIXED
    assert action.target_name == "Serum"
    assert action.source_format is PluginKind.VST
    assert action.track
    assert report.fixed_count == 1
    assert vst3_names(live_set) >= {"Serum"}


def test_a_broken_unmapped_device_is_reported_and_left_alone() -> None:
    live_set = make_set("11.3.42")
    info = vst2_info(live_set, "Effectrix")
    before = ET.tostring(info)
    report = repair_set(live_set, uid_lookup=UidLookup(), loadable=nothing_loads)
    action = only(report, "Effectrix")
    assert action.status is RepairStatus.BROKEN_UNMAPPED
    assert action.target_name is None
    assert action.target_format is None
    assert ET.tostring(info) == before


def test_a_device_already_in_another_format_is_judged_too() -> None:
    """Direction comes from the table, so every readable device is a possible source.

    The fixture's VST3 device is unmapped and unloadable here, which is the same
    thing to say about it as about an unmapped VST2 -- and would have been
    invisible when the command chose one direction for the whole run.
    """
    live_set = make_set("11.3.42")
    report = repair_set(live_set, uid_lookup=UidLookup(), loadable=nothing_loads)
    action = only(report, "Decapitator")
    assert action.source_format is PluginKind.VST3
    assert action.status is RepairStatus.BROKEN_UNMAPPED


def test_a_mapping_with_no_findable_class_id_is_reported_not_guessed() -> None:
    live_set = make_set("11.3.42")
    info = vst2_info(live_set, "Effectrix")
    before = ET.tostring(info)
    report = repair_set(
        live_set,
        targets={"Effectrix": NamedTarget(PluginKind.VST3, "Effectrix")},
        uid_lookup=UidLookup(),
        loadable=nothing_loads,
    )
    action = only(report, "Effectrix")
    assert action.status is RepairStatus.BROKEN_NO_UID
    assert action.target_name == "Effectrix"
    assert report.broken_no_uid_count == 1
    assert ET.tostring(info) == before
    assert info.tag == "VstPluginInfo"


def test_a_named_mapping_is_fixed_once_a_source_knows_the_class_id() -> None:
    live_set = make_set("11.3.42")
    lookup = UidLookup((UidSource("test", {"Effectrix": OTHER_FIELDS}),))
    report = repair_set(
        live_set,
        targets={"Effectrix": NamedTarget(PluginKind.VST3, "Effectrix")},
        uid_lookup=lookup,
        loadable=nothing_loads,
    )
    assert only(report, "Effectrix").status is RepairStatus.FIXED
    info = [i for i in live_set.root.iter("Vst3PluginInfo") if get_element(i, "Name", attribute="Value") == "Effectrix"]
    assert len(info) == 1


def test_a_loadable_mapped_device_is_left_untouched() -> None:
    """Repair is not translation: a working device is never converted."""
    live_set = make_set("11.3.42")
    before = ET.tostring(live_set.root)
    report = repair_set(live_set, uid_lookup=UidLookup(), loadable=everything_loads)
    action = only(report, "Serum_x64")
    assert action.status is RepairStatus.MAPPED_NOT_BROKEN
    assert action.target_name == "Serum"
    assert report.fixed_count == 0
    assert report.mapped_not_broken_count == 1
    assert ET.tostring(live_set.root) == before


def test_a_loadable_unmapped_device_is_simply_ok() -> None:
    live_set = make_set("11.3.42")
    report = repair_set(live_set, uid_lookup=UidLookup(), loadable=everything_loads)
    action = only(report, "Effectrix")
    assert action.status is RepairStatus.OK
    assert action.suggestion is None
    assert action.target_name is None


def test_every_status_is_reachable_in_one_pass() -> None:
    """Every per-device status, in one pass over one set.

    ``SET_TOO_OLD_FOR_TARGET`` is the one that cannot join them: it is a fact
    about the document, so it is either every device's answer or none of them.
    ``test_a_vst3_target_is_refused_on_a_set_older_than_vst3`` covers it, and the
    exhaustiveness check below stays honest by naming it as the exception.
    """
    live_set = make_set("10.1.3")
    loadable_names = {"Texture", "SPAN"}
    # A device only ever written down as a stub.
    make_stub(vst2_info(live_set, "midiChordAnalyzer-64"))

    def loadable(info: ET.Element) -> bool:
        return read_identity(info).name in loadable_names

    report = repair_set(
        live_set,
        targets={
            "Texture": NamedTarget(PluginKind.VST3, "Texture"),
            "Wolfram": TranslationTarget(PluginKind.VST3, "Wolfram", OTHER_FIELDS),
            "iZotope Trash 2": NamedTarget(PluginKind.VST3, "Trash 2"),
            "midiChordAnalyzer-64": TranslationTarget(PluginKind.VST3, "Chords", OTHER_FIELDS),
            # A pair with no translator: the user said it, abletoolz cannot do it.
            "Ohmicide_vstwin.64": TranslationTarget(PluginKind.AU, "Ohmicide", OTHER_FIELDS),
        },
        uid_lookup=UidLookup((UidSource("test", {"Texture": OTHER_FIELDS, "Pro-R": SERUM_FIELDS}),)),
        loadable=loadable,
    )
    statuses = {action.source_name: action.status for action in report.actions}
    assert statuses["Wolfram"] is RepairStatus.FIXED
    assert statuses["iZotope Trash 2"] is RepairStatus.BROKEN_NO_UID
    assert statuses["midiChordAnalyzer-64"] is RepairStatus.INCOMPLETE_DEVICE
    assert statuses["Ohmicide_vstwin.64"] is RepairStatus.UNSUPPORTED_PAIR
    assert statuses["Texture"] is RepairStatus.MAPPED_NOT_BROKEN
    assert statuses["SPAN"] is RepairStatus.OK
    assert set(statuses.values()) == set(RepairStatus) - {RepairStatus.SET_TOO_OLD_FOR_TARGET}
    # A VST3 device whose name a class id source knows becomes a suggestion too.
    assert any(line.startswith('"Pro-R": {to: vst3, name: "Pro-R"}') for line in report.suggestions)


def test_only_names_a_source_knows_are_suggested() -> None:
    """Nine unmapped devices here, one of which a class id source has heard of."""
    live_set = make_set("10.1.3")
    lookup = UidLookup((UidSource("test", {"Ohmicide_vstwin": SERUM_FIELDS}),))
    report = repair_set(live_set, targets={}, uid_lookup=lookup, loadable=nothing_loads)
    assert report.suggestions == (
        '"Ohmicide_vstwin.64": {to: vst3, name: "Ohmicide_vstwin"}'
        "  # state: unknown -- experiment, audition before trusting",
    )


def test_a_pair_with_no_translator_is_reported_rather_than_refused() -> None:
    """The entry decides the direction, and (vst, au) is a direction nobody wrote yet.

    Not an error and not silence: the user said what the device should become,
    and the honest answer is that abletoolz cannot do that one.
    """
    live_set = make_set("11.3.42")
    info = vst2_info(live_set, "Effectrix")
    before = ET.tostring(info)
    au_target = TranslationTarget(PluginKind.AU, "Test AU", (1, 2, 3, 4))
    report = repair_set(live_set, targets={"Effectrix": au_target}, uid_lookup=UidLookup(), loadable=nothing_loads)
    action = only(report, "Effectrix")
    assert action.status is RepairStatus.UNSUPPORTED_PAIR
    assert action.target_format is PluginKind.AU
    assert action.target_name == "Test AU"
    assert report.unsupported_pair_count == 1
    assert ET.tostring(info) == before
    assert info.tag == "VstPluginInfo"


def test_the_entry_alone_says_which_way_each_device_goes() -> None:
    """Two devices, two entries, two different target formats, one call."""
    live_set = make_set("11.3.42")
    report = repair_set(
        live_set,
        targets={
            "Serum_x64": TranslationTarget(PluginKind.VST3, "Serum", SERUM_FIELDS),
            "Effectrix": TranslationTarget(PluginKind.AU, "Effectrix AU", OTHER_FIELDS),
        },
        uid_lookup=UidLookup(),
        loadable=nothing_loads,
    )
    assert only(report, "Serum_x64").status is RepairStatus.FIXED
    assert only(report, "Serum_x64").target_format is PluginKind.VST3
    assert only(report, "Effectrix").status is RepairStatus.UNSUPPORTED_PAIR
    assert only(report, "Effectrix").target_format is PluginKind.AU


def test_repairing_twice_changes_nothing_the_second_time() -> None:
    live_set = make_set("11.3.42")
    repair_set(live_set, uid_lookup=UidLookup(), loadable=nothing_loads)
    settled = ET.tostring(live_set.root)
    report = repair_set(live_set, uid_lookup=UidLookup(), loadable=nothing_loads)
    assert report.fixed_count == 0
    assert ET.tostring(live_set.root) == settled


# -- devices Live only half wrote down --------------------------------------


def make_stub(info: ET.Element) -> None:
    """Reduce a device to the stub Live writes for a plugin it never loaded.

    Measured over 816 real sets: 54 devices in 11 of them carry exactly Dir,
    FileName, PlugName, UniqueId and sometimes Preset -- Live 9.7.7 in ten of
    those sets and one Live 10.1.3 set.
    """
    keep = {"Dir", "FileName", "PlugName", "UniqueId"}
    info[:] = [child for child in info if child.tag in keep]


def test_a_stub_device_is_still_read_by_name() -> None:
    """The name is all a stub has, and it is the half repair needs to report it."""
    live_set = make_set("11.3.42")
    info = vst2_info(live_set, "Serum_x64")
    make_stub(info)
    identity = read_identity(info)
    assert identity.name == "Serum_x64"
    assert identity.is_instrument is None


def test_a_stub_device_is_reported_rather_than_translated() -> None:
    """One of these used to take the whole set down with it."""
    live_set = make_set("11.3.42")
    info = vst2_info(live_set, "Serum_x64")
    make_stub(info)
    before = ET.tostring(info)

    report = repair_set(live_set, uid_lookup=UidLookup(), loadable=nothing_loads)
    action = only(report, "Serum_x64")
    assert action.status is RepairStatus.INCOMPLETE_DEVICE
    assert action.target_name == "Serum"
    assert report.incomplete_device_count == 1
    assert report.fixed_count == 0
    assert ET.tostring(info) == before
    assert info.tag == "VstPluginInfo"


def test_a_stub_device_does_not_stop_the_devices_around_it() -> None:
    live_set = make_set("10.1.3")
    make_stub(vst2_info(live_set, "Wolfram"))
    report = repair_set(
        live_set,
        targets={
            "Wolfram": TranslationTarget(PluginKind.VST3, "Wolfram", OTHER_FIELDS),
            "Texture": TranslationTarget(PluginKind.VST3, "Texture", SERUM_FIELDS),
        },
        uid_lookup=UidLookup(),
        loadable=nothing_loads,
    )
    assert only(report, "Wolfram").status is RepairStatus.INCOMPLETE_DEVICE
    assert only(report, "Texture").status is RepairStatus.FIXED


def test_translating_a_whole_set_leaves_a_stub_alone_too() -> None:
    live_set = make_set("11.3.42")
    info = vst2_info(live_set, "Serum_x64")
    make_stub(info)
    before = ET.tostring(info)
    report = translate_set(live_set, uid_lookup=UidLookup())
    assert ("Serum_x64" in [name for _track, name in report.unresolved]) is True
    assert ET.tostring(info) == before


def test_translating_a_stub_on_purpose_refuses_loudly() -> None:
    """Nothing reaches this, and if something ever does it must not guess a DeviceType."""
    live_set = make_set("11.3.42")
    info = vst2_info(live_set, "Serum_x64")
    make_stub(info)
    assert not is_translatable(info)
    with pytest.raises(IncompleteDevice):
        translate_device(info, TranslationTarget(PluginKind.VST3, "Serum", SERUM_FIELDS))


# -- what a repaired device says about its patch ----------------------------


def test_a_repaired_device_carries_what_is_known_about_its_patch() -> None:
    """Serum's rung is measured by ear, and the repaired device says so."""
    live_set = make_set("11.3.42")
    report = repair_set(live_set, uid_lookup=UidLookup(), loadable=nothing_loads)
    action = only(report, "Serum_x64")
    assert action.state is not None
    assert action.state.rung is StateRung.VERBATIM
    assert StateEvidence.EAR in action.state.evidence
    assert action.state.predictable
    assert report.fixed_measured_count == 1
    assert report.fixed_experimental_count == 0


def test_a_repaired_device_nobody_has_heard_is_counted_apart() -> None:
    """The number that tells the user how much auditioning is left."""
    live_set = make_set("11.3.42")
    report = repair_set(
        live_set,
        targets={"Effectrix": TranslationTarget(PluginKind.VST3, "Effectrix", OTHER_FIELDS)},
        uid_lookup=UidLookup(),
        loadable=nothing_loads,
    )
    action = only(report, "Effectrix")
    assert action.status is RepairStatus.FIXED
    assert action.state == UNMEASURED
    # Serum is measured, Effectrix is not, and the summary keeps them apart.
    assert report.fixed_count == 2
    assert report.fixed_measured_count == 1
    assert report.fixed_experimental_count == 1


def test_a_device_left_alone_claims_nothing_about_its_patch() -> None:
    live_set = make_set("11.3.42")
    report = repair_set(live_set, uid_lookup=UidLookup(), loadable=everything_loads)
    assert only(report, "Serum_x64").state is None
    assert report.fixed_measured_count == 0
    assert report.fixed_experimental_count == 0


def vst3_names(live_set: AbletonSet) -> set[str]:
    return {get_element(info, "Name", attribute="Value") for info in live_set.root.iter("Vst3PluginInfo")}


# -- the domain surface -----------------------------------------------------


@pytest.fixture
def hermetic(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """No real plugin dirs, no real Live database, no real user config or plugin db."""
    monkeypatch.setattr(plugins, "default_vst_dirs", lambda: [])
    monkeypatch.setattr(plugins, "default_live_database_dir", lambda: None)
    monkeypatch.setattr(plugin_db, "default_vst_dirs", lambda: [])
    monkeypatch.setattr(plugin_db, "default_live_database_dir", lambda: None)
    monkeypatch.setattr(plugin_db, "DEFAULT_PLUGIN_DB_PATH", tmp_path / "no plugin db here.json")
    monkeypatch.setattr(repair, "default_live_database_dir", lambda: None)
    monkeypatch.setattr(plugins, "load_config", lambda: AbletoolzConfig())


def test_plugins_repair_merges_config_and_caller_targets(hermetic: None, monkeypatch: pytest.MonkeyPatch) -> None:
    config = AbletoolzConfig(
        plugin_translation_targets={"Effectrix": TranslationTarget(PluginKind.VST3, "From Config", OTHER_FIELDS)}
    )
    monkeypatch.setattr(plugins, "load_config", lambda: config)
    live_set = make_set("11.3.42")
    report = live_set.plugins.repair()
    assert only(report, "Effectrix").target_name == "From Config"
    assert only(report, "Serum_x64").target_name == "Serum"
    assert report.fixed_count == 2


def test_plugins_repair_lets_the_caller_win_over_config(hermetic: None, monkeypatch: pytest.MonkeyPatch) -> None:
    config = AbletoolzConfig(
        plugin_translation_targets={"Serum_x64": TranslationTarget(PluginKind.VST3, "From Config", OTHER_FIELDS)}
    )
    monkeypatch.setattr(plugins, "load_config", lambda: config)
    live_set = make_set("11.3.42")
    explicit = TranslationTarget(PluginKind.VST3, "Explicit", OTHER_FIELDS)
    report = live_set.plugins.repair(targets={"Serum_x64": explicit})
    assert only(report, "Serum_x64").target_name == "Explicit"


def test_plugins_repair_resolves_a_named_config_target_through_the_live_database(
    hermetic: None, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: config says which VST3, the machine says which class id."""
    database_dir = tmp_path / "db"
    database_dir.mkdir()
    write_database(
        database_dir / "Live-plugins-1.db",
        [("Serum", f"device:vst3:instr:{SERUM_CID}", installed(tmp_path, "Serum.vst3"), "global", True)],
    )
    monkeypatch.setattr(plugin_db, "default_live_database_dir", lambda: database_dir)
    config = AbletoolzConfig(plugin_translation_targets={"Serum_x64": NamedTarget(PluginKind.VST3, "Serum")})
    monkeypatch.setattr(plugins, "load_config", lambda: config)

    live_set = make_set("11.3.42")
    report = live_set.plugins.repair()
    assert only(report, "Serum_x64").status is RepairStatus.FIXED
    (info,) = [i for i in live_set.root.iter("Vst3PluginInfo") if get_element(i, "Name", attribute="Value") == "Serum"]
    uid = info.find("Uid")
    assert uid is not None
    assert read_uid_fields(uid) == SERUM_FIELDS


# -- a set another tool generated -------------------------------------------
#
# `set_generator_9_7_7.als` is not a Live fixture and is deliberately kept out
# of the version matrix. It declares Creator "Ableton Live 9.7.7" and Live never
# touched it: it is indented with four spaces where Live uses tabs, it pastes
# tab-indented Live fragments inside its own output, it omits SchemaChangeCount,
# and it carries markup Live only started writing in 10.0. Every stub device,
# every dateless reference and every incomplete track in the library sits in a
# set with that fingerprint -- 23 of 811, all from the same generator.
#
# Tolerating them is in scope: abletoolz generates sets too, and reading one
# tool's output is what the whole library does. What these tests may not do is
# say anything about Live 9.7.7, which this file cannot testify to.


def test_a_generated_stub_is_shaped_like_the_synthetic_one() -> None:
    """``make_stub`` asserts a shape; this is that shape off a real file.

    Everything above builds stubs by stripping a full device, which proves the
    handling and not the premise. The generated fixture carries a device its
    writer only half described, with exactly the four children make_stub keeps
    -- so the synthetic helper is measuring the right thing.
    """
    live_set = generated_set()
    info = vst2_info(live_set, "Phase Plant")
    assert [child.tag for child in info] == ["Dir", "FileName", "PlugName", "UniqueId"]
    assert read_identity(info).name == "Phase Plant"
    assert read_identity(info).is_instrument is None  # no Category, so the set never says
    assert not is_translatable(info)


def test_a_generated_stub_device_is_reported_rather_than_translated() -> None:
    live_set = generated_set()
    info = vst2_info(live_set, "Phase Plant")
    before = ET.tostring(info)
    report = repair_set(
        live_set,
        targets={"Phase Plant": TranslationTarget(PluginKind.VST3, "Phase Plant", OTHER_FIELDS)},
        uid_lookup=UidLookup(),
        loadable=nothing_loads,
    )
    assert only(report, "Phase Plant").status is RepairStatus.INCOMPLETE_DEVICE
    assert report.fixed_count == 0
    assert ET.tostring(info) == before


def test_scanning_a_set_with_a_stub_device_reports_it_instead_of_crashing() -> None:
    """A stub's Dir holds the plugin's file name and no directory.

    Regression: parse_vst_element handed that to path_separator_type, which
    raises when a path has no separator, so --check-plugins died on every set of
    this shape instead of listing the devices it could read.
    """
    live_set = generated_set()
    refs = live_set.plugins.scan([])  # raised ValueError before the fix
    (stub,) = [ref for ref in refs if ref.name == "Phase Plant.dll"]
    assert stub.kind is PluginKind.VST
    assert stub.path is None  # no directory was stored, so none is invented
    assert not stub.exists
    assert stub.track_location.endswith("Reese Sub*")


# -- a target format the set's own schema never declared --------------------


def test_a_vst3_target_is_refused_on_a_set_older_than_vst3() -> None:
    """Live rejects the whole file, so the refusal has to come before the write.

    Measured 2026-08-13: Live 12.4.5b refused a repaired Live 9.0.1 set with
    "Unknown class 'Vst3PluginInfo' encountered". Nothing about the class id or
    the patch was wrong -- the document's schema has no such class, so the
    device could not be written into it at any quality.
    """
    live_set = make_set("9.0.1")
    before = [ET.tostring(info) for info in live_set.root.iter("VstPluginInfo")]
    report = repair_set(
        live_set,
        targets={"FabFilter Pro-Q": TranslationTarget(PluginKind.VST3, "Pro-Q 3", OTHER_FIELDS)},
        uid_lookup=UidLookup(),
        loadable=nothing_loads,
    )
    refused = report.by_status(RepairStatus.SET_TOO_OLD_FOR_TARGET)
    assert [action.source_name for action in refused] == ["FabFilter Pro-Q", "FabFilter Pro-Q"]
    assert all(action.target_name == "Pro-Q 3" for action in refused)  # the mapping is fine
    assert report.set_too_old_count == 2
    assert report.fixed_count == 0
    assert [ET.tostring(info) for info in live_set.root.iter("VstPluginInfo")] == before
    assert not list(live_set.root.iter("Vst3PluginInfo"))


def test_a_vst3_target_still_lands_on_a_set_new_enough_to_hold_one() -> None:
    """The floor refuses old documents and nothing else."""
    live_set = make_set("11.3.42")
    report = repair_set(
        live_set,
        targets={"Effectrix": TranslationTarget(PluginKind.VST3, "Effectrix", OTHER_FIELDS)},
        uid_lookup=UidLookup(),
        loadable=nothing_loads,
    )
    assert only(report, "Effectrix").status is RepairStatus.FIXED
    assert report.set_too_old_count == 0
