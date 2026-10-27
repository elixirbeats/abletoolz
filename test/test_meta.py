"""Per-set sidecars: what a scan writes down, what it reuses, and what it never touches.

Most of it drives ``run_set`` directly with a ``SetJob`` and a fixed scan
fingerprint, since writing and reusing a sidecar is what one set's run does. A
few drive ``cli.main()`` end to end, to show the command line's flags and the
batch's fingerprint reach it. Kept hermetic by the suite's ``locations``: no
plugin folders, no Live database, a scratch config folder.
"""

from __future__ import annotations

import hashlib
import pathlib
from typing import Any

import pytest
import yaml
from conftest import SKELETONS, copy_set, run_cli

from abletoolz.live_set import AbletonSet, find_sets, persistence
from abletoolz.machine import Locations
from abletoolz.plugins.identity import plugin_db
from abletoolz.run import meta
from abletoolz.run.job import SetJob, run_set

# Any fixed value: the cache reuses a scan only under the fingerprint it was made with.
FINGERPRINT = "test inputs"
PLUGINS = SetJob(check_plugins=True)
# What checking the 11.3.42 fixture's plugins finds with no plugin folders and no Live database.
MISSING = {"p39730.dll": 1, "p29892.dll": 1, "Decapitator": 1}
# An answer no check gives, planted in a sidecar to tell a reused scan from a fresh one.
PLANTED = {"planted": 1}


def run(job: SetJob, path: pathlib.Path) -> None:
    assert run_set(job, path, fingerprint=FINGERPRINT).error is None


