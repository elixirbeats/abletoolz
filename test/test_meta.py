"""Per-set sidecars: what a scan writes down, what it reuses, and what it never touches.

Driven through ``cli.main()`` like the rest of the CLI suite, because the
sidecar is a fact about a run rather than about a set. Kept hermetic the same
way: no real plugin dirs, no real Live database, no real user config.
"""

from __future__ import annotations

import hashlib
import pathlib
from typing import Any

import pytest
import yaml

from abletoolz import cli, meta
from abletoolz.live_set import plugins
from abletoolz.plugin_parsers import plugin_db, repair
from abletoolz.plugin_parsers.config import AbletoolzConfig

SKELETONS = pathlib.Path(__file__).parent / "version_fixtures" / "skeletons"


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


def copy_set(tmp_path: pathlib.Path, key: str, name: str = "set.als") -> pathlib.Path:
    copy = tmp_path / name
    copy.write_bytes((SKELETONS / f"{key}.als").read_bytes())
    return copy


def sidecar(set_path: pathlib.Path) -> dict[str, Any]:
    document = yaml.safe_load(meta.sidecar_path(set_path).read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def count_scans(monkeypatch: pytest.MonkeyPatch) -> list[pathlib.Path]:
    """Record every set whose plugins are actually resolved against disk."""
    scanned: list[pathlib.Path] = []

    def counted(self: plugins.Plugins, vst_dirs: list[pathlib.Path]) -> list[plugins.PluginRef]:
        scanned.append(self._set.path)
        return []

    monkeypatch.setattr(plugins.Plugins, "scan", counted)
    return scanned


# -- what a scan writes down -------------------------------------------------


def test_a_scan_leaves_what_it_found_beside_the_set(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """The machine zone is the run's answers, and the hash is of what it read."""
    copy = copy_set(tmp_path, "11.3.42")
    assert run_cli(monkeypatch, str(copy), "--check-plugins", "--check-samples") == 0

    document = sidecar(copy)
    scan = document["scan"]
    assert scan["set_hash"] == hashlib.sha256(copy.read_bytes()).hexdigest()
    assert scan["scanned_with"] == meta.SCANNER
    assert scan["live_version"] == "Ableton Live 11.3.42"
    assert scan["bpm"] == 120.0
    assert scan["bars"] == 0
    # With no plugin dirs and no Live database, nothing in the fixture resolves.
    assert scan["plugins_missing"] == {"p39730.dll": 1, "p29892.dll": 1, "Decapitator": 1}
    assert scan["samples_missing"] == 0
    # Nothing in this run fixed anything, which is not the same as fixing nothing.
    assert scan["plugins_fixed"] is None
    assert document["status"] is None and document["notes"] is None


def test_a_run_that_scans_nothing_writes_no_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Listing tracks is not a scan; there is nothing to write down."""
    copy = copy_set(tmp_path, "11.3.42")
    assert run_cli(monkeypatch, str(copy), "--list-tracks") == 0
    assert not meta.sidecar_path(copy).exists()


def test_no_meta_writes_no_sidecar(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    copy = copy_set(tmp_path, "11.3.42")
    assert run_cli(monkeypatch, str(copy), "--check-plugins", "--no-meta") == 0
    assert not meta.sidecar_path(copy).exists()


def test_backups_and_resource_forks_get_no_sidecar(tmp_path: pathlib.Path) -> None:
    """Neither is a set anybody keeps notes about, so neither gets a file written beside it."""
    scan = meta.SetScan(scanned=meta.now(), scanned_with=meta.SCANNER, set_hash="abc")
    backup = tmp_path / "abletoolz_backup" / "old__1.als"
    backup.parent.mkdir()
    fork = tmp_path / "._real.als"
    assert meta.write(backup, scan) is None
    assert meta.write(fork, scan) is None
    assert not list(tmp_path.rglob("*.meta.yaml"))


# -- the user's half ---------------------------------------------------------


def test_a_rescan_carries_the_human_zone_through_verbatim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """status, notes and anything else the user put there survive being rescanned."""
    copy = copy_set(tmp_path, "11.3.42")
    assert run_cli(monkeypatch, str(copy), "--check-plugins") == 0

    path = meta.sidecar_path(copy)
    document = sidecar(copy)
    document["status"] = "needs work"
    document["notes"] = "reverb tail runs long in the breakdown"
    document["rating"] = 4
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    # A different set under the same name: the machine zone must be replaced.
    copy.write_bytes((SKELETONS / "12.2.6.als").read_bytes())
    assert run_cli(monkeypatch, str(copy), "--check-plugins") == 0

    rescanned = sidecar(copy)
    assert rescanned["status"] == "needs work"
    assert rescanned["notes"] == "reverb tail runs long in the breakdown"
    assert rescanned["rating"] == 4
    assert rescanned["scan"]["set_hash"] == hashlib.sha256(copy.read_bytes()).hexdigest()
    assert rescanned["scan"]["live_version"] == "Ableton Live 12.2.6"


def test_an_unreadable_sidecar_is_ignored_and_replaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """A sidecar nothing can parse is no sidecar: the run says so and writes its own."""
    copy = copy_set(tmp_path, "11.3.42")
    meta.sidecar_path(copy).write_text("scan: [this is not a scan block", encoding="utf-8")
    assert run_cli(monkeypatch, str(copy), "--check-plugins") == 0
    assert sidecar(copy)["scan"]["scanned_with"] == meta.SCANNER


# -- the cache ---------------------------------------------------------------


def test_the_same_bytes_and_version_are_not_scanned_twice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The second pass over an unchanged library answers out of the sidecars."""
    copy = copy_set(tmp_path, "11.3.42")
    assert run_cli(monkeypatch, str(copy), "--check-plugins") == 0
    first = sidecar(copy)["scan"]

    scanned = count_scans(monkeypatch)
    assert run_cli(monkeypatch, str(copy), "--check-plugins") == 0
    assert scanned == []
    assert sidecar(copy)["scan"]["plugins_missing"] == first["plugins_missing"]


def test_changed_bytes_are_scanned_again(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    copy = copy_set(tmp_path, "11.3.42")
    assert run_cli(monkeypatch, str(copy), "--check-plugins") == 0

    copy.write_bytes((SKELETONS / "12.2.6.als").read_bytes())
    scanned = count_scans(monkeypatch)
    assert run_cli(monkeypatch, str(copy), "--check-plugins") == 0
    assert scanned == [copy]


def test_another_abletoolz_is_scanned_again(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """A newer abletoolz can give a different answer about the same bytes."""
    copy = copy_set(tmp_path, "11.3.42")
    assert run_cli(monkeypatch, str(copy), "--check-plugins") == 0

    monkeypatch.setattr(meta, "SCANNER", "abletoolz 99.0.0")
    scanned = count_scans(monkeypatch)
    assert run_cli(monkeypatch, str(copy), "--check-plugins") == 0
    assert scanned == [copy]
    assert sidecar(copy)["scan"]["scanned_with"] == "abletoolz 99.0.0"


def test_passes_with_different_flags_accumulate(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """A cache hit keeps what the run did not measure and adds what it did."""
    copy = copy_set(tmp_path, "11.3.42")
    assert run_cli(monkeypatch, str(copy), "--check-samples") == 0
    assert sidecar(copy)["scan"]["plugins_missing"] is None

    assert run_cli(monkeypatch, str(copy), "--check-plugins") == 0
    scan = sidecar(copy)["scan"]
    assert scan["plugins_missing"] == {"p39730.dll": 1, "p29892.dll": 1, "Decapitator": 1}
    assert scan["samples_missing"] == 0  # carried over from the samples pass


# -- where it goes -----------------------------------------------------------


def test_output_keeps_every_write_away_from_the_original(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """With --output the sidecar goes with the copy, and the original's folder is untouched."""
    library = tmp_path / "library"
    library.mkdir()
    copy = copy_set(library, "11.3.42", "repair.als")
    original = copy.read_bytes()
    quarantine = tmp_path / "quarantine"

    assert run_cli(monkeypatch, str(copy), "--repair-plugins", "-s", "--output", str(quarantine)) == 0

    assert [path.name for path in sorted(library.iterdir())] == ["repair.als"]
    assert copy.read_bytes() == original
    written = quarantine / "repair.als"
    assert written.exists()
    document = yaml.safe_load(meta.sidecar_path(written).read_text(encoding="utf-8"))
    assert document["scan"]["set_hash"] == hashlib.sha256(original).hexdigest()
    assert document["scan"]["plugins_fixed"] == {"Serum_x64 -> Serum": 1}


def test_an_output_sidecar_carries_the_original_s_notes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The copy inherits what the user wrote about the set it came from."""
    library = tmp_path / "library"
    library.mkdir()
    copy = copy_set(library, "11.3.42", "repair.als")
    meta.sidecar_path(copy).write_text("status: keeper\nnotes: mixed in 2019\n", encoding="utf-8")
    quarantine = tmp_path / "quarantine"

    assert run_cli(monkeypatch, str(copy), "--repair-plugins", "-s", "--output", str(quarantine)) == 0

    document = yaml.safe_load(meta.sidecar_path(quarantine / "repair.als").read_text(encoding="utf-8"))
    assert document["status"] == "keeper"
    assert document["notes"] == "mixed in 2019"


def test_an_unchanged_set_leaves_nothing_in_the_output_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """No set landed there, so no sidecar for one did either."""
    copy = copy_set(tmp_path, "11.3.42")
    quarantine = tmp_path / "quarantine"
    # No upgrade rules configured, so --upgrade-plugins edits nothing.
    assert run_cli(monkeypatch, str(copy), "--upgrade-plugins", "-s", "--output", str(quarantine)) == 0
    assert not list(quarantine.glob("*.meta.yaml"))
    assert not meta.sidecar_path(copy).exists()


def test_a_renamed_save_takes_its_sidecar_with_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """--append-bars-bpm renames the set, and the sidecar is named for the file that exists."""
    copy = copy_set(tmp_path, "11.3.42")
    assert run_cli(monkeypatch, str(copy), "--check-plugins", "-s", "--append-bars-bpm") == 0
    saved = next(path for path in tmp_path.glob("*.als") if path.name != "set.als")
    assert meta.sidecar_path(saved).exists()
