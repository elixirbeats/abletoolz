"""Finding a missing file again, and collecting it into a project.

Both kinds of reference a set holds come through here: Live's own sample
references, and the file paths a few plugins keep inside their patches. They
differ only in how much they say about the file beside its name. A Live
reference usually stores its size and modification time, often Live's checksum
too; most plugin patches store a path and nothing else, and what each plugin
stores is learned plugin by plugin (Serato Sample stores the sample's length).
So one lookup serves both, and it decides from whatever the reference carries:

* A candidate the reference contradicts -- a stored size or checksum that
  differs from the file's -- is a different file, whatever else agrees.
* With a size or a time to go on, a candidate has to agree with one of them.
* A file under another name -- a renamed sample -- is only a candidate when the
  reference stores a size and a nonzero checksum and the file on disk has both.
  The checksum covers only the first 16 KiB, so loops cut from one recording
  can share it: when several different renamed files match, only a matching
  date picks one, and otherwise the lookup refuses.
* With neither, a name is only trusted when it picks out one file: the only
  file of that name, or the only one whose length matches. When several remain
  the lookup refuses and says which, rather than taking whichever came first.

Collecting copies a found file into the project, the way Live's own collect
and save does. Copies are planned during the relink and made only when the set
is saved, so a run that saves nothing writes nothing.
"""

from __future__ import annotations

import dataclasses
import filecmp
import logging
import pathlib
import shutil

from abletoolz.ansi import Y
from abletoolz.samples.catalog import SampleCatalog, SampleRecord
from abletoolz.samples.crc import file_crc
from abletoolz.samples.matcher import folder_scores, get_audio_length_seconds, length_score

logger = logging.getLogger(__name__)

LENGTH_TOLERANCE = 0.01
"""Seconds two lengths may differ by and still be the same file, for plugins that store only a length."""

COLLECT_FOLDER = pathlib.PurePosixPath("Samples/Imported")
"""Where a collected file goes inside a project when it had no project folder of its own: Live's own choice."""


@dataclasses.dataclass(frozen=True, slots=True)
class Evidence:
    """What a reference says about the file it points at.

    ``size`` and ``crc`` are None when the reference stores none; Live writes a
    size of 0 for "not stored", a checksum of 0 is no checksum either, and a
    checksum only counts beside a size.
    """

    name: str
    original: pathlib.Path
    size: int | None = None
    mtime: int | None = None
    crc: int | None = None
    length: float | None = None

    def __post_init__(self) -> None:
        if not self.size:
            object.__setattr__(self, "size", None)
            object.__setattr__(self, "crc", None)
        if self.crc == 0:
            object.__setattr__(self, "crc", None)

    def same_bytes(self, record: SampleRecord) -> bool:
        """Whether ``record`` stores the reference's size and checksum, so it is the file whatever its name."""
        if self.crc is None or self.size is None or self.size <= 0:
            return False
        size, crc = record.get("size"), record.get("crc")
        return isinstance(size, int) and isinstance(crc, int) and size == self.size and crc == self.crc

    def is_same_file_renamed(self, path: pathlib.Path, record: SampleRecord) -> bool:
        """Whether ``path``, under a name other than the reference's, is its file: size and checksum, on disk too."""
        if not self.same_bytes(record):
            return False
        try:
            return path.stat().st_size == self.size and file_crc(path) == self.crc
        except FileNotFoundError:
            return False

    def contradicts(self, record: SampleRecord) -> bool:
        """Whether ``record`` is a different file: a stored size or checksum disagrees with it."""
        if self.size is not None and _int(record.get("size")) not in (None, self.size):
            return True
        return self.crc is not None and _int(record.get("crc")) not in (None, self.crc)

    def corroborates(self, record: SampleRecord) -> bool:
        """Whether ``record`` agrees with the reference's stored size or modification time."""
        if self.size is not None and _int(record.get("size")) == self.size:
            return True
        return self.mtime is not None and _whole_seconds(record.get("last_modified")) == self.mtime

    @property
    def can_corroborate(self) -> bool:
        """Whether the reference stores anything beside a name and a length to check a file against."""
        return self.size is not None or self.mtime is not None

    def rank(self, path: pathlib.Path, record: SampleRecord) -> tuple[int, ...]:
        """How well ``path`` fits, highest best: same bytes, same name, length, size, date, then the closest folder.

        Only files the reference does not rule out are ranked, so a rejected one never has its length read.
        """
        return (
            int(self.same_bytes(record)),
            int(record.get("name") == self.name),
            length_score(self.length, path),
            int(self.size is not None and _int(record.get("size")) == self.size),
            int(self.mtime is not None and _whole_seconds(record.get("last_modified")) == self.mtime),
            *folder_scores(self.original, path),
        )


