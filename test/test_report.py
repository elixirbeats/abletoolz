"""The run report: one record per set, the refusals in it, and the totals over it.

Same hermetic CLI drive as the rest of the suite. The point of every assertion
here is that the JSON says what the console said -- a device the console reports
as too old for its target is a refusal record with the plugin and the format in
it, not a gap between two fixes.
"""

from __future__ import annotations

import json
import pathlib
from collections import Counter
from typing import Any

import pytest

from abletoolz import cli
from abletoolz.live_set import plugins
from abletoolz.plugin_parsers import PluginKind, plugin_db, repair, upgrade_rules
from abletoolz.plugin_parsers.config import AbletoolzConfig
from abletoolz.plugin_parsers.format_translation import TranslationTarget

SKELETONS = pathlib.Path(__file__).parent / "version_fixtures" / "skeletons"

# Any four fields: repair refuses this set before it ever reads them.
SOME_FIELDS = (1, 2, 3, 4)


def run_cli(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr("sys.argv", ["abletoolz", *argv])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    code = excinfo.value.code
    assert isinstance(code, int)
    return code


@pytest.fixture(autouse=True)
def hermetic(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """No real plugin dirs, no real Live database, no real user config or plugin db."""
    monkeypatch.setattr(plugins, "default_vst_dirs", lambda: [])
    monkeypatch.setattr(plugins, "default_live_database_dir", lambda: None)
    monkeypatch.setattr(plugin_db, "default_vst_dirs", lambda: [])
    monkeypatch.setattr(plugin_db, "default_live_database_dir", lambda: None)
    monkeypatch.setattr(plugin_db, "DEFAULT_PLUGIN_DB_PATH", tmp_path / "config" / "plugin_db.json")
    monkeypatch.setattr(repair, "default_live_database_dir", lambda: None)
    monkeypatch.setattr(cli, "load_config", lambda: AbletoolzConfig())
    monkeypatch.setattr(plugins, "load_config", lambda: AbletoolzConfig())


def copy_set(directory: pathlib.Path, key: str, name: str) -> pathlib.Path:
    copy = directory / name
    copy.write_bytes((SKELETONS / f"{key}.als").read_bytes())
    return copy


def read_report(directory: pathlib.Path) -> dict[str, Any]:
    written = list(directory.glob("abletoolz_report_*.json"))
    assert len(written) == 1, f"expected one report in {directory}, found {written}"
    document = json.loads(written[0].read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def use_config(monkeypatch: pytest.MonkeyPatch, config: AbletoolzConfig) -> None:
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(plugins, "load_config", lambda: config)


# -- the record --------------------------------------------------------------


def test_the_report_holds_one_record_per_set(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """Every set the run walked over is in it, whether or not anything happened to it."""
    copy_set(tmp_path, "11.3.42", "first.als")
    copy_set(tmp_path, "12.2.6", "second.als")
    assert run_cli(monkeypatch, str(tmp_path), "--check-plugins") == 0

    document = read_report(tmp_path)
    assert document["command"] == [str(tmp_path), "--check-plugins"]
    assert {pathlib.Path(record["path"]).name for record in document["sets"]} == {"first.als", "second.als"}
    assert document["totals"]["sets"] == 2
    assert document["totals"]["changed"] == 0
    assert document["totals"]["failed"] == 0
    # The missing-plugin table over the run is the per-set tables added up.
    combined: Counter[str] = Counter()
    for record in document["sets"]:
        combined.update(record["plugins_missing"])
    assert document["totals"]["plugins_missing"] == dict(combined)
    assert combined["p39730.dll"] == 1


def test_a_set_that_cannot_be_read_is_a_record_with_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """A run that fails still says what failed, in the same file as everything else."""
    copy_set(tmp_path, "11.3.42", "good.als")
    (tmp_path / "corrupt.als").write_bytes(b"not a gzip file")
    assert run_cli(monkeypatch, str(tmp_path), "--check-plugins") == 1

    document = read_report(tmp_path)
    errors = {pathlib.Path(record["path"]).name: record["error"] for record in document["sets"]}
    assert errors["good.als"] is None
    assert errors["corrupt.als"] == "could not read the set"
    assert document["totals"]["failed"] == 1


# -- what was fixed ----------------------------------------------------------


def test_a_repaired_device_says_what_it_became_and_what_did_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """With nothing loadable, the fixture's one mapped device is a repair."""
    copy = copy_set(tmp_path, "11.3.42", "repair.als")
    assert run_cli(monkeypatch, str(copy), "--repair-plugins", "-s") == 0

    document = read_report(tmp_path)
    record = document["sets"][0]
    assert record["changed"] is True
    assert record["written"] == str(copy)
    assert record["fixes"] == [
        {
            "device": "Serum_x64",
            "mechanism": "repair",
            "track": "1-Serum_x64",
            "source": "Serum_x64",
            "target": "Serum",
        }
    ]
    assert document["totals"]["fixes_by_mechanism"] == {"repair": 1}
    # What the run walked in on, repaired devices included.
    assert record["plugins_missing"]["Serum_x64"] == 1


def test_an_upgraded_device_says_which_machinery_moved_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Upgrading points a device at another file, and the report keeps that apart from a repair."""
    installed = tmp_path / "VstPlugins"
    installed.mkdir()
    (installed / "Fresh.dll").write_bytes(b"")
    monkeypatch.setattr(upgrade_rules, "default_vst_dirs", lambda: [installed])
    use_config(monkeypatch, AbletoolzConfig(plugin_upgrade_rules={"p39730.dll": ["Fresh.dll"]}))

    copy = copy_set(tmp_path, "11.3.42", "upgrade.als")
    assert run_cli(monkeypatch, str(copy), "--upgrade-plugins", "-s") == 0

    record = read_report(tmp_path)["sets"][0]
    assert record["fixes"] == [
        {
            "device": "p39730.dll",
            "mechanism": "upgrade",
            "track": "1-Serum_x64",
            "source": "p39730.dll",
            "target": "Fresh.dll",
        }
    ]


def test_a_run_that_changes_nothing_says_so(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """No upgrade rules, so the set comes out of the run exactly as it went in."""
    copy = copy_set(tmp_path, "11.3.42", "steady.als")
    assert run_cli(monkeypatch, str(copy), "--upgrade-plugins") == 0

    record = read_report(tmp_path)["sets"][0]
    assert record["changed"] is False
    assert record["fixes"] == []


# -- what was refused --------------------------------------------------------


def test_a_set_too_old_for_its_target_is_a_refusal_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The refusal a user can act on, in a shape a program can count.

    Live rejects the whole file rather than the one device, so this never gets
    written; the report has to be able to say which plugin and which format,
    because "open it in Live and save it" is what fixes it.
    """
    use_config(
        monkeypatch,
        AbletoolzConfig(
            plugin_translation_targets={"FabFilter Pro-Q": TranslationTarget(PluginKind.VST3, "Pro-Q 3", SOME_FIELDS)}
        ),
    )
    copy = copy_set(tmp_path, "9.0.1", "old.als")
    assert run_cli(monkeypatch, str(copy), "--repair-plugins", "-s") == 0

    document = read_report(tmp_path)
    record = document["sets"][0]
    assert record["changed"] is False
    too_old = [refusal for refusal in record["refusals"] if refusal["reason"] == "set_too_old_for_target"]
    assert [refusal["device"] for refusal in too_old] == ["FabFilter Pro-Q", "FabFilter Pro-Q"]
    assert all(refusal["target_format"] == "vst3" for refusal in too_old)
    assert all(refusal["target_name"] == "Pro-Q 3" for refusal in too_old)
    assert document["totals"]["refusals_by_reason"]["set_too_old_for_target"] == 2
    assert document["totals"]["fixes"] == 0


def test_an_unmapped_broken_device_is_a_refusal_too(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """Repair looked at it and left it alone, which is an answer worth counting."""
    copy = copy_set(tmp_path, "11.3.42", "repair.als")
    assert run_cli(monkeypatch, str(copy), "--repair-plugins") == 0

    document = read_report(tmp_path)
    reasons = document["totals"]["refusals_by_reason"]
    assert reasons["broken_unmapped"] >= 1
    assert {refusal["device"] for refusal in document["sets"][0]["refusals"]} >= {"Effectrix"}


# -- where it goes -----------------------------------------------------------


def test_the_report_follows_output(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """With --output nothing is written next to the originals, the report included."""
    library = tmp_path / "library"
    library.mkdir()
    copy = copy_set(library, "11.3.42", "repair.als")
    quarantine = tmp_path / "quarantine"

    assert run_cli(monkeypatch, str(copy), "--repair-plugins", "-s", "--output", str(quarantine)) == 0

    assert not list(library.glob("abletoolz_report_*.json"))
    document = read_report(quarantine)
    assert document["sets"][0]["path"] == str(copy)
    assert document["sets"][0]["written"] == str(quarantine / "repair.als")
