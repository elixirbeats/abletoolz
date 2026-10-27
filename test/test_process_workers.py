"""Spawned CLI workers preserve output boundaries and use a shared catalog revision."""

from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import subprocess
import sys

import pytest

from abletoolz.live_set import find_sets
from abletoolz.samples import database
from abletoolz.samples.catalog import SampleCatalog

SKELETON = pathlib.Path(__file__).parent / "version_fixtures" / "skeletons" / "11.3.42.als"


def fixture_sets(directory: pathlib.Path) -> list[pathlib.Path]:
    sets = [directory / f"song-{index}.als" for index in range(2)]
    for path in sets:
        shutil.copyfile(SKELETON, path)
    return sets


def hashes(paths: list[pathlib.Path]) -> list[str]:
    return [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]


def test_spawned_workers_return_whole_json_documents(tmp_path: pathlib.Path, child_env: dict[str, str]) -> None:
    sets = fixture_sets(tmp_path)
    before = hashes(sets)
    result = subprocess.run(
        [sys.executable, "-m", "abletoolz", "-j", "2", "--describe", "patterns", *(str(path) for path in sets)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        env=child_env,
    )
    documents = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(documents) == 2
    assert all(set(document) == {"set", "tracks", "patterns", "session", "arrangement"} for document in documents)
    assert "Using 2 set worker(s)" in result.stderr
    assert hashes(sets) == before


def test_overlapping_inputs_and_file_aliases_are_processed_once(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sets = fixture_sets(tmp_path)
    alias = tmp_path / "alias.als"
    alias.hardlink_to(sets[0])
    monkeypatch.chdir(tmp_path)
    selected = find_sets([str(sets[0]), sets[0].name, str(tmp_path)])
    assert len(selected) == 2
    assert selected[0] == sets[0]


def test_process_workers_can_read_the_catalog_without_saving_sets(
    tmp_path: pathlib.Path, child_env: dict[str, str]
) -> None:
    sets = fixture_sets(tmp_path)
    before = hashes(sets)
    library = tmp_path / "empty-library"
    library.mkdir()
    db_path = database.create_or_update_db([str(library)], tmp_path / "catalog.sqlite3", workers=1)
    catalog_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "abletoolz",
            "-j",
            "2",
            "--fix-samples-absolute",
            "--no-meta",
            "--db-path",
            str(db_path),
            *(str(path) for path in sets),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        env=child_env,
    )
    assert hashes(sets) == before
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == catalog_hash
    assert not list(tmp_path.glob("*.meta.yaml"))
    assert not (tmp_path / "abletoolz_backup").exists()


def test_backup_copies_the_pinned_revision_and_search_index(tmp_path: pathlib.Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    sample = library / "kick.wav"
    sample.write_bytes(b"first sample")
    db_path = database.create_or_update_db([str(library)], tmp_path / "catalog.sqlite3", workers=1)
    with database.load_db(db_path) as pinned:
        assert isinstance(pinned, SampleCatalog)
        previous = dict(pinned.candidates("kick.wav")[str(sample)])
        sample.write_bytes(b"a different sample with another size")
        database.create_or_update_db([str(library)], db_path, workers=1)
        snapshot = tmp_path / "snapshot.sqlite3"
        pinned.backup_to(snapshot)
        with database.load_db(snapshot) as copied:
            assert isinstance(copied, SampleCatalog)
            assert dict(copied.candidates("kick.wav")[str(sample)]) == previous
            assert copied.search("kick") == (str(sample),)
        with pytest.raises(FileExistsError):
            pinned.backup_to(snapshot)
    with database.load_db(db_path) as latest:
        assert latest.candidates("kick.wav")[str(sample)]["size"] != previous["size"]
