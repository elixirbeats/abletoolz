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

from abletoolz.live_set import AbletonSet, persistence
from abletoolz.live_set.describe import DescribeLevel, describe_json
from abletoolz.live_set.files import Relinked
from abletoolz.live_set.plugin_files import PluginFile
from abletoolz.live_set.plugins import PluginRef
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
    live_samples_missing: dict[str, int] | None = None
    plugin_files_missing: tuple[PluginFile, ...] = ()
    plugin_notes: tuple[PluginFile, ...] = ()
    plugin_refs: tuple[PluginRef, ...] | None = None

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

    def record(
        self, path: pathlib.Path, *, written: pathlib.Path | None, changed: bool, **set_facts: Any
    ) -> report.SetRecord:
        """The run record of the set read from ``path``; ``set_facts`` are its header, edits, notices and save."""
        refs = self.plugin_refs
        return report.SetRecord(
            path=str(path),
            written=None if written is None else str(written),
            changed=changed,
            relinks=[] if self.relinked is None else report.relink_records(self.relinked.relinks),
            refused=[] if self.relinked is None else report.refusal_records(self.relinked.refused),
            plugins_missing=self.plugins_missing,
            plugins_loaded=None
            if refs is None
            else report.count_names(ref.name or "<unknown>" for ref in refs if ref.exists),
            plugin_alternatives={}
            if refs is None
            else {ref.name or "<unknown>": str(ref.alternative) for ref in refs if ref.alternative is not None},
            samples_missing=self.samples_missing,
            samples_missing_by_name=self.live_samples_missing,
            plugin_files_missing=report.plugin_file_records(self.plugin_files_missing),
            plugin_notes=report.note_records(self.plugin_notes),
            **set_facts,
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

    logger.debug("Parsing %s", path)
    live_set = AbletonSet(path)
    unreadable = persistence.load_set(live_set)
    if unreadable is not None:
        return report.SetRecord(path=str(path), error=unreadable)
    # An --output sweep only writes sets the run actually changed and the record
    # says which those were, so remember how the tree serialized before any edit
    # touched it.
    pristine_xml = live_set.generate_xml() if job.editing else None
    live_set.load_version()
    length = live_set.transport.length()

    edits, notices = _edit_tracks(job, live_set)
    if live_set.version.split()[-1:] and "b" in live_set.version.split()[-1]:
        notices.append("saved by a beta of Live; some commands might not work properly")
    if job.list_tracks:
        console.render_tracks(live_set.tracks.load())
    if job.describe:
        print(describe_json(live_set, DescribeLevel(job.describe)))
    findings = _measure(job, live_set, cached, locations, db, plugin_files_db)
    if job.dump_plugins:
        live_set.plugins.dump()

    if findings.relinked is not None:
        notices.extend(findings.relinked.notices)

    changed = pristine_xml is not None and live_set.generate_xml() != pristine_xml
    collected = [
        report.CopyRecord(source=str(source), destination=str(destination))
        for destination, source in (
            findings.relinked.collector.copies.items() if findings.relinked and findings.relinked.collector else ()
        )
    ]
    saved, backup = _save(job, live_set, findings.relinked, changed=changed)
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

    return findings.record(
        path,
        written=live_set.path if saved else None,
        changed=changed,
        live_version=live_set.version,
        bpm=live_set.transport.bpm(),
        bars=length.bars,
        seconds=length.seconds,
        backup=None if backup is None else str(backup),
        collected=collected if saved else [],
        edits=edits,
        notices=notices,
    )


def _from_cache(job: SetJob, path: pathlib.Path, scan: meta.SetScan) -> report.SetRecord:
    """The record of a set its sidecar answers for whole: nothing is parsed, checked or written."""
    plugins_missing = scan.plugins_missing if job.check_plugins else None
    findings = Findings(plugins_missing=plugins_missing)
    return findings.record(
        path, written=None, changed=False, cached=True, live_version=scan.live_version, bpm=scan.bpm, bars=scan.bars
    )


def _edit_tracks(job: SetJob, live_set: AbletonSet) -> tuple[list[str], list[str]]:
    """Make the track edits the job asks for; answers what was done, then what could not be."""
    edits: list[str] = []
    notices: list[str] = []
    for number, tag, name in ((job.master_out, Tag.MAIN_TRACK, "main"), (job.cue_out, Tag.PRE_HEAR_TRACK, "cue")):
        if number:
            output = live_set.tracks.set_audio_output(number, element_string=tag)
            if output is None:
                notices.append(f"the set has no {name} track to route")
            else:
                edits.append(f"{name} out to {output}")
    if job.fold:
        live_set.tracks.fold()
        edits.append("folded every track")
    elif job.unfold:
        live_set.tracks.unfold()
        edits.append("unfolded every track")
    if job.set_track_heights:
        live_set.tracks.set_heights(job.set_track_heights)
        edits.append(f"track heights {job.set_track_heights}")
    if job.set_track_widths:
        live_set.tracks.set_widths(job.set_track_widths)
        edits.append(f"track widths {job.set_track_widths}")
    if job.gradient_tracks:
        live_set.tracks.gradient()
        edits.append("colour gradient")
    return edits, notices


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
    live_samples_missing: dict[str, int] | None = None
    plugin_files_missing: tuple[PluginFile, ...] = ()
    plugin_notes: tuple[PluginFile, ...] = ()
    plugin_refs: tuple[PluginRef, ...] | None = None
    if job.check_samples:
        missing = live_set.files.check()
        samples_missing = report.count_names(missing.names())
        live_samples_missing = report.count_names(ref.name for ref in missing.samples)
        plugin_files_missing, plugin_notes = tuple(missing.plugin_files), tuple(missing.notes)

    relinked: Relinked | None = None
    if job.relinks:
        if db is None:
            raise ValueError("Relinking samples needs a sample database")
        relinked = live_set.files.relink(db, plugin_files_db, collect=job.fix_samples_collect)

    plugins_missing: dict[str, int] | None = None
    if job.check_plugins:
        if cached is not None and cached.plugins_missing is not None:
            plugins_missing = cached.plugins_missing
        else:
            oracle = loadable.oracle(Locations.this_computer() if locations is None else locations)
            refs = live_set.plugins.scan(oracle)
            plugins_missing = report.scan_missing(refs)
            plugin_refs = tuple(refs)

    return Findings(
        plugins_missing=plugins_missing,
        samples_missing_by_name=samples_missing,
        relinked=relinked,
        live_samples_missing=live_samples_missing,
        plugin_files_missing=plugin_files_missing,
        plugin_notes=plugin_notes,
        plugin_refs=plugin_refs,
    )


def _save(
    job: SetJob, live_set: AbletonSet, relinked: Relinked | None, *, changed: bool
) -> tuple[bool, pathlib.Path | None]:
    """Write the set if the job says to, answering whether it was written and its backup."""
    if job.save:
        if not changed:
            logger.debug("%s is unchanged, nothing written", live_set.path)
            return False, None
        # The collector's copies go into the project only when the set that
        # points at them is written, and before it, so the set never names a
        # file that is not there yet.
        if relinked is not None and relinked.collector is not None:
            relinked.collector.run()
        return True, live_set.save_set(output_dir=job.output)
    return False, None
