"""One set, start to finish: what the command line asked of it, and what doing that found.

``SetJob`` is the part of a command line that is about a single set, decided
once per run and picklable, so every spawned worker gets the same one.
``run_set`` reads one set, edits it, measures it, saves it and answers its run
record. ``Findings`` is what that run measured, held once: the sidecar scan and
the run record are two readings of the same values, not two computations of
them.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import pathlib
from typing import Any

from abletoolz.ansi import B, C, Y
from abletoolz.live_set import AbletonSet, persistence
from abletoolz.live_set.describe import DescribeLevel, describe_json
from abletoolz.live_set.files import Relinked
from abletoolz.live_set.transport import SetLength
from abletoolz.machine import Locations
from abletoolz.plugins import loadable
from abletoolz.run import console, meta, report
from abletoolz.run.meta import SetScan
from abletoolz.samples.catalog import SampleCatalog
from abletoolz.schema import Tag

logger = logging.getLogger(__name__)

# Field metadata keys. A command is a flag that asks something of a set; an
# edit is a command that changes the set in memory, which is what makes the
# run compare the tree before and after, and remind the user nothing was saved.
_COMMAND = "command"
_EDITS = "edits"


def _edit(default: Any) -> Any:
    return dataclasses.field(default=default, metadata={_COMMAND: True, _EDITS: True})


def _command(default: Any) -> Any:
    return dataclasses.field(default=default, metadata={_COMMAND: True})


@dataclasses.dataclass(frozen=True)
class SetJob:
    """What to do to each set of a run, decided once from the command line.

    Every field is named for the argparse destination it comes from, so
    ``from_args`` is a copy and the CLI's lists of edit and set commands are
    read off these fields rather than kept beside them.
    """

    master_out: int | None = _edit(None)
    cue_out: int | None = _edit(None)
    fold: bool = _edit(False)
    unfold: bool = _edit(False)
    set_track_heights: int | None = _edit(None)
    set_track_widths: int | None = _edit(None)
    gradient_tracks: bool = _edit(False)
    fix_samples_absolute: bool = _edit(False)
    fix_samples_collect: bool = _edit(False)

    list_tracks: bool = _command(False)
    describe: str | None = _command(None)
    check_samples: bool = _command(False)
    check_plugins: bool = _command(False)
    dump_plugins: bool = _command(False)
    xml: bool = _command(False)
    save: bool = _command(False)

    # Where a saved set goes instead of in place, and whether sidecars are kept.
    output: pathlib.Path | None = None
    meta: bool = True

    def __post_init__(self) -> None:
        if self.fold and self.unfold:
            raise ValueError("Only fold or unfold a set, not both")
        if self.fix_samples_absolute and self.fix_samples_collect:
            raise ValueError("Only relink samples absolutely or collect them, not both")

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> SetJob:
        """The job a parsed command line describes."""
        return cls(**{field.name: getattr(args, field.name) for field in dataclasses.fields(cls)})

    @property
    def editing(self) -> bool:
        """Whether any flag changes the set in memory."""
        return any(getattr(self, name) for name in EDIT_FLAGS)

    @property
    def relinks(self) -> bool:
        return self.fix_samples_absolute or self.fix_samples_collect

    @property
    def scans(self) -> bool:
        """Whether the run's work is what a sidecar records.

        A run that scans leaves a ``<set stem>.meta.yaml`` beside every set it
        looked at, unless ``meta`` is off, and reads it back on the next pass to
        skip work it already did.
        """
        return self.check_plugins or self.check_samples or self.relinks

    def answered_by(self, scan: SetScan) -> bool:
        """Whether ``scan`` answers everything this job asks, so the set need not be read at all.

        Only the plugin check is cached. A sample check is not: the cache key
        leaves sample files out, so a deleted or unplugged sample would go
        unnoticed. Anything else a job asks needs the set itself.
        """
        if any(getattr(self, name) for name in COMMAND_FLAGS if name != "check_plugins"):
            return False
        return self.check_plugins and scan.plugins_missing is not None


# The flags that ask something of a set, and those of them that edit it, read
# off the fields so a new flag only needs adding to SetJob.
COMMAND_FLAGS = tuple(field.name for field in dataclasses.fields(SetJob) if field.metadata.get(_COMMAND))
EDIT_FLAGS = tuple(field.name for field in dataclasses.fields(SetJob) if field.metadata.get(_EDITS))


@dataclasses.dataclass(frozen=True)
class Findings:
    """What one run measured about one set, read by the sidecar and by the run record.

    A None was not asked by the run's flags rather than measured as nothing.
    """

    plugins_missing: dict[str, int] | None = None
    samples_missing_by_name: dict[str, int] | None = None
    relinked: Relinked | None = None

    @property
    def samples_missing(self) -> int | None:
        return None if self.samples_missing_by_name is None else sum(self.samples_missing_by_name.values())

    def scan(self, live_set: AbletonSet, length: SetLength, *, digest: str, fingerprint: str | None) -> meta.SetScan:
        """The sidecar's scan block for the bytes ``digest`` names."""
        return meta.SetScan(
            scanned=meta.now(),
            scanned_with=meta.SCANNER,
            set_hash=digest,
            scan_fingerprint=fingerprint,
            live_version=live_set.version,
            bars=length.bars,
            bpm=length.bpm,
            plugins_missing=self.plugins_missing,
            plugins_fixed=None if self.relinked is None else report.plugin_relink_counts(self.relinked.relinks),
            samples_fixed=None if self.relinked is None else len(self.relinked.samples),
            samples_missing=self.samples_missing,
            samples_missing_by_name=self.samples_missing_by_name,
        )

    def record(self, path: pathlib.Path, *, written: pathlib.Path | None, changed: bool) -> report.SetRecord:
        """The run record of the set read from ``path``."""
        return report.SetRecord(
            path=str(path),
            written=None if written is None else str(written),
            changed=changed,
            relinks=[] if self.relinked is None else report.relink_records(self.relinked.relinks),
            refused=[] if self.relinked is None else report.refusal_records(self.relinked.refused),
            plugins_missing=self.plugins_missing,
            samples_missing=self.samples_missing,
        )


