"""Per-set sidecar metadata: ``<set stem>.meta.yaml`` beside the set it describes.

The file has two zones and the split is the whole point. Everything under
``scan`` belongs to abletoolz and is replaced whole every time the set is
scanned. ``status``, ``notes``, and any other key someone puts in the file
belong to the user: they are read, carried across every rescan, and written back
exactly as they came. Nothing here ever invents or edits them.

The scan block doubles as a cache. A set whose bytes still hash to ``set_hash``,
scanned by the version in ``scanned_with``, has already been answered: the scans
that fill this file are skipped and the stored answers reused, which is what
makes a second pass over a library cheap. Either fact moving throws the whole
block away, because a set that changed has to be looked at again.

``set_hash`` is of the bytes that were read, not of anything written afterwards.
A run that edits and saves a set therefore leaves a sidecar that deliberately no
longer matches the file beside it -- the set is not the one that was scanned,
and the next run has to look for itself.

A field left null was not measured by the run that wrote the block, which is not
the same as measuring zero: a ``--check-samples`` run says nothing about
plugins. A rescan of bytes already in the cache adds what it measured and keeps
what it did not, so passes with different flags accumulate into one record.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import pathlib

import pydantic
import yaml

from abletoolz import __version__
from abletoolz.misc import BACKUP_DIR

logger = logging.getLogger(__name__)

SUFFIX = ".meta.yaml"

# What wrote a scan block, and what a cached block has to match to be reused.
SCANNER = f"abletoolz {__version__}"

HEADER = """\
# abletoolz sidecar.
#
# status and notes are yours. abletoolz reads them, carries them across every
# rescan and never writes them itself; so is any other key you add here.
# Everything under scan: is abletoolz's, and is replaced whole every time the
# set is scanned.
"""


class SetScan(pydantic.BaseModel):
    """What one scan of a set found.

    A null field was not measured by that run rather than measured as nothing.
    ``plugins_fixed`` is keyed by ``"source -> target"`` where the fix replaced
    one plugin identity with another, and by the device name where it repaired a
    device in place.
    """

    model_config = pydantic.ConfigDict(extra="ignore")

    scanned: datetime.datetime
    scanned_with: str
    set_hash: str
    live_version: str | None = None
    bars: int | None = None
    bpm: float | None = None
    plugins_missing: dict[str, int] | None = None
    plugins_fixed: dict[str, int] | None = None
    samples_missing: int | None = None
    samples_missing_by_name: dict[str, int] | None = None


class SetMeta(pydantic.BaseModel):
    """One sidecar file: the user's zone, then abletoolz's.

    ``extra="allow"`` is the user's zone being the user's: a key nothing here
    knows about survives a rescan the same way ``status`` and ``notes`` do.
    """

    model_config = pydantic.ConfigDict(extra="allow")

    status: str | None = None
    notes: str | None = None
    scan: SetScan | None = None


def sidecar_path(set_path: pathlib.Path) -> pathlib.Path:
    """Where the sidecar for one set lives."""
    return set_path.parent / (set_path.stem + SUFFIX)


def file_hash(path: pathlib.Path) -> str:
    """Sha256 of a file's bytes, which is what tells one scan's set from another."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def describes_a_real_set(set_path: pathlib.Path) -> bool:
    """Whether a sidecar beside this file would describe anything worth describing.

    A backup is a copy of a set the user already has and a ``._`` file is macOS
    resource-fork litter that only looks like one; neither gets a file written
    next to it.
    """
    if set_path.name.startswith("._"):
        return False
    return BACKUP_DIR not in set_path.parts[:-1]


def read(set_path: pathlib.Path) -> SetMeta | None:
    """The sidecar beside ``set_path``, or None when there is no usable one.

    A sidecar nothing can parse counts as no sidecar: nothing is cached from it
    and the next write replaces it, which loses whatever the user had written in
    a file that no longer says it. That is loud on purpose.
    """
    path = sidecar_path(set_path)
    if not path.is_file():
        return None
    try:
        return SetMeta.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, pydantic.ValidationError) as error:
        logger.warning("Ignoring unreadable sidecar %s: %s", path, error)
        return None


def cached_scan(set_path: pathlib.Path, digest: str) -> SetScan | None:
    """The stored scan when it answers for these exact bytes and this version.

    Both facts have to hold. A different hash is a different set; a different
    abletoolz is a different answer to the same question, which is the whole
    reason the version is written down beside the hash.
    """
    document = read(set_path)
    if document is None or document.scan is None:
        return None
    scan = document.scan
    if scan.set_hash != digest or scan.scanned_with != SCANNER:
        return None
    return scan


def carry_forward(previous: SetScan, current: SetScan) -> SetScan:
    """Fill what this run did not measure from a scan of the same bytes.

    Only reached on a cache hit, so ``previous`` describes the same file this
    run read, and anything it measured is still true of it.
    """
    kept = {name: value for name, value in previous.model_dump().items() if getattr(current, name) is None}
    return current.model_copy(update=kept)


def write(set_path: pathlib.Path, scan: SetScan, *, human_source: pathlib.Path | None = None) -> pathlib.Path | None:
    """Write the sidecar for ``set_path``, keeping every user-written value.

    ``human_source`` is the set whose sidecar the user's zone comes from, for
    the ``--output`` case where the scanned set and the written one are
    different files. Answers None when the file is one nothing should be written
    beside.
    """
    if not describes_a_real_set(set_path):
        logger.debug("No sidecar written for %s", set_path)
        return None
    existing = read(human_source if human_source is not None else set_path)
    document = SetMeta() if existing is None else existing
    document = document.model_copy(update={"scan": scan})
    path = sidecar_path(set_path)
    body = yaml.safe_dump(document.model_dump(mode="json"), sort_keys=False, allow_unicode=True)
    path.write_text(HEADER + body, encoding="utf-8")
    logger.debug("Wrote sidecar %s", path)
    return path


def now() -> datetime.datetime:
    """Scan time, with this machine's offset on it so it reads the same anywhere."""
    return datetime.datetime.now().astimezone()
