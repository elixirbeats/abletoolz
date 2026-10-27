"""Whether Live could load a plugin device here today, and where its plugin is.

Hermetic on any OS. Every question goes to the one oracle a run builds
(:class:`LoadableOracle`), directly or through ``Plugins.scan``. Live's plugin
database is built row by row in the schema measured on Live 12.4.5b, so the tests
state what that schema means rather than depending on what this machine has
installed; plugin folders and mac bundles are tmp_path, macOS's component
registry is stood in for, and the sets are the version skeletons.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import pathlib
import plistlib
import sqlite3
from xml.etree import ElementTree as ET

import pytest
from conftest import (
    device_named,
    installed,
    live_database,
    make_bundle,
    skeleton,
    skeleton_set,
    write_live_database,
)

from abletoolz import cli
from abletoolz.live_set import AbletonSet
from abletoolz.live_set.plugins import PluginRef
from abletoolz.machine import Locations
from abletoolz.plugins import loadable
from abletoolz.plugins.config import AbletoolzConfig
from abletoolz.plugins.container.device import PluginDevice
from abletoolz.plugins.loadable import LoadableOracle
from abletoolz.run.console import render_plugins

# Dev identifiers as Live's database spells them. Decapitator's is the class id
# the 11.3.42 skeleton's one VST3 device stores.
SERUM_VST2 = "device:vst:instr:1483109208?n=Serum_x64"
DECAPITATOR = "device:vst3:audiofx:56535453-7444-6364-6563-617069746174"
APPLE_BANDPASS = (1635083896, 1651532147, 1634758764)
ARTURIA_ANALOG_LAB = (1635085685, 1097621878, 1098019957)


def refs(ableton_set: AbletonSet, locations: Locations, kind: str) -> dict[str | None, PluginRef]:
    """The set's plugin references of one format, by name, as an oracle on ``locations`` judges them."""
    return {ref.name: ref for ref in ableton_set.plugins.scan(LoadableOracle(locations)) if ref.kind == kind}


def with_plugins(locations: Locations, folder: pathlib.Path) -> Locations:
    return dataclasses.replace(locations, vst_dirs=(folder,))


def files_database(path: pathlib.Path) -> pathlib.Path:
    """Live's file database, which sits beside the plugin one and has none of its tables."""
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT)")
    connection.commit()
    connection.close()
    return path


# -- Live's database --------------------------------------------------------


@pytest.mark.parametrize(
    ("device", "row", "present", "loads"),
    [
        pytest.param(
            "Serum_x64", ("Serum_x64", SERUM_VST2, "Serum_x64.dll", "custom", True), True, True, id="folder on"
        ),
        # The measured cause of "missing" devices: the .dll is in a plugin folder, Live's custom folder is off.
        pytest.param(
            "Serum_x64", ("Serum_x64", SERUM_VST2, "p39730.dll", "custom", False), True, False, id="folder off"
        ),
        pytest.param(
            "Decapitator", ("Decapitator", DECAPITATOR, "D.vst3", "global", True), True, True, id="vst3 by class id"
        ),
        pytest.param(
            "Decapitator", ("Decapitator", DECAPITATOR, "D.vst3", "global", True), False, False, id="module gone"
        ),
        # A Windows set names the device after its file; Live's database names the plugin, and loads it by id.
        pytest.param(
            "Serum_x64",
            ("Serum", "device:vst:instr:1483109208?n=Serum", "Serum_x64.vst", "custom", True),
            True,
            True,
            id="vst2 by id when the names differ",
        ),
        pytest.param(
            "Effectrix", ("Serum_x64", SERUM_VST2, "Serum_x64.dll", "custom", True), True, False, id="not listed"
        ),
    ],
)
def test_the_database_answers_and_wins_over_disk(
    tmp_path: pathlib.Path,
    locations: Locations,
    device: str,
    row: tuple[str, str, str, str, bool],
    present: bool,
    loads: bool,
) -> None:
    """Every module file sits in a plugin folder, so only the database can say no."""
    name, dev_identifier, file_name, domain, enabled = row
    module = installed(tmp_path, file_name) if present else tmp_path / file_name
    here = dataclasses.replace(
        with_plugins(locations, tmp_path),
        live_database=live_database(tmp_path, [(name, dev_identifier, module, domain, enabled)]),
    )
    assert LoadableOracle(here).loads(device_named(skeleton("11.3.42"), device)) is loads