@dataclasses.dataclass(frozen=True, slots=True)
class Lookup:
    """What a search for one missing file found.

    ``found`` is the file to point at, or None. ``candidates`` is every file of
    that name on disk that the reference does not rule out, best first; when
    ``found`` is None and there are several, the reference could not tell them
    apart. ``gone`` is every database record it turned up whose file is no
    longer on disk; those are never candidates.
    """

    found: pathlib.Path | None
    candidates: tuple[pathlib.Path, ...]
    gone: tuple[pathlib.Path, ...] = ()


def find(db: SampleCatalog, evidence: Evidence) -> Lookup:
    """Look ``evidence`` up in the sample database; see the module docstring for the rules."""
    found = db.candidates(evidence.name, size=evidence.size, crc=evidence.crc)
    records = {pathlib.Path(key): record for key, record in found.items()}
    on_disk = {path: record for path, record in records.items() if path.is_file()}
    survivors = [
        path
        for path, record in on_disk.items()
        if not evidence.contradicts(record)
        and (record.get("name") == evidence.name or evidence.is_same_file_renamed(path, record))
    ]
    if len(survivors) > 1:
        survivors.sort(key=lambda path: evidence.rank(path, on_disk[path]), reverse=True)
    kept = tuple(survivors)
    gone = tuple(path for path in records if path not in on_disk)
    return Lookup(_pick(evidence, kept, on_disk), kept, gone)


def _pick(
    evidence: Evidence, kept: tuple[pathlib.Path, ...], records: dict[pathlib.Path, SampleRecord]
) -> pathlib.Path | None:
    if evidence.can_corroborate:
        agreeing = [path for path in kept if evidence.corroborates(records[path])]
        return _choose(evidence, agreeing, records)
    if len(kept) == 1:
        return kept[0]
    if evidence.length is not None:
        close = [path for path in kept if _length_matches(path, evidence.length)]
        if len(close) == 1:
            return close[0]
    return None


def _choose(
    evidence: Evidence, agreeing: list[pathlib.Path], records: dict[pathlib.Path, SampleRecord]
) -> pathlib.Path | None:
    """The best of ``agreeing``, unless it is one of several different renamed files the reference cannot tell apart."""
    if not agreeing or records[agreeing[0]].get("name") == evidence.name:
        return agreeing[0] if agreeing else None
    renamed = [path for path in agreeing if records[path].get("name") != evidence.name]
    if len(renamed) == 1:
        return renamed[0]
    if evidence.mtime is not None:
        dated = [path for path in renamed if _whole_seconds(records[path].get("last_modified")) == evidence.mtime]
        if len(dated) == 1:
            return dated[0]
    if all(filecmp.cmp(renamed[0], path, shallow=False) for path in renamed[1:]):
        # Copies of one file: any of them is the file.
        return renamed[0]
    return next((path for path in agreeing if records[path].get("name") == evidence.name), None)


