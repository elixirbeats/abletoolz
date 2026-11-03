"""Many sets at once: one job run on each, in worker processes when there is more than one.

``run_sets`` answers one record per set. A set that fails is that set's record
with its error, never the end of the run; only Ctrl+C ends a run early, and then
it answers nothing. Each worker gets the job and the run's locations pickled and
its own snapshot of the sample catalogs, and returns what it printed itself --
diagnostics, --describe, --list-tracks -- whole with its record, so sets running
side by side cannot interleave their lines. Each set's block is printed as its
record comes in, in the order the sets were given.
"""

from __future__ import annotations

import atexit
import io
import logging
import multiprocessing
import os
import pathlib
import signal
import sys
import tempfile
import traceback
from collections import deque
from collections.abc import Sequence
from concurrent.futures import Future, ProcessPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from itertools import islice

from abletoolz.errors import ElementNotFound, SetError
from abletoolz.machine import Locations
from abletoolz.run import console, report
from abletoolz.run.job import SetJob, run_set
from abletoolz.samples import database
from abletoolz.samples.catalog import SampleCatalog

logger = logging.getLogger(__name__)


def run_sets(
    job: SetJob,
    paths: Sequence[pathlib.Path],
    *,
    jobs: int,
    locations: Locations,
    db: SampleCatalog | None = None,
    plugin_files_db: SampleCatalog | None = None,
    fingerprint: str | None = None,
    verbose: bool = False,
) -> list[report.SetRecord] | None:
    """Do ``job`` to every set in ``paths``, ``jobs`` at a time; None when Ctrl+C stopped the run.

    ``locations``, ``db``, ``plugin_files_db`` and ``fingerprint`` are what
    :func:`run_set` takes. With one job the sets run in this process; with more
    they run side by side. Either way each set's block is printed, and its
    record answered, in the order the sets were given.
    """
    records: list[report.SetRecord] = []
    if jobs == 1:
        try:
            for path in paths:
                records.append(_record(job, path, locations, db, plugin_files_db, fingerprint))
                console.render_set(records[-1])
        except KeyboardInterrupt:
            console.problem("Interrupted!")
            return None
        return records
    with tempfile.TemporaryDirectory(prefix="abletoolz-workers-") as temporary:
        folder = pathlib.Path(temporary)
        snapshot = _snapshot(db, folder / "catalog.sqlite3") if db is not None else None
        plugin_files = (
            _snapshot(plugin_files_db, folder / "plugin_files.sqlite3") if plugin_files_db is not None else None
        )
        executor = ProcessPoolExecutor(
            max_workers=jobs,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_initialize_worker,
            initargs=(_Worker(job, locations, fingerprint, verbose), snapshot, plugin_files),
        )
        try:
            remaining = iter(paths)
            # In the order given: the oldest set is waited on while the ones after it keep running.
            queued: deque[Future[_Output]] = deque(
                executor.submit(_run_in_worker, path) for path in islice(remaining, jobs * 2)
            )
            while queued:
                output = queued.popleft().result()
                sys.stdout.write(output.stdout)
                sys.stderr.write(output.stderr)
                console.render_set(output.record)
                records.append(output.record)
                following = next(remaining, None)
                if following is not None:
                    queued.append(executor.submit(_run_in_worker, following))
        except KeyboardInterrupt:
            console.problem("Interrupted! Cancelling pending sets, waiting for dispatched sets to finish...")
            executor.shutdown(wait=True, cancel_futures=True)
            return None
        finally:
            executor.shutdown(wait=True)
    return records


def _record(
    job: SetJob,
    path: pathlib.Path,
    locations: Locations,
    db: SampleCatalog | None,
    plugin_files_db: SampleCatalog | None,
    fingerprint: str | None,
) -> report.SetRecord:
    """One set's record, its failure included."""
    try:
        return run_set(job, path, locations=locations, db=db, plugin_files_db=plugin_files_db, fingerprint=fingerprint)
    except (ElementNotFound, SetError) as error:
        logger.debug(traceback.format_exc())
        return report.SetRecord(path=str(path), error=f"{type(error).__name__}: {error}")
    except Exception as error:  # noqa: BLE001 -- one set's failure is that set's record, not the end of the run
        logger.debug("Unexpected error on %s:\n%s", path, traceback.format_exc())
        return report.SetRecord(path=str(path), error=f"{type(error).__name__}: {error}")


def _snapshot(catalog: SampleCatalog, destination: pathlib.Path) -> pathlib.Path:
    catalog.backup_to(destination)
    return destination


@dataclass(frozen=True, slots=True)
class _Worker:
    """What every worker process is handed once, when it starts."""

    job: SetJob
    locations: Locations
    fingerprint: str | None
    verbose: bool


@dataclass(slots=True)
class _WorkerState:
    worker: _Worker
    db: SampleCatalog | None
    plugin_files_db: SampleCatalog | None


@dataclass(frozen=True, slots=True)
class _Output:
    record: report.SetRecord
    stdout: str
    stderr: str


_STATE: _WorkerState | None = None


def _initialize_worker(worker: _Worker, db_path: pathlib.Path | None, plugin_files_path: pathlib.Path | None) -> None:
    global _STATE
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    # numpy (only --gradient-tracks loads it) would start a BLAS thread pool sized to every CPU in
    # every worker; that many workers times that many threads exhausts the commit limit on Windows.
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    db = database.load_db(db_path) if db_path is not None else None
    plugin_files_db = database.load_db(plugin_files_path) if plugin_files_path is not None else None
    for opened in (db, plugin_files_db):
        if opened is not None:
            atexit.register(opened.close)
    _STATE = _WorkerState(worker, db, plugin_files_db)


def _run_in_worker(path: pathlib.Path) -> _Output:
    state = _STATE
    assert state is not None
    worker = state.worker
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        # Onto this set's own streams, which is why it is forced.
        console.setup(verbose=worker.verbose, machine_readable=bool(worker.job.describe), force=True)
        record = _record(worker.job, path, worker.locations, state.db, state.plugin_files_db, worker.fingerprint)
    return _Output(record, stdout.getvalue(), stderr.getvalue())
