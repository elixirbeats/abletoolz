"""Upgrade rules point a VST2 device at a different file, in whichever form the set stores it."""

from __future__ import annotations

import pathlib

import pytest

from abletoolz.formats.path_data import SetOperatingSystem
from abletoolz.live_set import AbletonSet, plugins
from abletoolz.plugins.config import AbletoolzConfig
from abletoolz.plugins.container import upgrade_rules
from abletoolz.plugins.container.device import PluginDevices, PluginKind, ReferenceForm

SKELETONS = pathlib.Path(__file__).parent / "version_fixtures" / "skeletons"


def make_set(key: str) -> AbletonSet:
    ableton_set = AbletonSet(SKELETONS / f"{key}.als")
    assert ableton_set.parse()
    return ableton_set


def install(tmp_path: pathlib.Path, name: str) -> pathlib.Path:
    folder = tmp_path / "plugins"
    folder.mkdir(exist_ok=True)
    installed = folder / name
    installed.write_bytes(b"MZ")
    return installed


@pytest.fixture
def rules(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setattr(upgrade_rules, "default_vst_dirs", lambda: [tmp_path / "plugins"])


@pytest.mark.usefixtures("rules")
def test_a_path_form_device_is_pointed_at_the_installed_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    live_set = make_set("11.3.42")
    serum = next(device for device in PluginDevices(live_set.root) if device.identity.name == "Serum_x64")
    old_name = serum.reference.file_name
    assert old_name is not None and serum.reference.form is ReferenceForm.PATH
    installed = install(tmp_path, "Serum.dll")
    config = AbletoolzConfig(plugin_upgrade_rules={old_name: ["Serum.dll"]})
    monkeypatch.setattr(plugins, "load_config", lambda: config)
    upgraded = live_set.plugins.upgrade()
    assert [(entry.source, entry.target) for entry in upgraded] == [(old_name, "Serum.dll")]
    assert serum.kind is PluginKind.VST and serum.identity.name == "Serum"
    reference = serum.reference
    assert reference.form is ReferenceForm.PATH
    assert reference.path == installed
    assert reference.file_name == "Serum.dll"


@pytest.mark.usefixtures("rules")
def test_a_dir_form_device_is_pointed_at_the_installed_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Sets from before Live 11 store the folder as a blob; the rule applies there too."""
    live_set = make_set("9.0.1")
    first = next(iter(PluginDevices(live_set.root)))
    old_name = first.reference.file_name
    assert old_name is not None and first.reference.form is ReferenceForm.DIR
    installed = install(tmp_path, "New.dll")
    config = AbletoolzConfig(plugin_upgrade_rules={old_name: ["New.dll"]})
    monkeypatch.setattr(plugins, "load_config", lambda: config)
    upgraded = live_set.plugins.upgrade()
    assert upgraded and upgraded[0].source == old_name and upgraded[0].target == "New.dll"
    reference = PluginDevices(live_set.root)[0].reference
    assert reference.form is ReferenceForm.DIR
    assert reference.os is SetOperatingSystem.WINDOWS_OS
    assert reference.file_name == "New.dll"
    assert reference.path is not None and str(reference.path).endswith("New.dll")
    assert str(installed.parent) in str(reference.path)


def test_no_rules_means_no_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    live_set = make_set("11.3.42")
    monkeypatch.setattr(plugins, "load_config", lambda: AbletoolzConfig())
    assert live_set.plugins.upgrade() == []