def test_only_the_newest_database_with_plugin_tables_answers(tmp_path: pathlib.Path, locations: Locations) -> None:
    """Measured: a Live 11 database sits beside the Live 12 one and contradicts it.

    Live 11's combined files-and-plugins database has the custom VST2 folder
    enabled, Live 12's has it disabled. Merging the two would answer for the Live
    the user stopped running, so only the newest file that has the tables counts
    -- and Live's file database, newer still, has none of them.
    """
    module = installed(tmp_path, "Serum_x64.dll")
    folder = tmp_path / "Live Database"
    folder.mkdir()
    live_11 = write_live_database(folder / "Live-files-53.db", [("Serum_x64", SERUM_VST2, module, "custom", True)])
    live_12 = write_live_database(folder / "Live-plugins-1.db", [("Serum_x64", SERUM_VST2, module, "custom", False)])
    files = files_database(folder / "Live-files-12300.db")
    for age, database in enumerate((live_11, live_12, files), start=1):
        os.utime(database, (age, age))
    here = dataclasses.replace(locations, live_database=folder)
    serum = device_named(skeleton("11.3.42"), "Serum_x64")

    assert not LoadableOracle(here).loads(serum)
    os.utime(live_11, (9, 9))  # running Live 11 again
    assert LoadableOracle(here).loads(serum)


@pytest.mark.parametrize(
    ("stored_there", "copy_in_folder", "loads"),
    [(True, False, True), (False, False, False), (False, True, True)],
    ids=["stored path there", "stored path gone", "a copy in a plugin folder"],
)
def test_without_a_usable_database_the_plugin_folders_answer(
    tmp_path: pathlib.Path, locations: Locations, stored_there: bool, copy_in_folder: bool, loads: bool
) -> None:
    """No Live installed, another OS, or a database folder holding nothing usable: the files on disk decide."""
    device = device_named(skeleton("11.3.42"), "Serum_x64")
    device.set_reference(installed(tmp_path, "Stored.dll") if stored_there else tmp_path / "gone" / "Stored.dll")
    plugins = tmp_path / "VstPlugins"
    plugins.mkdir()
    if copy_in_folder:
        installed(plugins, "Stored.dll")
    database_dir = tmp_path / "Live Database"
    database_dir.mkdir()
    files_database(database_dir / "Live-files-12300.db")
    here = dataclasses.replace(with_plugins(locations, plugins), live_database=database_dir)
    assert LoadableOracle(here).loads(device) is loads


