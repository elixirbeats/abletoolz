"""VST3 resolution: bundle/Info.plist matching, single-file paths, Live Database lookup.

Hermetic on any OS: mac-shaped ``.vst3`` bundles are synthesized in tmp_path via
plistlib, Live Database fixtures via sqlite3, and the platform default dirs are
monkeypatched. Nothing touches real plugin dirs or Ableton data.
"""

from __future__ import annotations

import os
import pathlib
import plistlib
import sqlite3
from xml.etree import ElementTree as ET

import pytest

from abletoolz.live_set import AbletonSet, plugins

SKELETONS = pathlib.Path(__file__).parent / "version_fixtures" / "skeletons"


def make_bundle(
    root: pathlib.Path,
    dirname: str,
    plist: dict[str, object] | None = None,
    raw_plist: bytes | None = None,
) -> pathlib.Path:
    """Synthesize a mac-shaped .vst3 bundle dir; plist omitted when both args are None."""
    bundle = root / f"{dirname}.vst3"
    contents = bundle / "Contents"
    contents.mkdir(parents=True)
    if raw_plist is not None:
        (contents / "Info.plist").write_bytes(raw_plist)
    elif plist is not None:
        with (contents / "Info.plist").open("wb") as file:
            plistlib.dump(plist, file)
    return bundle


def make_plugin_db(path: pathlib.Path, rows: list[tuple[str, str, str]]) -> None:
    """Build a Live-plugins-shaped sqlite db from (name, dev_identifier, module_path) rows."""
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE plugin_modules (module_id INTEGER PRIMARY KEY, path TEXT, arch INTEGER)")
    connection.execute(
        "CREATE TABLE plugins"
        " (plugin_id INTEGER PRIMARY KEY, module_id INTEGER, dev_identifier TEXT, name TEXT, vendor TEXT)"
    )
    for module_id, (name, dev_identifier, module_path) in enumerate(rows, start=1):
        connection.execute("INSERT INTO plugin_modules VALUES (?, ?, 0)", (module_id, module_path))
        connection.execute("INSERT INTO plugins VALUES (?, ?, ?, ?, '')", (module_id, module_id, dev_identifier, name))
    connection.commit()
    connection.close()


def test_bundle_matched_by_dir_name(tmp_path: pathlib.Path) -> None:
    """A bundle without any Info.plist still matches when the dir name is the display name."""
    bundle = make_bundle(tmp_path, "Pro-R")
    assert plugins.resolve_vst3_name("Pro-R", [tmp_path]) == bundle


def test_bundle_matched_by_display_name(tmp_path: pathlib.Path) -> None:
    """Bundle dir name differs from the display name the set stores; Info.plist bridges them."""
    bundle = make_bundle(tmp_path, "FabFilter Weird Install Name", {"CFBundleDisplayName": "Pro-R 2"})
    assert plugins.resolve_vst3_name("Pro-R 2", [tmp_path]) == bundle


def test_bundle_matched_by_cfbundlename(tmp_path: pathlib.Path) -> None:
    bundle = make_bundle(tmp_path, "Vendor Thing", {"CFBundleName": "Thing"})
    assert plugins.resolve_vst3_name("Thing", [tmp_path]) == bundle


def test_bundle_matched_by_audio_component_name(tmp_path: pathlib.Path) -> None:
    bundle = make_bundle(
        tmp_path,
        "Shell",
        {"CFBundleName": "ShellCore", "AudioComponents": [{"name": "Vendor: Nice Plugin"}, {"name": "Other"}]},
    )
    assert plugins.resolve_vst3_name("Vendor: Nice Plugin", [tmp_path]) == bundle
    assert plugins.resolve_vst3_name("Other", [tmp_path]) == bundle


def test_malformed_plist_skips_bundle_not_scan(tmp_path: pathlib.Path) -> None:
    """A broken Info.plist in one bundle must not kill resolution of its neighbors."""
    make_bundle(tmp_path, "AAA Broken", raw_plist=b"\x00not a plist at all")
    good = make_bundle(tmp_path, "ZZZ Fine", {"CFBundleDisplayName": "Nice Name"})
    assert plugins.resolve_vst3_name("Nice Name", [tmp_path]) == good
    assert plugins.resolve_vst3_name("Only In Broken", [tmp_path]) is None


def test_single_file_vst3_matched_by_stem(tmp_path: pathlib.Path) -> None:
    """Windows installs .vst3 as plain files; match on the stem."""
    serum = tmp_path / "Serum.vst3"
    serum.write_bytes(b"")
    assert plugins.resolve_vst3_name("Serum", [tmp_path]) == serum
    assert plugins.resolve_vst3_name("NotThere", [tmp_path]) is None


def test_bundle_in_vendor_subdir(tmp_path: pathlib.Path) -> None:
    vendor = tmp_path / "SomeVendor"
    vendor.mkdir()
    bundle = make_bundle(vendor, "Deep One")
    assert plugins.resolve_vst3_name("Deep One", [tmp_path]) == bundle