def sidecar(set_path: pathlib.Path) -> dict[str, Any]:
    document = yaml.safe_load(meta.sidecar_path(set_path).read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def plant(set_path: pathlib.Path, **stale: Any) -> None:
    """Rewrite the scan block beside ``set_path``; a run that reuses it gives itself away by answering ``PLANTED``."""
    document = meta.read(set_path)
    assert document is not None and document.scan is not None
    meta.write(set_path, document.scan.model_copy(update={"plugins_missing": PLANTED, **stale}))


# -- what a scan writes down -------------------------------------------------


def test_a_scan_leaves_what_it_found_beside_the_set(tmp_path: pathlib.Path) -> None:
    """The machine zone is the run's answers, and the hash is of what it read."""
    copy = copy_set(tmp_path, "11.3.42")
    assert run_cli(str(copy), "--check-plugins", "--check-samples") == 0

    document = sidecar(copy)
    scan = document["scan"]
    assert scan["set_hash"] == hashlib.sha256(copy.read_bytes()).hexdigest()
    assert scan["scanned_with"] == meta.SCANNER
    assert scan["live_version"] == "Ableton Live 11.3.42"
    assert scan["bpm"] == 120.0
    assert scan["bars"] == 0
    # With no plugin dirs and no Live database, nothing in the fixture resolves.
    assert scan["plugins_missing"] == MISSING
    assert scan["samples_missing"] == 0
    # Nothing in this run fixed anything, which is not the same as fixing nothing.
    assert scan["plugins_fixed"] is None
    assert document["status"] is None and document["notes"] is None


def test_a_run_that_scans_nothing_writes_no_sidecar(tmp_path: pathlib.Path) -> None:
    """Listing tracks is not a scan; there is nothing to write down."""
    copy = copy_set(tmp_path, "11.3.42")
    run(SetJob(list_tracks=True), copy)
    assert not meta.sidecar_path(copy).exists()


def test_no_meta_writes_no_sidecar(tmp_path: pathlib.Path) -> None:
    copy = copy_set(tmp_path, "11.3.42")
    assert run_cli(str(copy), "--check-plugins", "--no-meta") == 0
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


def test_lives_own_backup_folder_gets_no_sidecar(tmp_path: pathlib.Path) -> None:
    """Ableton's Backup/ counts too, not just the folder abletoolz writes.

    A set in there reached the sidecar writer only when named directly on the
    command line -- directory walks already skipped it -- and the writer used
    to accept it, leaving files inside Live's own backup folder.
    """
    scan = meta.SetScan(scanned=meta.now(), scanned_with=meta.SCANNER, set_hash="abc")
    # Each spelling gets its own root: Windows would fold "Backup" and "backup"
    # into one directory, so a shared parent would only ever test one of them.
    for index, folder in enumerate(("Backup", "backup")):
        live_backup = tmp_path / str(index) / folder / "MyTune [2024-01-02 143000].als"
        live_backup.parent.mkdir(parents=True)
        assert meta.write(live_backup, scan) is None
    assert not list(tmp_path.rglob("*.meta.yaml"))


def test_a_directly_named_backup_is_not_collected(tmp_path: pathlib.Path) -> None:
    """The collector and the sidecar writer answer the same question the same way."""
    for index, folder in enumerate(("Backup", "backup", "abletoolz_backup")):
        inside = tmp_path / str(index) / folder / "old.als"
        inside.parent.mkdir(parents=True)
        inside.write_bytes(b"")
        assert find_sets([str(inside)]) == []
        assert not meta.describes_a_real_set(inside)

    real = tmp_path / "real.als"
    real.write_bytes(b"")
    assert find_sets([str(real)]) == [real]
    assert meta.describes_a_real_set(real)


def test_a_set_named_backup_is_still_a_set(tmp_path: pathlib.Path) -> None:
    """Only parent directories are checked -- "Backup.als" is a filename, not a folder."""
    named = tmp_path / "Backup.als"
    named.write_bytes(b"")
    assert find_sets([str(named)]) == [named]
    assert meta.describes_a_real_set(named)


# -- the user's half ---------------------------------------------------------


def test_a_rescan_carries_the_human_zone_through_verbatim(tmp_path: pathlib.Path) -> None:
    """status, notes and anything else the user put there survive being rescanned."""
    copy = copy_set(tmp_path, "11.3.42")
    run(PLUGINS, copy)

    path = meta.sidecar_path(copy)
    document = sidecar(copy)
    document["status"] = "needs work"
    document["notes"] = "reverb tail runs long in the breakdown"
    document["rating"] = 4
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    # A different set under the same name: the machine zone must be replaced.
    copy.write_bytes((SKELETONS / "12.2.6.als").read_bytes())
    run(PLUGINS, copy)

    rescanned = sidecar(copy)
    assert rescanned["status"] == "needs work"
    assert rescanned["notes"] == "reverb tail runs long in the breakdown"
    assert rescanned["rating"] == 4
    assert rescanned["scan"]["set_hash"] == hashlib.sha256(copy.read_bytes()).hexdigest()
    assert rescanned["scan"]["live_version"] == "Ableton Live 12.2.6"


def test_an_unreadable_sidecar_is_ignored_and_replaced(tmp_path: pathlib.Path) -> None:
    """A sidecar nothing can parse is no sidecar: the run says so and writes its own."""
    copy = copy_set(tmp_path, "11.3.42")
    meta.sidecar_path(copy).write_text("scan: [this is not a scan block", encoding="utf-8")
    run(PLUGINS, copy)
    assert sidecar(copy)["scan"]["scanned_with"] == meta.SCANNER


# -- the cache ---------------------------------------------------------------


def test_the_same_bytes_and_inputs_are_not_scanned_twice(tmp_path: pathlib.Path) -> None:
    """The second pass over an unchanged library answers out of the sidecars."""
    copy = copy_set(tmp_path, "11.3.42")
    assert run_cli(str(copy), "--check-plugins") == 0
    plant(copy)
    assert run_cli(str(copy), "--check-plugins") == 0
    assert sidecar(copy)["scan"]["plugins_missing"] == PLANTED


def test_a_check_the_sidecar_answers_never_opens_the_set(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parsing is most of a scan, so an unchanged set is answered without it; asking anything else opens it."""
    copy = copy_set(tmp_path, "11.3.42")
    run(PLUGINS, copy)
    plant(copy)
    before = meta.sidecar_path(copy).read_bytes()
    parsed: list[pathlib.Path] = []
    real_load = persistence.load_set

    def counting_load(live_set: AbletonSet) -> str | None:
        parsed.append(live_set.path)
        return real_load(live_set)

    monkeypatch.setattr(persistence, "load_set", counting_load)
    record = run_set(PLUGINS, copy, fingerprint=FINGERPRINT)
    assert record.plugins_missing == PLANTED and parsed == []
    assert meta.sidecar_path(copy).read_bytes() == before
    # A sample check always opens the set.
    run(SetJob(check_plugins=True, check_samples=True), copy)
    assert parsed == [copy]
    run(SetJob(check_plugins=True, list_tracks=True), copy)
    assert parsed == [copy, copy]


@pytest.mark.parametrize(
    ("stale", "fingerprint"),
    [
        ({"set_hash": "0" * 64}, FINGERPRINT),
        ({"scanned_with": "abletoolz 0.0.1 scan 0"}, FINGERPRINT),
        ({"scan_fingerprint": "other inputs"}, FINGERPRINT),
        ({"scan_fingerprint": None}, FINGERPRINT),
        ({}, None),
    ],
    ids=["other bytes", "another abletoolz", "other inputs", "before fingerprints", "unreadable inputs"],
)
def test_a_scan_of_anything_else_is_not_reused(
    tmp_path: pathlib.Path, stale: dict[str, Any], fingerprint: str | None
) -> None:
    """Other bytes, another abletoolz, other plugins, config or support files: each can change the answer.

    So can inputs this run could not read, which is no fingerprint at all.
    """
    copy = copy_set(tmp_path, "11.3.42")
    run(PLUGINS, copy)
    plant(copy, **stale)
    assert run_set(PLUGINS, copy, fingerprint=fingerprint).plugins_missing == MISSING
    assert sidecar(copy)["scan"]["scanned_with"] == meta.SCANNER


def test_a_rebuilt_plugin_database_rescans_unchanged_sets(tmp_path: pathlib.Path, locations: Locations) -> None:
    """The command line's fingerprint covers the computer's plugin database, so building one makes old answers stale."""
    copy = copy_set(tmp_path, "11.3.42")
    assert run_cli(str(copy), "--check-plugins") == 0
    plant(copy)
    plugin_db.write_plugin_db(plugin_db.PluginDatabase(built=meta.now()), locations.plugin_db)
    assert run_cli(str(copy), "--check-plugins") == 0
    assert sidecar(copy)["scan"]["plugins_missing"] == MISSING


def test_a_sample_check_never_answers_from_the_sidecar(tmp_path: pathlib.Path) -> None:
    """Sample files are outside the cache key, so a sample deleted or unplugged since the last scan must still show."""
    copy = copy_set(tmp_path, "11.3.42")
    samples = SetJob(check_samples=True)
    run(samples, copy)
    plant(copy, samples_missing_by_name=PLANTED)
    assert run_set(samples, copy, fingerprint=FINGERPRINT).samples_missing == 0
    assert sidecar(copy)["scan"]["samples_missing_by_name"] == {}


def test_passes_with_different_flags_accumulate(tmp_path: pathlib.Path) -> None:
    """A cache hit keeps what the run did not measure and adds what it did."""
    copy = copy_set(tmp_path, "11.3.42")
    run(SetJob(check_samples=True), copy)
    assert sidecar(copy)["scan"]["plugins_missing"] is None

    run(PLUGINS, copy)
    scan = sidecar(copy)["scan"]
    assert scan["plugins_missing"] == MISSING
    assert scan["samples_missing"] == 0  # carried over from the samples pass


# -- where it goes -----------------------------------------------------------


def test_output_keeps_every_write_away_from_the_original(tmp_path: pathlib.Path) -> None:
    """With --output the sidecar goes with the copy, and the original's folder is untouched."""
    library = tmp_path / "library"
    library.mkdir()
    copy = copy_set(library, "11.3.42", "repair.als")
    original = copy.read_bytes()
    quarantine = tmp_path / "quarantine"

    run(SetJob(gradient_tracks=True, check_plugins=True, save=True, output=quarantine), copy)

    assert [path.name for path in sorted(library.iterdir())] == ["repair.als"]
    assert copy.read_bytes() == original
    written = quarantine / "repair.als"
    assert AbletonSet(written).parse()
    document = yaml.safe_load(meta.sidecar_path(written).read_text(encoding="utf-8"))
    assert document["scan"]["set_hash"] == hashlib.sha256(original).hexdigest()


def test_an_output_sidecar_carries_the_original_s_notes(tmp_path: pathlib.Path) -> None:
    """The copy inherits what the user wrote about the set it came from."""
    library = tmp_path / "library"
    library.mkdir()
    copy = copy_set(library, "11.3.42", "repair.als")
    meta.sidecar_path(copy).write_text("status: keeper\nnotes: mixed in 2019\n", encoding="utf-8")
    quarantine = tmp_path / "quarantine"

    run(SetJob(gradient_tracks=True, check_plugins=True, save=True, output=quarantine), copy)

    document = yaml.safe_load(meta.sidecar_path(quarantine / "repair.als").read_text(encoding="utf-8"))
    assert document["status"] == "keeper"
    assert document["notes"] == "mixed in 2019"


def test_a_renamed_save_takes_its_sidecar_with_it(tmp_path: pathlib.Path) -> None:
    """A save that lands under another name, here beside a set already in --output, takes its sidecar along."""
    copy = copy_set(tmp_path, "11.3.42")
    output = tmp_path / "out"
    output.mkdir()
    (output / "set.als").write_bytes(copy.read_bytes())
    run(SetJob(gradient_tracks=True, check_plugins=True, save=True, output=output), copy)
    saved = output / "set__1.als"
    assert saved.exists()
    assert meta.sidecar_path(saved).exists()
    assert not meta.sidecar_path(output / "set.als").exists()


def test_an_xml_dump_with_output_goes_with_the_copy(tmp_path: pathlib.Path) -> None:
    """-x beside --output writes nothing next to the original, not even a backup of an old dump."""
    library = tmp_path / "library"
    library.mkdir()
    copy = copy_set(library, "11.3.42", "repair.als")
    (library / "repair.xml").write_text("an old dump", encoding="utf-8")
    quarantine = tmp_path / "quarantine"

    run(SetJob(gradient_tracks=True, xml=True, save=True, output=quarantine), copy)

    assert sorted(path.name for path in library.iterdir()) == ["repair.als", "repair.xml"]
    assert (library / "repair.xml").read_text(encoding="utf-8") == "an old dump"
    assert (quarantine / "repair.xml").read_bytes().startswith(b"<?xml")


def test_a_set_and_a_clip_of_one_name_keep_their_own_sidecars(tmp_path: pathlib.Path) -> None:
    """Song.als keeps Song.meta.yaml, so old sidecars still load; Song.alc beside it gets one of its own."""
    scan = meta.SetScan(scanned=meta.now(), scanned_with=meta.SCANNER, set_hash="set")
    song, clip = tmp_path / "Song.als", tmp_path / "Song.alc"
    meta.write(song, scan)
    meta.write(clip, scan.model_copy(update={"set_hash": "clip"}))

    assert meta.sidecar_path(song) == tmp_path / "Song.meta.yaml"
    assert meta.sidecar_path(clip) == tmp_path / "Song.alc.meta.yaml"
    for path, digest in ((song, "set"), (clip, "clip")):
        document = meta.read(path)
        assert document is not None and document.scan is not None
        assert document.scan.set_hash == digest
