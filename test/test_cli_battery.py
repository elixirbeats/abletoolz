"""Every CLI flag exercised end-to-end through ``cli.main()`` on skeleton fixtures.

The version matrix proves the domain logic per Live version; this file proves the
CLI wiring above it — argument plumbing, save/rename behavior, exit codes. Kept
hermetic: the suite's ``locations`` stand in for this computer, with no plugin
folders, no Live database and a scratch config folder.
"""

from __future__ import annotations

import dataclasses
import gzip
import json
import pathlib
import sqlite3
from contextlib import closing

from conftest import SKELETONS, copy_set, run_cli, skeleton_set

from abletoolz.live_set import AbletonSet
from abletoolz.machine import Locations
from abletoolz.plugins.identity import plugin_db


def test_edit_flags_through_cli(tmp_path: pathlib.Path) -> None:
    """The full edit combo saves, renames, and the written set carries every edit.

    One version: the version matrix proves each edit on every version, this the flags reaching it.
    """
    copy = copy_set(tmp_path, "11.3.42", "combo.als")
    code = run_cli(
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
    )
    assert code == 0
    assert (tmp_path / "combo.xml").exists()  # -x dump
    assert (tmp_path / "abletoolz_backup").exists()  # original moved to backup
    reloaded = AbletonSet(copy)
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


def test_analysis_flags_through_cli(tmp_path: pathlib.Path) -> None:
    """Read-only flags run together and leave the set untouched."""
    copy = tmp_path / "readonly.als"
    original = (SKELETONS / "11.2.10.als").read_bytes()
    copy.write_bytes(original)
    code = run_cli(
        str(copy),
        "-v",
        "--list-tracks",
        "--check-samples",
        "--check-plugins",
        "--dump-plugins",
    )
    assert code == 0
    assert copy.read_bytes() == original  # nothing written without -s


def test_db_create_and_fix_through_cli(tmp_path: pathlib.Path) -> None:
    """--db builds a database at --db-path; --fix-samples-absolute consumes it and repairs the set."""
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    db_path = tmp_path / "db.sqlite3"

    # Ground truth from the fixture: the first missing ref's name and stored mtime.
    probe = skeleton_set("11.2.10")
    target = probe.samples.check()[0]
    (samples_dir / target.name).write_bytes(b"\x00" * 16)

    assert run_cli("--db", "--db-path", str(db_path), str(samples_dir)) == 0
    with closing(sqlite3.connect(db_path)) as connection, connection:
        # Align the db entry with what the set stores so the matcher accepts it: a
        # stored size that disagrees would rule the file out whatever its date says.
        aligned = connection.execute(
            "UPDATE samples SET last_modified=?, size=?, crc=NULL WHERE name=?",
            (float(target.last_modified), target.size, target.name),
        )
        assert aligned.rowcount == 1

    copy = copy_set(tmp_path, "11.2.10", "fixme.als")
    assert run_cli(str(copy), "--fix-samples-absolute", "--db-path", str(db_path), "-s") == 0
    reloaded = AbletonSet(copy)
    assert reloaded.parse()
    fixed = [ref for ref in reloaded.samples.iterate() if ref.absolute == samples_dir / target.name]
    assert fixed


def test_db_create_makes_missing_config_dir(tmp_path: pathlib.Path) -> None:
    """First run on a fresh machine: the db's parent dir doesn't exist yet and must be created.

    Regression: a fresh macOS install scanned for a minute, then crashed writing to the
    never-created Application Support dir.
    """
    samples_dir = tmp_path / "samples"
    samples_dir.mkdir()
    (samples_dir / "kick.wav").write_bytes(b"\x00" * 16)
    db_path = tmp_path / "fresh" / "config" / "db.sqlite3"  # parents absent
    assert run_cli("--db", "--db-path", str(db_path), str(samples_dir)) == 0
    assert db_path.exists()


def test_plugin_db_through_cli(tmp_path: pathlib.Path, locations: Locations) -> None:
    """--plugin-db reads this machine's plugins into a database at --plugin-db-path."""
    folder = tmp_path / "VST3"
    contents = folder / "Thing.vst3" / "Contents"
    contents.mkdir(parents=True)
    module = {"Classes": [{"CID": "00" * 16, "Category": "Audio Module Class", "Name": "Thing"}]}
    (contents / "moduleinfo.json").write_text(json.dumps(module), encoding="utf-8")
    db_path = tmp_path / "plugin_db.json"

    here = dataclasses.replace(locations, vst_dirs=(folder,))
    assert run_cli("--plugin-db", "--plugin-db-path", str(db_path), locations=here) == 0
    database = plugin_db.read_plugin_db(db_path)
    assert [entry.name for entry in database.plugins] == ["Thing"]


def test_plugin_db_takes_no_set_and_no_other_command(tmp_path: pathlib.Path) -> None:
    """A machine command, like --db: mixing it with set work is a usage error."""
    copy = copy_set(tmp_path, "11.3.42", "combo.als")
    assert run_cli("--plugin-db", str(copy)) == 2
    assert run_cli("--plugin-db", "--db") == 2


def test_output_dir_suffixes_on_collision(tmp_path: pathlib.Path) -> None:
    """An already-taken DIR/<name> is kept; new saves land beside it with the backup __N suffixes."""
    copy = copy_set(tmp_path, "11.3.42", "repair.als")
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()
    stranger = b"not a set, must survive"
    (quarantine / "repair.als").write_bytes(stranger)
    assert run_cli(str(copy), "--gradient-tracks", "-s", "--output", str(quarantine)) == 0
    assert run_cli(str(copy), "--gradient-tracks", "-s", "--output", str(quarantine)) == 0
    assert (quarantine / "repair.als").read_bytes() == stranger
    for suffixed in ("repair__1.als", "repair__2.als"):
        saved = AbletonSet(quarantine / suffixed)
        assert saved.parse()


def test_output_dir_skips_unchanged_set(tmp_path: pathlib.Path) -> None:
    """A set the run doesn't change is not written to the output dir at all.

    The run report lands there regardless -- it is the record of the run, not of
    a set -- so the output dir exists; nothing of the set is in it.
    """
    copy = tmp_path / "steady.als"
    original = (SKELETONS / "11.3.42.als").read_bytes()
    copy.write_bytes(original)
    quarantine = tmp_path / "quarantine"
    # Checking plugins edits nothing, so there is nothing to write.
    assert run_cli(str(copy), "--check-plugins", "-s", "--output", str(quarantine)) == 0
    assert copy.read_bytes() == original
    assert not list(quarantine.glob("*.als"))
    # Nor a sidecar, there or beside the original.
    assert not list(quarantine.glob("*.meta.yaml"))
    assert not list(tmp_path.glob("*.meta.yaml"))


def test_unfold_through_cli(tmp_path: pathlib.Path) -> None:
    copy = copy_set(tmp_path, "11.3.42", "unfold.als")
    assert run_cli(str(copy), "--unfold", "-s") == 0
    saved = AbletonSet(copy)
    assert saved.parse()
    assert all(el.get("Value") == "true" for el in saved.root.iter("TrackUnfolded"))


def test_plain_xml_set_through_cli(tmp_path: pathlib.Path) -> None:
    """A hand-unzipped XML .als processes through the CLI like any other set."""
    xml_copy = tmp_path / "unzipped.als"
    xml_copy.write_bytes(gzip.decompress((SKELETONS / "12.2.6.als").read_bytes()))
    assert run_cli(str(xml_copy), "--list-tracks") == 0
