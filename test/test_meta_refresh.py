"""The scan fingerprint: every input that can change a scan's answer changes it.

A changed fingerprint is what makes a cached scan stale; test_meta shows a
stale scan is never reused. Here each input is handed in -- the support files'
folder, the computer's locations, the config, the machine -- and the
fingerprint is compared before and after.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import platform

import pytest

from abletoolz.machine import Locations
from abletoolz.plugins.config import AbletoolzConfig
from abletoolz.plugins.identity import plugin_db
from abletoolz.plugins.identity.ids import PluginKind
from abletoolz.run import meta


def support_files(root: pathlib.Path) -> pathlib.Path:
    """A shipped package in miniature: one module, one table, one module about to be removed."""
    root.mkdir()
    (root / "vendor.py").write_text("support = 1\n", encoding="utf-8")
    (root / "mapping.json").write_text('{"supported": 1}', encoding="utf-8")
    (root / "removed_vendor.py").write_text("removed = 1", encoding="utf-8")
    return root


@pytest.mark.parametrize("changed", ["vendor.py", "mapping.json", "new_vendor.py", "removed_vendor.py"])
def test_support_changes_change_the_fingerprint_without_a_version_bump(
    tmp_path: pathlib.Path, locations: Locations, changed: str
) -> None:
    """New plugin support refreshes old answers even when the release number stays put, and file times are no clue."""
    root = support_files(tmp_path / "support")
    config = AbletoolzConfig()
    first = meta.scan_fingerprint(config, locations, support_root=root)
    assert first is not None

    path = root / changed
    if changed == "removed_vendor.py":
        path.unlink()
    elif path.exists():
        before = path.stat()
        path.write_bytes(path.read_bytes().replace(b"1", b"2"))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    else:
        path.write_text("new = 2", encoding="utf-8")
    assert meta.scan_fingerprint(config, locations, support_root=root) != first


def test_support_hash_tracks_content_and_relative_paths(tmp_path: pathlib.Path) -> None:
    root = support_files(tmp_path / "support")
    first = meta.support_fingerprint(root)
    destination = tmp_path / "other-install"
    destination.mkdir()
    for path in root.iterdir():
        (destination / path.name).write_bytes(path.read_bytes())
        path.touch()
    (root / "ignored.pyc").write_bytes(b"cached bytecode")
    assert meta.support_fingerprint(root) == first
    assert meta.support_fingerprint(destination) == first
    (destination / "vendor.py").rename(destination / "renamed.py")
    assert meta.support_fingerprint(destination) != first


@pytest.mark.parametrize("change", ["create", "update", "delete"])
def test_plugin_database_changes_change_the_fingerprint(locations: Locations, change: str) -> None:
    path = locations.plugin_db
    if change != "create":
        plugin_db.write_plugin_db(plugin_db.PluginDatabase(built=meta.now()), path)
    first = meta.scan_fingerprint(AbletoolzConfig(), locations)
    if change == "delete":
        path.unlink()
    else:
        entry = plugin_db.PluginEntry(name="new plugin", kind=PluginKind.VST3, source=plugin_db.PluginSource.MODULEINFO)
        plugin_db.write_plugin_db(plugin_db.PluginDatabase(built=meta.now(), plugins=(entry,)), path)
    assert meta.scan_fingerprint(AbletoolzConfig(), locations) != first


def test_live_s_own_plugin_database_changes_change_the_fingerprint(
    tmp_path: pathlib.Path, locations: Locations
) -> None:
    """The plugin checks read Live's database, so Live rescanning its plugins makes a cached check stale."""
    folder = tmp_path / "Live Database"
    folder.mkdir()
    here = dataclasses.replace(locations, live_database=folder)
    empty = meta.scan_fingerprint(AbletoolzConfig(), here)
    database = folder / "Live-plugins-1.db"
    database.write_bytes(b"one plugin")
    created = meta.scan_fingerprint(AbletoolzConfig(), here)
    assert created != empty
    assert meta.scan_fingerprint(AbletoolzConfig(), here) == created
    database.write_bytes(b"two plugins")
    assert meta.scan_fingerprint(AbletoolzConfig(), here) != created


def test_config_uid_database_and_machine_are_scan_dependencies(tmp_path: pathlib.Path, locations: Locations) -> None:
    config = AbletoolzConfig()
    original = meta.scan_fingerprint(config, locations)
    assert original is not None
    assert meta.scan_fingerprint(config, locations) == original
    assert (
        meta.scan_fingerprint(dataclasses.replace(config, plugin_paths=[tmp_path / "plugins"]), locations) != original
    )
    assert meta.scan_fingerprint(config, dataclasses.replace(locations, vst_dirs=(tmp_path,))) != original
    uid_path = tmp_path / "uids.json"
    with_uids = dataclasses.replace(config, plugin_uid_db=uid_path)
    missing = meta.scan_fingerprint(with_uids, locations)
    uid_path.write_text("{}", encoding="utf-8")
    assert meta.scan_fingerprint(with_uids, locations) != missing

    # The machine is this computer's name and working directory unless one is handed in.
    here = (platform.node(), str(pathlib.Path.cwd()))
    assert meta.scan_fingerprint(config, locations, machine=here) == original
    assert meta.scan_fingerprint(config, locations, machine=("another-machine", here[1])) != original
    assert meta.scan_fingerprint(config, locations, machine=(here[0], str(tmp_path))) != original


def test_unreadable_dependencies_disable_cache_reuse(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture, locations: Locations
) -> None:
    """A uid database that is there but cannot be read as a file gives no fingerprint, and so no reuse."""
    assert meta.scan_fingerprint(AbletoolzConfig(plugin_uid_db=tmp_path), locations) is None
    assert "cached scans will not be reused" in caplog.text