def run_set(
    job: SetJob,
    path: pathlib.Path,
    *,
    locations: Locations | None = None,
    db: SampleCatalog | None = None,
    plugin_files_db: SampleCatalog | None = None,
    fingerprint: str | None = None,
) -> report.SetRecord:
    """Do ``job`` to the set at ``path`` and answer what happened to it.

    ``locations`` is the computer a plugin check judges against, this one when
    None. ``db`` and ``plugin_files_db`` are the catalogs a relink looks files
    up in; ``fingerprint`` is the batch's :func:`meta.scan_fingerprint`, without
    which no cached scan is reused. A set that cannot be read is a record with
    an error; anything else that goes wrong is raised for the caller to record.
    """
    # The sidecar answers for these exact bytes or not at all, so the hash comes
    # off the file before anything reads or edits it.
    digest = meta.file_hash(path) if job.meta and job.scans else None
    cached = meta.cached_scan(path, digest, fingerprint=fingerprint) if digest is not None else None
    if cached is not None and job.answered_by(cached):
        return _from_cache(job, path, cached)

    logger.info("%sParsing: %s", C, path)
    live_set = AbletonSet(path)
    unreadable = persistence.load_set(live_set)
    if unreadable is not None:
        return report.SetRecord(path=str(path), error=unreadable)
    # An --output sweep only writes sets the run actually changed and the record
    # says which those were, so remember how the tree serialized before any edit
    # touched it.
    pristine_xml = live_set.generate_xml() if job.editing else None
    live_set.load_version()
    logger.info("%sSet name: %s, %sBPM: %s", C, path.stem, B, live_set.transport.bpm())
    length = live_set.transport.length()
    console.render_length(length)

    _edit_tracks(job, live_set)
    if job.list_tracks:
        console.render_tracks(live_set.tracks.load())
    if job.describe:
        print(describe_json(live_set, DescribeLevel(job.describe)))
    findings = _measure(job, live_set, cached, locations, db, plugin_files_db)
    if job.dump_plugins:
        logger.info("%sDumping plugin buffer data for reverse engineering...", C)
        live_set.plugins.dump()

    changed = pristine_xml is not None and live_set.generate_xml() != pristine_xml
    saved = _save(job, live_set, findings.relinked, changed=changed)
    if job.xml:
        # After the save, so with --output the dump lands beside the copy under its name.
        persistence.save_xml(live_set, output_dir=job.output)

    if digest is not None:
        scan = findings.scan(live_set, length, digest=digest, fingerprint=fingerprint)
        if cached is not None:
            scan = meta.carry_forward(cached, scan)
        # With --output nothing may be written next to the original, so the
        # sidecar goes with the copy or nowhere. live_set.path is where the set
        # actually landed, suffixes, renames and all.
        if job.output is None or saved:
            meta.write(live_set.path, scan, human_source=path)

    return findings.record(path, written=live_set.path if saved else None, changed=changed)


