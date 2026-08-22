"""The machine record of one run: what happened to every set, and the totals.

The console tells a person what happened and this tells a program, but both are
built from the same facts -- one repair report, one plugin scan, one sample
check -- so the two can never disagree about a run. Nothing here re-derives
anything; every function in the middle of this module takes a report a domain
call already produced and restates it in the record's terms.

A refusal is its own kind of record rather than a missing fix. "This set is too
old to hold a VST3" is not a failure and not silence: it is an answer, with a
plugin and a target format in it, and a program reading the run has to be able
to count those the same way a person reads them off the console.
"""

from __future__ import annotations

import datetime
import enum
import json
import logging
import pathlib
from collections import Counter
from collections.abc import Iterable, Sequence

import pydantic

from abletoolz import __version__
from abletoolz.live_set.plugins import DeviceStateFix, DeviceUpgrade, PluginRef
from abletoolz.plugin_parsers.base import PluginKind
from abletoolz.plugin_parsers.repair import RepairReport, RepairStatus

logger = logging.getLogger(__name__)

PREFIX = "abletoolz_report_"

# Every status that means repair looked at a device and did not rewrite it. The
# statuses left out are the two that need no explanation -- it was fixed, or it
# was never broken.
REFUSALS = (
    RepairStatus.BROKEN_UNMAPPED,
    RepairStatus.BROKEN_NO_UID,
    RepairStatus.UNSUPPORTED_PAIR,
    RepairStatus.SET_TOO_OLD_FOR_TARGET,
    RepairStatus.INCOMPLETE_DEVICE,
)


class FixMechanism(enum.StrEnum):
    """Which machinery rewrote a device.

    They fix different things and fail differently, so a run that fixed sixty
    devices is not one number: ``repair`` and ``translate`` swap one plugin
    identity for another, ``upgrade`` points a device at a different file, and
    ``deep_parser`` leaves the device alone and repairs what it saved.
    """

    TRANSLATE = "translate"
    UPGRADE = "upgrade"
    REPAIR = "repair"
    DEEP_PARSER = "deep-parser"


class DeviceFix(pydantic.BaseModel):
    """One device a run rewrote, and what it became.

    ``source`` and ``target`` are null for a fix that changed no identity --
    a deep parser repairs a device's saved state and leaves the device itself
    exactly where it was.
    """

    model_config = pydantic.ConfigDict(extra="forbid")

    device: str
    mechanism: FixMechanism
    track: str | None = None
    source: str | None = None
    target: str | None = None

    @property
    def key(self) -> str:
        """How this fix reads in a count: the move it made, or the device it mended."""
        if self.source is None or self.target is None:
            return self.device
        return f"{self.source} -> {self.target}"


class Refusal(pydantic.BaseModel):
    """One device a run could not rewrite, and why not."""

    model_config = pydantic.ConfigDict(extra="forbid")

    reason: RepairStatus
    device: str
    track: str | None = None
    target_format: PluginKind | None = None
    target_name: str | None = None


class SetRecord(pydantic.BaseModel):
    """What one run did to one set.

    ``path`` is always the set the run read, so a record can be matched back to
    the library whatever the run renamed or copied; ``written`` is where the
    result of the run landed, which for an ``--output`` sweep is somewhere else
    entirely and null for a set nothing was written for.

    ``plugins_missing`` and ``samples_missing`` are null when the run's flags
    never asked -- a run that only repairs plugins says nothing about samples,
    and saying zero would be a claim nobody measured.
    """

    model_config = pydantic.ConfigDict(extra="forbid")

    path: str
    written: str | None = None
    changed: bool = False
    error: str | None = None
    fixes: list[DeviceFix] = pydantic.Field(default_factory=list)
    refusals: list[Refusal] = pydantic.Field(default_factory=list)
    plugins_missing: dict[str, int] | None = None
    samples_missing: int | None = None


class RunTotals(pydantic.BaseModel):
    """The whole run in one block, so nothing has to add the records up itself."""

    model_config = pydantic.ConfigDict(extra="forbid")

    sets: int
    changed: int
    failed: int
    fixes: int
    fixes_by_mechanism: dict[FixMechanism, int]
    refusals_by_reason: dict[RepairStatus, int]
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


def repair_missing(report: RepairReport) -> dict[str, int]:
    """The same table read off a repair pass: every device it found broken.

    A repaired device counts as missing too. It is what the run walked in on,
    and the fix beside it is what the run did about it.
    """
    broken = (RepairStatus.FIXED, *REFUSALS)
    return count_names(action.source_name for action in report.actions if action.status in broken)


def repair_fixes(report: RepairReport) -> list[DeviceFix]:
    """Every device a repair pass rewrote."""
    return [
        DeviceFix(
            device=action.source_name,
            mechanism=FixMechanism.REPAIR,
            track=action.track,
            source=action.source_name,
            target=action.target_name,
        )
        for action in report.by_status(RepairStatus.FIXED)
    ]


def repair_refusals(report: RepairReport) -> list[Refusal]:
    """Every device a repair pass declined to rewrite, with its reason."""
    return [
        Refusal(
            reason=action.status,
            device=action.source_name,
            track=action.track,
            target_format=action.target_format,
            target_name=action.target_name,
        )
        for action in report.actions
        if action.status in REFUSALS
    ]


def upgrade_fixes(upgrades: Sequence[DeviceUpgrade]) -> list[DeviceFix]:
    """Every device an upgrade pass pointed at a different file."""
    return [
        DeviceFix(
            device=upgrade.source,
            mechanism=FixMechanism.UPGRADE,
            track=upgrade.track,
            source=upgrade.source,
            target=upgrade.target,
        )
        for upgrade in upgrades
    ]


def parser_fixes(fixes: Sequence[DeviceStateFix]) -> list[DeviceFix]:
    """Every device a registered parser mended from the inside."""
    return [
        DeviceFix(device=fix.name, mechanism=FixMechanism.DEEP_PARSER, track=fix.track) for fix in fixes
    ]


def fix_counts(fixes: Sequence[DeviceFix]) -> dict[str, int]:
    """Fixes as a sidecar counts them: what moved where, and how often."""
    return count_names(fix.key for fix in fixes)


# -- the run ----------------------------------------------------------------


def totals(records: Sequence[SetRecord]) -> RunTotals:
    """Add the run up: what was found across it, and what was done about it."""
    missing: Counter[str] = Counter()
    mechanisms: Counter[FixMechanism] = Counter()
    reasons: Counter[RepairStatus] = Counter()
    for record in records:
        missing.update(record.plugins_missing or {})
        mechanisms.update(fix.mechanism for fix in record.fixes)
        reasons.update(refusal.reason for refusal in record.refusals)
    return RunTotals(
        sets=len(records),
        changed=sum(1 for record in records if record.changed),
        failed=sum(1 for record in records if record.error is not None),
        fixes=sum(len(record.fixes) for record in records),
        fixes_by_mechanism=dict(mechanisms),
        refusals_by_reason=dict(reasons),
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