def test_a_plugin_folder_from_config_is_one_the_checks_look_in(
    tmp_path: pathlib.Path, locations: Locations, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``plugin_database.paths`` reaches --check-plugins and plans, not only the plugin database."""
    device = device_named(skeleton("11.3.42"), "Serum_x64")
    device.set_reference(tmp_path / "gone" / "Stored.dll")
    extra = tmp_path / "Extra VSTs"
    extra.mkdir()
    installed(extra, "Stored.dll")
    assert LoadableOracle(locations).loads(device) is False
    seen: list[Locations] = []
    monkeypatch.setattr(cli, "process", lambda args, config, here, argv: seen.append(here) or 0)
    config = AbletoolzConfig(plugin_paths=[extra])
    assert cli.main(["--check-plugins", str(tmp_path)], config=config, locations=locations) == 0
    (here,) = seen
    assert here.vst_dirs.count(extra) == 1
    assert here.with_plugin_folders([extra]) is here
    assert LoadableOracle(here).loads(device) is True


# -- where a plugin is ------------------------------------------------------

SHELL = {"CFBundleName": "ShellCore", "AudioComponents": [{"name": "Vendor: Nice Plugin"}, {"name": "Other"}]}


def vst3_device(name: str) -> PluginDevice:
    """A VST3 device that stores its display name and nothing else to find it by."""
    info = ET.Element("Vst3PluginInfo")
    ET.SubElement(info, "Name", Value=name)
    return PluginDevice(info)


@pytest.mark.parametrize(
    ("bundle", "plist", "name"),
    [
        pytest.param("Pro-R.vst3", None, "Pro-R", id="named after the plugin, no Info.plist"),
        pytest.param("Weird Install Name.vst3", {"CFBundleDisplayName": "Pro-R 2"}, "Pro-R 2", id="display name"),
        pytest.param("Vendor Thing.vst3", {"CFBundleName": "Thing"}, "Thing", id="bundle name"),
        pytest.param("Shell.vst3", SHELL, "Vendor: Nice Plugin", id="first audio component"),
        pytest.param("Shell.vst3", SHELL, "Other", id="second audio component"),
        pytest.param("SomeVendor/Deep One.vst3", None, "Deep One", id="in a vendor folder"),
    ],
)
def test_an_installed_vst3_bundle_is_found_by_the_name_a_set_stores(
    tmp_path: pathlib.Path, locations: Locations, bundle: str, plist: dict[str, object] | None, name: str
) -> None:
    """The bundle's folder is often named otherwise; its Info.plist bridges the two."""
    found = make_bundle(tmp_path, bundle, plist=plist)
    assert LoadableOracle(with_plugins(locations, tmp_path)).find(vst3_device(name)) == found


def test_a_single_file_vst3_is_found_by_its_stem_and_a_broken_bundle_hides_nothing(
    tmp_path: pathlib.Path, locations: Locations
) -> None:
    """Windows installs a .vst3 as a plain file; a broken Info.plist must not lose its neighbours."""
    serum = installed(tmp_path, "Serum.vst3")
    make_bundle(tmp_path, "AAA Broken.vst3", raw_plist=b"\x00not a plist at all")
    good = make_bundle(tmp_path, "ZZZ Fine.vst3", plist={"CFBundleDisplayName": "Nice Name"})
    oracle = LoadableOracle(with_plugins(locations, tmp_path))
    assert oracle.find(vst3_device("Serum")) == serum
    assert oracle.find(vst3_device("Nice Name")) == good
    assert oracle.find(vst3_device("Only In Broken")) is None


def test_a_windows_dll_is_only_found_on_windows(tmp_path: pathlib.Path, locations: Locations) -> None:
    """A Windows DLL cannot be loaded on macOS, so a Mac never offers one as a copy."""
    copy = installed(tmp_path, "CamelCrusher.dll")
    on_windows = with_plugins(locations, tmp_path)
    assert refs(skeleton_set("10.1.18"), on_windows, "vst")["CamelCrusher.dll"].alternative == copy
    on_a_mac = dataclasses.replace(on_windows, platform="darwin")
    assert refs(skeleton_set("10.1.18"), on_a_mac, "vst")["CamelCrusher.dll"].alternative is None


def test_scan_resolves_bundle_via_plist(tmp_path: pathlib.Path, locations: Locations) -> None:
    """A set's vst3 ref resolves to a bundle whose dir name differs from the display name."""
    bundle = make_bundle(tmp_path, "FabFilter Pro-L 2 Installed.vst3", plist={"CFBundleDisplayName": "Pro-L 2"})
    found = refs(skeleton_set("10.1.30"), with_plugins(locations, tmp_path), "vst3")
    assert found["Pro-L 2"].exists
    assert found["Pro-L 2"].path == bundle
    assert not found["Pro-Q 3"].exists
    assert found["Pro-Q 3"].path is None


def test_an_oracle_indexes_its_folders_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, locations: Locations
) -> None:
    """Every device of every set a run reads is judged against one walk of the plugin folders."""
    make_bundle(tmp_path, "FabFilter Pro-R Installed.vst3", plist={"CFBundleDisplayName": "Pro-R"})
    oracle = loadable.oracle(with_plugins(locations, tmp_path))
    walks: list[pathlib.Path] = []
    original_rglob = pathlib.Path.rglob

    def counting_rglob(path: pathlib.Path, pattern: str) -> object:
        walks.append(path)
        return original_rglob(path, pattern)

    monkeypatch.setattr(pathlib.Path, "rglob", counting_rglob)
    for key in ("10.1.18", "10.1.30", "11.3.42"):
        skeleton_set(key).plugins.scan(oracle)
    assert loadable.oracle(with_plugins(locations, tmp_path)) is oracle
    assert walks == [tmp_path]


def test_scan_resolves_via_live_database(tmp_path: pathlib.Path, locations: Locations) -> None:
    """With nothing in the plugin folders, Live's own database says where a VST3 is."""
    module = installed(tmp_path, "Decapitator.vst3")
    database_dir = live_database(tmp_path, [("Decapitator", DECAPITATOR, module, "global", True)])
    found = refs(skeleton_set("11.3.42"), dataclasses.replace(locations, live_database=database_dir), "vst3")
    assert found["Decapitator"].exists
    assert found["Decapitator"].path == module