def test_live_database_lookup(tmp_path: pathlib.Path) -> None:
    database = tmp_path / "Live-plugins-1.db"
    module_path = r"C:\Program Files\Common Files\VST3\Acid V.vst3"
    make_plugin_db(
        database,
        [
            ("Acid V", "device:vst3:instr:41727475", module_path),
            ("OldSynth", "device:vst:instr:1234", r"C:\vst\OldSynth.dll"),
        ],
    )
    assert plugins.live_database_lookup("Acid V", database) == pathlib.Path(module_path)
    assert plugins.live_database_lookup("Missing", database) is None
    # VST2 rows never answer a VST3 lookup.
    assert plugins.live_database_lookup("OldSynth", database) is None


def test_search_live_databases_skips_alien_schema(tmp_path: pathlib.Path) -> None:
    """Live-files-*.db has no plugin tables; a newer one must not mask the plugins db."""
    plugins_db = tmp_path / "Live-plugins-1.db"
    module_path = "/Library/Audio/Plug-Ins/VST3/Pro-R.vst3"
    make_plugin_db(plugins_db, [("Pro-R", "device:vst3:audiofx:abcd", module_path)])
    files_db = tmp_path / "Live-files-12300.db"
    connection = sqlite3.connect(files_db)
    connection.execute("CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT)")
    connection.commit()
    connection.close()
    newer = os.path.getmtime(plugins_db) + 10
    os.utime(files_db, (newer, newer))
    assert plugins.search_live_databases("Pro-R", tmp_path) == pathlib.Path(module_path)
    assert plugins.search_live_databases("Missing", tmp_path) is None


def make_set(key: str) -> AbletonSet:
    ableton_set = AbletonSet(SKELETONS / f"{key}.als")
    assert ableton_set.parse()
    return ableton_set


@pytest.fixture
def hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugins, "default_vst_dirs", lambda: [])
    monkeypatch.setattr(plugins, "default_live_database_dir", lambda: None)


@pytest.mark.usefixtures("hermetic")
def test_scan_resolves_bundle_via_plist(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """A set's vst3 ref resolves to a bundle whose dir name differs from the display name."""
    bundle = make_bundle(tmp_path, "FabFilter Pro-R Installed", {"CFBundleDisplayName": "Pro-R"})
    monkeypatch.setattr(plugins, "default_vst_dirs", lambda: [tmp_path])
    refs = {ref.name: ref for ref in make_set("10.1.3").plugins.scan([]) if ref.kind == "vst3"}
    assert refs["Pro-R"].exists
    assert refs["Pro-R"].path == bundle
    assert not refs["Pro-Q 3"].exists
    assert refs["Pro-Q 3"].path is None


@pytest.mark.usefixtures("hermetic")
def test_scan_resolves_via_live_database(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """With nothing on disk, Live's own database is the secondary source."""
    make_plugin_db(
        tmp_path / "Live-plugins-1.db",
        [("Decapitator", "device:vst3:audiofx:abcd", "/Library/Audio/Plug-Ins/VST3/Decapitator.vst3")],
    )
    monkeypatch.setattr(plugins, "default_live_database_dir", lambda: tmp_path)
    refs = {ref.name: ref for ref in make_set("11.3.42").plugins.scan([]) if ref.kind == "vst3"}
    assert refs["Decapitator"].exists
    assert refs["Decapitator"].path == pathlib.Path("/Library/Audio/Plug-Ins/VST3/Decapitator.vst3")


@pytest.mark.usefixtures("hermetic")
def test_scan_honors_stored_path(tmp_path: pathlib.Path) -> None:
    """Newer sets store a Path on Vst3PluginInfo; an existing one wins without any search."""
    on_disk = tmp_path / "Decapitator.vst3"
    on_disk.write_bytes(b"")
    ableton_set = make_set("11.3.42")
    element = ableton_set.root.find(".//Vst3PluginInfo")
    assert element is not None
    ET.SubElement(element, "Path", {"Value": str(on_disk)})
    refs = [ref for ref in ableton_set.plugins.scan([]) if ref.kind == "vst3"]
    assert refs[0].name == "Decapitator"
    assert refs[0].exists
    assert refs[0].path == on_disk
    assert refs[0].alternative is None


@pytest.mark.usefixtures("hermetic")
def test_scan_searches_when_stored_path_is_gone(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """A dead stored path keeps exists False but surfaces the found copy as alternative."""
    bundle = make_bundle(tmp_path, "Decapitator")
    monkeypatch.setattr(plugins, "default_vst_dirs", lambda: [tmp_path])
    ableton_set = make_set("11.3.42")
    element = ableton_set.root.find(".//Vst3PluginInfo")
    assert element is not None
    ET.SubElement(element, "Path", {"Value": str(tmp_path / "gone" / "Decapitator.vst3")})
    refs = [ref for ref in ableton_set.plugins.scan([]) if ref.kind == "vst3"]
    assert not refs[0].exists
    assert refs[0].alternative == bundle


def test_parse_vst3_element_name_and_path() -> None:
    plugins_domain = make_set("10.1.3").plugins
    element = ET.fromstring('<Vst3PluginInfo><Name Value="Pro-R" /><Path Value="C:/x/Pro-R.vst3" /></Vst3PluginInfo>')
    assert plugins_domain.parse_vst3_element(element) == ("Pro-R", pathlib.Path("C:/x/Pro-R.vst3"))
    bare = ET.fromstring('<Vst3PluginInfo><Name Value="Pro-R" /></Vst3PluginInfo>')
    assert plugins_domain.parse_vst3_element(bare) == ("Pro-R", None)
