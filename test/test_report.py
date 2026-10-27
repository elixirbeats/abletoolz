"""The run report: one record per set, what was fixed in it, and the totals over it.

Same hermetic CLI drive as the rest of the suite, and ``run_set`` directly for
what one set's record says. The point of every assertion here is that the JSON
says what the console said.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
from collections import Counter
from typing import Any

import pytest
from conftest import copy_set, run_cli

from abletoolz.formats import live_document
from abletoolz.live_set import plugins
from abletoolz.live_set.files import Relinked
from abletoolz.machine import Locations
from abletoolz.plugins.config import AbletoolzConfig
from abletoolz.plugins.container.device import PluginDevices
from abletoolz.plugins.loadable import LoadableOracle
from abletoolz.run import meta, report
from abletoolz.run.job import Findings, SetJob, run_set
from abletoolz.samples.catalog import DatabaseT, SampleCatalog
from abletoolz.samples.relink import Refusal


def sample_db(path: pathlib.Path, records: DatabaseT) -> pathlib.Path:
    """A sample database at ``path`` holding exactly ``records``."""
    with SampleCatalog.from_records(records) as catalog:
        catalog.backup_to(path)
    return path


def read_report(directory: pathlib.Path) -> dict[str, Any]:
    written = list(directory.glob("abletoolz_report_*.json"))
    assert len(written) == 1, f"expected one report in {directory}, found {written}"
    document = json.loads(written[0].read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


# -- the record --------------------------------------------------------------


def test_the_report_holds_one_record_per_set(tmp_path: pathlib.Path) -> None:
    """Every set the run walked over is in it, whether or not anything happened to it."""
    copy_set(tmp_path, "11.3.42", "first.als")
    copy_set(tmp_path, "12.2.6", "second.als")
    assert run_cli(str(tmp_path), "--check-plugins") == 0

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


def test_spawned_workers_get_the_runs_locations_and_fingerprint(tmp_path: pathlib.Path, locations: Locations) -> None:
    """Every worker checks plugins on the run's locations and caches under the run's one fingerprint.

    So a plugin in the run's folder counts in every set and nothing else does,
    and every sidecar matches what the next run on these inputs computes.
    """
    sets = tmp_path / "sets"
    sets.mkdir()
    paths = [copy_set(sets, "11.3.42", "first.als"), copy_set(sets, "11.3.42", "second.als")]
    folder = tmp_path / "VstPlugins"
    folder.mkdir()
    (folder / "p39730.dll").write_bytes(b"")
    here = dataclasses.replace(locations, vst_dirs=(folder,))
    assert run_cli(str(sets), "--check-plugins", "--jobs", "2", locations=here) == 0

    missing = [record["plugins_missing"] for record in read_report(sets)["sets"]]
    assert missing == [{"p29892.dll": 1, "Decapitator": 1}] * 2
    fingerprint = meta.scan_fingerprint(AbletoolzConfig(), here)
    assert fingerprint is not None
    for path in paths:
        document = meta.read(path)
        assert document is not None and document.scan is not None
        assert document.scan.scan_fingerprint == fingerprint


def test_a_set_that_cannot_be_read_is_a_record_with_an_error(tmp_path: pathlib.Path) -> None:
    """A run that fails still says what failed, in the same file as everything else."""
    copy_set(tmp_path, "11.3.42", "good.als")
    (tmp_path / "corrupt.als").write_bytes(b"not a gzip file")
    (tmp_path / "live8.als").write_bytes(live_document.PRE_8_2_MAGIC + bytes(30))
    assert run_cli(str(tmp_path), "--check-plugins") == 1

    document = read_report(tmp_path)
    errors = {pathlib.Path(record["path"]).name: record["error"] for record in document["sets"]}
    assert errors["good.als"] is None
    assert errors["corrupt.als"] == "not a Live document: it opens with 6e6f, neither gzip nor XML"
    assert errors["live8.als"] == "a document from before Live 8.2, which is not supported"
    assert document["totals"]["failed"] == 2


# -- what was fixed ----------------------------------------------------------


def test_a_relinked_plugin_sample_says_which_device_and_what_did_it(tmp_path: pathlib.Path) -> None:
    """A Serato Sample pointing at a file that moved, relinked through the sample database."""
    found = tmp_path / "library" / "kick.wav"
    found.parent.mkdir()
    found.write_bytes(b"RIFF")
    database = sample_db(tmp_path / "db.sqlite3", {str(found): {"name": "kick.wav", "size": 4}})
    copy = copy_set(tmp_path, "11.3.42", "relink.als")
    root = live_document.read(copy)
    device = next(item for item in PluginDevices(root) if item.identity.name == "Serum_x64")
    device.set_name("Serato Sample")
    device.set_unique_id(1399681132)
    device.set_state(b'{"project":{"sourceSong":{"File":"D:/Gone/kick.wav"}}}')
    copy.write_bytes(live_document.encode(root))

    assert run_cli(str(copy), "--fix-samples-absolute", "--db-path", str(database), "-s") == 0

    document = read_report(tmp_path)
    record = document["sets"][0]
    assert record["changed"] is True
    assert record["relinks"] == [
        {"old": "D:/Gone/kick.wav", "new": str(found), "device": "Serato Sample", "track": "1-Serum_x64"}
    ]
    assert document["totals"]["relinks"] == 1


def test_a_relinked_live_sample_reference_is_in_the_record_too(tmp_path: pathlib.Path) -> None:
    """Live's own references used to be relinked without a word in the record."""
    found = tmp_path / "library" / "p45753.wav"
    found.parent.mkdir()
    found.write_bytes(b"RIFF")
    record = {"name": found.name, "size": 2144028, "last_modified": 1425359424.0}
    database = sample_db(tmp_path / "db.sqlite3", {str(found): record})
    copy = copy_set(tmp_path, "12.2.6", "samples.als")

    assert run_cli(str(copy), "--fix-samples-absolute", "--db-path", str(database), "--no-meta") == 0

    relinks = read_report(tmp_path)["sets"][0]["relinks"]
    assert [(fix["new"], fix["device"]) for fix in relinks] == [(str(found), None)]