def locate(
    db: SampleCatalog, evidence: Evidence, what: str, refused: list[Refusal], gone: set[pathlib.Path]
) -> pathlib.Path | None:
    """The file to point a missing reference at, or None after a warning that says why.

    Every kind of reference is relinked through here, so they all refuse the
    same way: a reference that cannot tell several files apart is left as it
    is, never pointed at a guess, and goes into ``refused``. ``what`` names the
    reference in the warning. Database records whose file is gone go into ``gone``.
    """
    lookup = find(db, evidence)
    gone.update(lookup.gone)
    if lookup.found is None and lookup.candidates:
        candidates = "".join(f"\n\t{path}" for path in lookup.candidates)
        logger.warning("%sCould not tell which file is %s from what it stores; left as it is:%s", Y, what, candidates)
        refused.append(
            Refusal(
                what,
                "several files match and nothing it stores tells them apart: " + ", ".join(map(str, lookup.candidates)),
            )
        )
    elif lookup.found is None:
        logger.warning("%sCould not find %s", Y, what)
    return lookup.found


@dataclasses.dataclass(frozen=True, slots=True)
class Refusal:
    """A missing file a run could have relinked but left as it was, and why."""

    what: str
    why: str


@dataclasses.dataclass(frozen=True, slots=True)
class Relink:
    """One reference a run pointed at a file found again.

    ``device`` is the plugin device whose patch held the path, None for Live's
    own sample reference. ``track`` is where that device sits; Live's own
    references are not traced to a track.
    """

    old: str
    new: str
    device: str | None = None
    track: str | None = None


class Collector:
    """The copies a relink pass plans into one project, made only when the set is saved."""

    def __init__(self, project_root: pathlib.Path) -> None:
        self.project_root = project_root
        self._planned: dict[pathlib.Path, pathlib.Path] = {}
        self.refused: list[Refusal] = []

    def plan(self, source: pathlib.Path, folder: pathlib.PurePath) -> pathlib.Path | None:
        """Where ``source`` lands in ``folder`` of the project, or None when a different file holds that name.

        The same file collected twice lands once. A file already in place is
        not copied again; a different file of the same name, on disk or
        already planned, is never replaced.
        """
        destination = self.project_root / folder / source.name
        holder = self._planned.get(destination, destination if destination.exists() else None)
        if holder is None:
            self._planned[destination] = source
        elif not _same_file(holder, source):
            logger.error("Not collecting %s: a different file named %s is already in %s", source, source.name, folder)
            self.refused.append(
                Refusal(f"collecting {source}", f"a different file named {source.name} is already in {folder}")
            )
            return None
        return destination

    @property
    def copies(self) -> dict[pathlib.Path, pathlib.Path]:
        """Destination to source, for every copy not yet made."""
        return dict(self._planned)

    def run(self) -> None:
        """Make every planned copy."""
        for destination, source in self._planned.items():
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Created exclusively: another set in this project may have collected the same name since the plan.
            try:
                with source.open("rb") as reader, destination.open("xb") as writer:
                    shutil.copyfileobj(reader, writer)
            except FileExistsError:
                if not _same_file(destination, source):
                    raise FileExistsError(
                        f"Not collecting {source}: a different {destination.name} appeared in {destination.parent}"
                    ) from None
            logger.info("Collected %s into %s", source, destination.parent)
        self._planned.clear()


def _same_file(a: pathlib.Path, b: pathlib.Path) -> bool:
    """Same size and the same checksum Live takes: as close to the same file as Live itself checks."""
    return a.stat().st_size == b.stat().st_size and file_crc(a) == file_crc(b)


def _length_matches(path: pathlib.Path, length: float) -> bool:
    measured = get_audio_length_seconds(path)
    return measured is not None and abs(measured - length) <= LENGTH_TOLERANCE


def _int(value: object) -> int | None:
    try:
        return int(value) if isinstance(value, (int, float, str)) else None
    except ValueError:
        return None


def _whole_seconds(value: object) -> int | None:
    """The database stores st_mtime as a float; sets store whole epoch seconds."""
    try:
        return int(float(value)) if isinstance(value, (int, float, str)) else None
    except ValueError:
        return None