def test_scan_honors_stored_path(tmp_path: pathlib.Path, locations: Locations) -> None:
    """Newer sets store a Path on Vst3PluginInfo; an existing one wins without any search."""
    on_disk = installed(tmp_path, "Decapitator.vst3")
    ableton_set = skeleton_set("11.3.42")
    element = ableton_set.root.find(".//Vst3PluginInfo")
    assert element is not None
    ET.SubElement(element, "Path", {"Value": str(on_disk)})
    (ref,) = refs(ableton_set, locations, "vst3").values()
    assert ref.name == "Decapitator"
    assert ref.exists
    assert ref.path == on_disk
    assert ref.alternative is None


def test_scan_searches_when_stored_path_is_gone(tmp_path: pathlib.Path, locations: Locations) -> None:
    """A dead stored path is not the end: Live loads the copy it finds by name, which is the alternative."""
    bundle = make_bundle(tmp_path, "Decapitator.vst3")
    ableton_set = skeleton_set("11.3.42")
    element = ableton_set.root.find(".//Vst3PluginInfo")
    assert element is not None
    ET.SubElement(element, "Path", {"Value": str(tmp_path / "gone" / "Decapitator.vst3")})
    (ref,) = refs(ableton_set, with_plugins(locations, tmp_path), "vst3").values()
    assert ref.exists
    assert ref.path == tmp_path / "gone" / "Decapitator.vst3"
    assert ref.alternative == bundle


# -- Audio Units: macOS's component registry, and the bundles declaring them --


def make_component(root: pathlib.Path, name: str, component_type: str, subtype: str, manufacturer: str) -> pathlib.Path:
    bundle = root / f"{name}.component"
    contents = bundle / "Contents"
    contents.mkdir(parents=True)
    component = {"type": component_type, "subtype": subtype, "manufacturer": manufacturer, "name": f"Vendor: {name}"}
    with (contents / "Info.plist").open("wb") as file:
        plistlib.dump({"AudioComponents": [component]}, file)
    return bundle


def on_a_mac(locations: Locations, components: pathlib.Path | None = None) -> Locations:
    """``locations`` as a Mac, its Audio Unit folder ``components``."""
    return dataclasses.replace(locations, platform="darwin", au_dirs=() if components is None else (components,))


def registered(monkeypatch: pytest.MonkeyPatch, *identities: tuple[int, int, int]) -> None:
    """Have macOS's component registry list exactly ``identities``."""
    monkeypatch.setattr(loadable, "audio_component_registered", lambda identifier: identifier in identities)


def audio_units(locations: Locations) -> dict[str | None, PluginRef]:
    """The 11.3.41 skeleton's Audio Units, Apple's AUBandpass and Arturia's Analog Lab V among them."""
    return refs(skeleton_set("11.3.41"), locations, "au")


def test_scan_resolves_registered_component_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, locations: Locations
) -> None:
    bundle = make_component(tmp_path, "Installed Analog Lab", "aumu", "Alav", "Artu")
    registered(monkeypatch, ARTURIA_ANALOG_LAB)
    analog_lab = audio_units(on_a_mac(locations, tmp_path))["Analog Lab V"]
    assert analog_lab.exists
    assert analog_lab.path == bundle
    assert analog_lab.alternative is None


def test_a_registered_builtin_needs_no_bundle_and_reads_as_verified(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, locations: Locations
) -> None:
    registered(monkeypatch, APPLE_BANDPASS)
    bandpass = audio_units(on_a_mac(locations))["AUBandpass"]
    assert bandpass.exists
    assert bandpass.path is None
    assert bandpass.alternative is None

    with caplog.at_level(logging.INFO):
        render_plugins([bandpass])
    assert "Plugin: Apple: AUBandpass, Path: None, Exists: True" in caplog.text
    assert "cannot be verified" not in caplog.text


def test_an_unregistered_au_is_missing_and_its_bundle_only_an_alternative(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, locations: Locations
) -> None:
    bundle = make_component(tmp_path, "Broken Analog Lab", "aumu", "Alav", "Artu")
    registered(monkeypatch)
    found = audio_units(on_a_mac(locations, tmp_path))
    assert {(ref.exists, ref.path) for ref in found.values()} == {(False, None)}
    assert found["Analog Lab V"].alternative == bundle
    assert found["AUBandpass"].alternative is None


def test_the_registry_is_never_asked_off_a_mac(monkeypatch: pytest.MonkeyPatch, locations: Locations) -> None:
    registered(monkeypatch, APPLE_BANDPASS)
    assert not any(ref.exists for ref in audio_units(locations).values())