def _from_cache(job: SetJob, path: pathlib.Path, scan: meta.SetScan) -> report.SetRecord:
    """The record of a set its sidecar answers for whole: nothing is parsed, checked or written."""
    logger.info("%sUnchanged since its last scan: %s", C, path)
    logger.info("%sSet name: %s, %sBPM: %s, bars: %s", C, path.stem, B, scan.bpm, scan.bars)
    plugins_missing = scan.plugins_missing if job.check_plugins else None
    if plugins_missing is not None:
        console.render_cached_plugins(plugins_missing)
    findings = Findings(plugins_missing=plugins_missing)
    return findings.record(path, written=None, changed=False)


def _edit_tracks(job: SetJob, live_set: AbletonSet) -> None:
    if job.master_out:
        live_set.tracks.set_audio_output(job.master_out, element_string=Tag.MAIN_TRACK)
    if job.cue_out:
        live_set.tracks.set_audio_output(job.cue_out, element_string=Tag.PRE_HEAR_TRACK)
    if job.fold:
        live_set.tracks.fold()
    elif job.unfold:
        live_set.tracks.unfold()
    if job.set_track_heights:
        live_set.tracks.set_heights(job.set_track_heights)
    if job.set_track_widths:
        live_set.tracks.set_widths(job.set_track_widths)
    if job.gradient_tracks:
        live_set.tracks.gradient()


def _measure(
    job: SetJob,
    live_set: AbletonSet,
    cached: meta.SetScan | None,
    locations: Locations | None,
    db: SampleCatalog | None,
    plugin_files_db: SampleCatalog | None,
) -> Findings:
    """Check samples, relink, check plugins, in that order; a cached answer stands in for the plugin check only."""
    samples_missing: dict[str, int] | None = None
    if job.check_samples:
        missing = live_set.files.check()
        console.render_missing_samples(missing.samples)
        console.render_missing_plugin_files(missing.plugin_files, missing.notes)
        samples_missing = report.count_names(missing.names())

    relinked: Relinked | None = None
    if job.relinks:
        if db is None:
            raise ValueError("Relinking samples needs a sample database")
        relinked = live_set.files.relink(db, plugin_files_db, collect=job.fix_samples_collect)

    plugins_missing: dict[str, int] | None = None
    if job.check_plugins:
        if cached is not None and cached.plugins_missing is not None:
            plugins_missing = cached.plugins_missing
            console.render_cached_plugins(plugins_missing)
        else:
            oracle = loadable.oracle(Locations.this_computer() if locations is None else locations)
            refs = live_set.plugins.scan(oracle)
            console.render_plugins(refs)
            plugins_missing = report.scan_missing(refs)

    return Findings(plugins_missing=plugins_missing, samples_missing_by_name=samples_missing, relinked=relinked)


def _save(job: SetJob, live_set: AbletonSet, relinked: Relinked | None, *, changed: bool) -> bool:
    """Write the set if the job says to, answering whether it was written."""
    if job.save:
        if job.output is not None and not changed:
            logger.info("%sSet is unchanged, nothing written to %s", Y, job.output)
            return False
        # The collector's copies go into the project only when the set that
        # points at them is written, and before it, so the set never names a
        # file that is not there yet.
        if relinked is not None and relinked.collector is not None:
            relinked.collector.run()
        live_set.save_set(output_dir=job.output)
        return True
    if job.editing:
        logger.info("%sNo changes saved, use -s/--save option to write changes to file.", Y)
        if relinked is not None and relinked.collector is not None and relinked.collector.copies:
            logger.info("%sNo samples copied into the project either: %s", Y, len(relinked.collector.copies))
    return False
