"""Audio Unit resolution from exact set identities, component plists, and macOS registration."""

from __future__ import annotations

import logging
import pathlib
import plistlib

import pytest

from abletoolz.console import render_plugins
from abletoolz.live_set import AbletonSet, plugins

SKELETON = pathlib.Path(__file__).parent / "version_fixtures" / "skeletons" / "11.3.41.als"
APPLE_BANDPASS = (1635083896, 1651532147, 1634758764)
ARTURIA_ANALOG_LAB = (1635085685, 1097621878, 1098019957)


def make_component(
    root: pathlib.Path,
    name: str,
    component_type: str,
    subtype: str,
    manufacturer: str,
) -> pathlib.Path:
    bundle = root / f"{name}.component"
    contents = bundle / "Contents"
    contents.mkdir(parents=True)
    with (contents / "Info.plist").open("wb") as file:
        plistlib.dump(
            {
                "AudioComponents": [
                    {
                        "type": component_type,
                        "subtype": subtype,
                        "manufacturer": manufacturer,
                        "name": f"Vendor: {name}",
                    }
                ]
            },
            file,
        )
    return bundle


def make_set() -> AbletonSet:
    ableton_set = AbletonSet(SKELETON)
    assert ableton_set.parse()
    return ableton_set


@pytest.fixture
def hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugins, "default_vst_dirs", lambda: [])
    monkeypatch.setattr(plugins, "default_live_database_dir", lambda: None)
    monkeypatch.setattr(plugins, "default_au_component_dirs", lambda: [])
    monkeypatch.setattr(plugins, "audio_component_registered", lambda _identifier: False)


@pytest.mark.usefixtures("hermetic")
def test_parse_au_element_reads_exact_identity() -> None:
    domain = make_set().plugins
    element = domain._root.find(".//AuPluginInfo")
    assert element is not None
    assert domain.parse_au_element(element) == ("AUBandpass", "Apple", APPLE_BANDPASS)


@pytest.mark.usefixtures("hermetic")
def test_scan_resolves_registered_component_bundle(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    bundle = make_component(tmp_path, "Installed Analog Lab", "aumu", "Alav", "Artu")
    monkeypatch.setattr(plugins, "default_au_component_dirs", lambda: [tmp_path])
    monkeypatch.setattr(plugins, "audio_component_registered", lambda identifier: identifier == ARTURIA_ANALOG_LAB)

    refs = {ref.name: ref for ref in make_set().plugins.scan([]) if ref.kind == "au"}
    assert refs["Analog Lab V"].exists
    assert refs["Analog Lab V"].path == bundle
    assert refs["Analog Lab V"].alternative is None


@pytest.mark.usefixtures("hermetic")
def test_scan_resolves_registered_builtin_without_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugins, "audio_component_registered", lambda identifier: identifier == APPLE_BANDPASS)

    refs = {ref.name: ref for ref in make_set().plugins.scan([]) if ref.kind == "au"}
    assert refs["AUBandpass"].exists
    assert refs["AUBandpass"].path is None
    assert refs["AUBandpass"].alternative is None


@pytest.mark.usefixtures("hermetic")
def test_scan_reports_unregistered_au_missing() -> None:
    refs = [ref for ref in make_set().plugins.scan([]) if ref.kind == "au"]
    assert refs
    assert all(not ref.exists for ref in refs)
    assert all(ref.path is None for ref in refs)
    assert all(ref.alternative is None for ref in refs)


@pytest.mark.usefixtures("hermetic")
def test_unregistered_bundle_is_only_an_alternative(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    bundle = make_component(tmp_path, "Broken Analog Lab", "aumu", "Alav", "Artu")
    monkeypatch.setattr(plugins, "default_au_component_dirs", lambda: [tmp_path])

    ref = next(ref for ref in make_set().plugins.scan([]) if ref.name == "Analog Lab V")
    assert not ref.exists
    assert ref.path is None
    assert ref.alternative == bundle


@pytest.mark.usefixtures("hermetic")
def test_registered_builtin_renders_as_verified(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(plugins, "audio_component_registered", lambda identifier: identifier == APPLE_BANDPASS)
    ref = next(ref for ref in make_set().plugins.scan([]) if ref.name == "AUBandpass")

    with caplog.at_level(logging.INFO):
        render_plugins([ref])

    assert "Plugin: Apple: AUBandpass, Path: None, Exists: True" in caplog.text
    assert "cannot be verified" not in caplog.text
