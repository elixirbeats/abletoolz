"""The machine record of one run: what happened to every set, and the totals.

The console tells a person what happened and this tells a program, but both are
built from the same facts -- one plugin scan, one sample check, one relink
pass -- so the two can never disagree about a run. Nothing here re-derives
anything; every function in the middle of this module takes a report a domain
call already produced and restates it in the record's terms.

Converting plugin devices is not a run of this command; ``abletoolz plugins
apply`` reports what it did itself.
"""

from __future__ import annotations

import datetime
import json
import logging
import pathlib
from collections import Counter
from collections.abc import Iterable, Sequence

import pydantic

from abletoolz import __version__
from abletoolz.live_set.plugins import PluginRef
from abletoolz.samples.relink import Refusal, Relink

logger = logging.getLogger(__name__)

PREFIX = "abletoolz_report_"


class RelinkRecord(pydantic.BaseModel):
    """One reference the run pointed at a file found again.

    ``device`` is the plugin device whose patch held the path, null for Live's
    own sample reference; ``track`` is where that device sits.
    """

    model_config = pydantic.ConfigDict(extra="forbid")

    old: str
    new: str
    device: str | None = None
    track: str | None = None


class RefusalRecord(pydantic.BaseModel):
    """A missing file the run could have relinked but left as it was, and why."""

    model_config = pydantic.ConfigDict(extra="forbid")

    what: str
    why: str


class SetRecord(pydantic.BaseModel):
    """What one run did to one set.

    ``path`` is always the set the run read, so a record can be matched back to
    the library whatever the run renamed or copied; ``written`` is where the
    result of the run landed, which for an ``--output`` sweep is somewhere else
    entirely and null for a set nothing was written for.

    ``plugins_missing`` and ``samples_missing`` are null when the run's flags
    never asked -- a run that only checks plugins says nothing about samples,
    and saying zero would be a claim nobody measured.
    """

    model_config = pydantic.ConfigDict(extra="forbid")

    path: str
    written: str | None = None
    changed: bool = False
    error: str | None = None
    relinks: list[RelinkRecord] = pydantic.Field(default_factory=list)
    refused: list[RefusalRecord] = pydantic.Field(default_factory=list)
    plugins_missing: dict[str, int] | None = None
    samples_missing: int | None = None


class RunTotals(pydantic.BaseModel):
    """The whole run in one block, so nothing has to add the records up itself."""

    model_config = pydantic.ConfigDict(extra="forbid")

    sets: int
    changed: int
    failed: int
    relinks: int
    refused: int
    plugins_missing: dict[str, int]
    samples_missing: int


class RunReport(pydantic.BaseModel):
    """One run, start to finish."""

    model_config = pydantic.ConfigDict(extra="forbid")

    abletoolz: str
    command: list[str]
    started: datetime.datetime
    finished: datetime.datetime
    totals: RunTotals
    sets: list[SetRecord]


def count_names(names: Iterable[str]) -> dict[str, int]:
    """Name to how many times it came up, which is how every table here is built."""
    return dict(Counter(names))


# -- from what the domain already reported ----------------------------------


def scan_missing(refs: Sequence[PluginRef]) -> dict[str, int]:
    """The missing-plugin table of one ``Plugins.scan``, by device count."""
    return count_names(ref.name if ref.name is not None else "<unknown>" for ref in refs if not ref.exists)


def relink_records(relinks: Sequence[Relink]) -> list[RelinkRecord]:
    """Every reference pointed at a file found again, Live's own and the ones inside plugin patches."""
    return [RelinkRecord(old=fix.old, new=fix.new, device=fix.device, track=fix.track) for fix in relinks]


def refusal_records(refused: Sequence[Refusal]) -> list[RefusalRecord]:
    """Every missing file the run left as it was although it could have relinked it, with the reason."""
    return [RefusalRecord(what=refusal.what, why=refusal.why) for refusal in refused]


def plugin_relink_counts(relinks: Sequence[Relink]) -> dict[str, int]:
    """Plugin devices whose files a run relinked, by device name, the way a sidecar counts them."""
    return count_names(fix.device for fix in relinks if fix.device is not None)


# -- the run ----------------------------------------------------------------


def totals(records: Sequence[SetRecord]) -> RunTotals:
    """Add the run up: what was found across it, and what was done about it."""
    missing: Counter[str] = Counter()
    for record in records:
        missing.update(record.plugins_missing or {})
    return RunTotals(
        sets=len(records),
        changed=sum(1 for record in records if record.changed),
        failed=sum(1 for record in records if record.error is not None),
        relinks=sum(len(record.relinks) for record in records),
        refused=sum(len(record.refused) for record in records),
        plugins_missing=dict(missing),
        samples_missing=sum(record.samples_missing or 0 for record in records),
    )


def build(records: Sequence[SetRecord], *, command: Sequence[str], started: datetime.datetime) -> RunReport:
    """One run's records with its totals in front of them."""
    return RunReport(
        abletoolz=__version__,
        command=list(command),
        started=started,
        finished=datetime.datetime.now().astimezone(),
        totals=totals(records),
        sets=list(records),
    )


def write(run: RunReport, directory: pathlib.Path) -> pathlib.Path:
    """Write the run report into ``directory``, named for when the run ended.

    The name carries the timestamp so a sweep repeated over the same folder
    keeps every record instead of overwriting the last one. Colons are out
    because Windows has no room for them in a filename.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{PREFIX}{run.finished.strftime('%Y-%m-%dT%H%M%S')}.json"
    path.write_text(json.dumps(run.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    return path
