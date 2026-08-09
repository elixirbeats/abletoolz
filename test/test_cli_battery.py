"""Every CLI flag exercised end-to-end through ``cli.main()`` on skeleton fixtures.

The version matrix proves the domain logic per Live version; this file proves the
CLI wiring above it — argument plumbing, save/rename behavior, exit codes. Kept
hermetic: plugin-dir scanning and user config are monkeypatched so nothing
touches the real machine.
"""

from __future__ import annotations

import gzip
import json
import pathlib

import pytest

from abletoolz import cli
from abletoolz.live_set import AbletonSet, plugins
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
def hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    """No real plugin dirs, no real user config."""
    monkeypatch.setattr(plugins, "default_vst_dirs", lambda: [])
    monkeypatch.setattr(cli, "load_config", lambda: AbletoolzConfig())


@pytest.mark.parametrize("key", ["9.0.1", "11.3.42", "12.2.6", "12.4.5b"])
def test_edit_flags_through_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, key: str) -> None:
    """The full edit combo saves, renames, and the written set carries every edit."""
    copy = tmp_path / "combo.als"
    copy.write_bytes((SKELETONS / f"{key}.als").read_bytes())
    code = run_cli(
        monkeypatch,
        str(copy),
        "-s",
        "-x",
        "--gradient-tracks",
        "--fold",
        "--set-track-heights",
        "68",
        "--set-track-widths",
        "120",
        "--master-out",
        "1",
        "--cue-out",
        "1",
        "--append-bars-bpm",
        "--prepend-version",
    )
    assert code == 0
    assert (tmp_path / "combo.xml").exists()  # -x dump
    assert (tmp_path / "abletoolz_backup").exists()  # original moved to backup
    saved = [p for p in tmp_path.glob("*.als") if p.name != "combo.als"]
    assert len(saved) == 1
    assert "bars_" in saved[0].name and saved[0].name.startswith(key.split("b")[0].split(".")[0])

    reloaded = AbletonSet(saved[0])
    assert reloaded.parse()
    widths = [
        el.get("Value")
        for tag in ("ViewStateSesstionTrackWidth", "ViewStateSessionTrackWidth")
        for el in reloaded.root.iter(tag)
    ]
    assert widths and all(value == "120" for value in widths)
    heights = [el.get("Value") for el in reloaded.root.iter("LaneHeight")]
    assert heights and all(value == "68" for value in heights)
    assert all(el.get("Value") == "false" for el in reloaded.root.iter("TrackUnfolded"))


def test_analysis_flags_through_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """Read-only flags run together and leave the set untouched."""
    copy = tmp_path / "readonly.als"
    original = (SKELETONS / "11.2.10.als").read_bytes()
    copy.write_bytes(original)
    code = run_cli(
        monkeypatch,
        str(copy),
        "-v",
        "--list-tracks",
        "--check-samples",
        "--check-plugins",
        "--analyze-plugins",
        "--dump-plugins",
    )
    assert code == 0
    assert copy.read_bytes() == original  # nothing written without -s


def test_db_create_and_fix_through_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """--db builds a database at --db-path; --fix-samples-absolute consumes it and repairs the set."""
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    db_path = tmp_path / "db.json"

    # Ground truth from the fixture: the first missing ref's name and stored mtime.
    probe = AbletonSet(SKELETONS / "11.2.10.als")
    assert probe.parse()
    target = probe.samples.check()[0]
    (samples_dir / target.name).write_bytes(b"\x00" * 16)

    assert run_cli(monkeypatch, "--db", "--db-path", str(db_path), str(samples_dir)) == 0
    db = json.loads(db_path.read_text(encoding="utf-8"))
    assert any(info["name"] == target.name for info in db.values())

    # Align the db entry's mtime with what the set stores so the matcher accepts it.
    for info in db.values():
        if info["name"] == target.name:
            info["last_modified"] = float(target.last_modified)
    db_path.write_text(json.dumps(db), encoding="utf-8")

    copy = tmp_path / "fixme.als"
    copy.write_bytes((SKELETONS / "11.2.10.als").read_bytes())
    assert run_cli(monkeypatch, str(copy), "--fix-samples-absolute", "--db-path", str(db_path), "-s") == 0
    reloaded = AbletonSet(copy)
    assert reloaded.parse()
    fixed = [ref for ref in reloaded.samples.iterate() if ref.absolute == samples_dir / target.name]
    assert fixed


def test_db_create_makes_missing_config_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """First run on a fresh machine: the db's parent dir doesn't exist yet and must be created.

    Regression: a fresh macOS install scanned for a minute, then crashed writing to the
    never-created Application Support dir.
    """
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    (samples_dir / "kick.wav").write_bytes(b"\x00" * 16)
    db_path = tmp_path / "fresh" / "config" / "db.json"  # parents absent
    assert run_cli(monkeypatch, "--db", "--db-path", str(db_path), str(samples_dir)) == 0
    assert db_path.exists()


def test_upgrade_plugins_through_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """--upgrade-plugins with no rules configured is a clean no-op."""
    copy = tmp_path / "upgrade.als"
    copy.write_bytes((SKELETONS / "10.1.3.als").read_bytes())
    assert run_cli(monkeypatch, str(copy), "--upgrade-plugins") == 0


def test_unfold_through_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    copy = tmp_path / "unfold.als"
    copy.write_bytes((SKELETONS / "11.3.42.als").read_bytes())
    assert run_cli(monkeypatch, str(copy), "--unfold", "-s") == 0
    saved = AbletonSet(copy)
    assert saved.parse()
    assert all(el.get("Value") == "true" for el in saved.root.iter("TrackUnfolded"))


def test_plain_xml_set_through_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """A hand-unzipped XML .als processes through the CLI like any other set."""
    xml_copy = tmp_path / "unzipped.als"
    xml_copy.write_bytes(gzip.decompress((SKELETONS / "12.2.6.als").read_bytes()))
    assert run_cli(monkeypatch, str(xml_copy), "--list-tracks") == 0