def test_a_file_left_as_it_was_says_why() -> None:
    """A relink pass's refusals reach the record and the totals as they were given."""
    refusal = Refusal("kick.wav for Serato Sample on 1-MIDI", "several files match")
    findings = Findings(relinked=Relinked([], None, [refusal]))
    record = findings.record(pathlib.Path("set.als"), written=None, changed=False)
    assert [(item.what, item.why) for item in record.refused] == [(refusal.what, refusal.why)]
    assert report.totals([record, record]).refused == 2


def test_collecting_without_saving_copies_nothing(tmp_path: pathlib.Path) -> None:
    found = tmp_path / "library" / "kick.wav"
    found.parent.mkdir()
    found.write_bytes(b"RIFF")
    database = sample_db(tmp_path / "db.sqlite3", {str(found): {"name": "kick.wav", "size": 4}})
    project = tmp_path / "Song Project"
    (project / "Ableton Project Info").mkdir(parents=True)
    copy = copy_set(project, "11.3.42", "collect.als")
    root = live_document.read(copy)
    device = next(item for item in PluginDevices(root) if item.identity.name == "Serum_x64")
    device.set_name("Serato Sample")
    device.set_unique_id(1399681132)
    device.set_state(b'{"project":{"sourceSong":{"File":"D:/Gone/kick.wav"}}}')
    copy.write_bytes(live_document.encode(root))
    imported = project / "Samples" / "Imported" / "kick.wav"
    args = (str(copy), "--fix-samples-collect", "--db-path", str(database), "--no-meta")

    assert run_cli(*args) == 0
    assert not imported.exists()

    assert run_cli(*args, "-s") == 0
    assert imported.read_bytes() == b"RIFF"


def test_an_unexpected_error_in_one_set_is_that_sets_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    good = copy_set(tmp_path, "11.3.42", "good.als")
    bad = copy_set(tmp_path, "11.3.42", "bad.als")
    scan = plugins.Plugins.scan

    def flaky(self: plugins.Plugins, oracle: LoadableOracle) -> list[plugins.PluginRef]:
        if self._set.path.name == bad.name:
            raise ValueError("a patch nobody measured")
        return scan(self, oracle)

    monkeypatch.setattr(plugins.Plugins, "scan", flaky)
    # One job keeps the sets in this process, where the patched scan is.
    assert run_cli(str(good), str(bad), "--check-plugins", "--no-meta", "--jobs", "1") == 1

    records = {pathlib.Path(record["path"]).name: record for record in read_report(tmp_path)["sets"]}
    assert records["good.als"]["error"] is None
    assert records["bad.als"]["error"] == "ValueError: a patch nobody measured"


def test_a_run_that_changes_nothing_says_so(tmp_path: pathlib.Path) -> None:
    copy = copy_set(tmp_path, "11.3.42", "steady.als")
    record = run_set(SetJob(check_plugins=True, meta=False), copy)
    assert record.changed is False
    assert record.relinks == []
    assert record.written is None


def test_an_edit_that_serializes_differently_is_a_change(tmp_path: pathlib.Path) -> None:
    """Changed means the tree the run would write is not the one it read, saved or not."""
    copy = copy_set(tmp_path, "11.3.42", "edited.als")
    original = copy.read_bytes()
    record = run_set(SetJob(set_track_heights=40), copy)
    assert record.changed is True
    assert record.written is None
    assert copy.read_bytes() == original


def test_the_record_and_the_sidecar_are_the_same_findings(tmp_path: pathlib.Path) -> None:
    """One check of the set, read twice: the counts in the record are the sidecar's."""
    copy = copy_set(tmp_path, "12.2.6", "both.als")
    record = run_set(SetJob(check_plugins=True, check_samples=True), copy, fingerprint="inputs")
    document = meta.read(copy)
    assert document is not None and document.scan is not None
    assert record.samples_missing == document.scan.samples_missing == 2
    assert document.scan.samples_missing_by_name == {"p45753.wav": 1, "p88340.wav": 1}
    assert record.plugins_missing == document.scan.plugins_missing
    assert record.plugins_missing


# -- where it goes -----------------------------------------------------------


def test_the_report_follows_output(tmp_path: pathlib.Path) -> None:
    """With --output nothing is written next to the originals, the report included."""
    library = tmp_path / "library"
    library.mkdir()
    copy = copy_set(library, "11.3.42", "repair.als")
    quarantine = tmp_path / "quarantine"

    assert run_cli(str(copy), "--gradient-tracks", "-s", "--output", str(quarantine)) == 0

    assert not list(library.glob("abletoolz_report_*.json"))
    document = read_report(quarantine)
    assert document["sets"][0]["path"] == str(copy)
    assert document["sets"][0]["written"] == str(quarantine / "repair.als")
