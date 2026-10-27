"""Sample reference checking and repair."""

from __future__ import annotations

import logging
import pathlib
from collections.abc import Iterator
from typing import TYPE_CHECKING

from abletoolz.ansi import G, R, Y
from abletoolz.live_set import sample_ref
from abletoolz.live_set.domain import Domain
from abletoolz.live_set.sample_ref import Resolution
from abletoolz.samples import relink
from abletoolz.samples.catalog import SampleCatalog
from abletoolz.samples.matcher import is_factory_pack_path
from abletoolz.schema import Tag

if TYPE_CHECKING:
    from abletoolz.live_set.document import AbletonSet

logger = logging.getLogger(__name__)


class Samples(Domain):
    """Sample references of one set."""

    def __init__(self, live_set: AbletonSet) -> None:
        super().__init__(live_set)
        self._refs: list[sample_ref.SampleRef] | None = None

    def iterate(self) -> Iterator[sample_ref.SampleRef]:
        """Iterate through set sample references, parsed once, all together, and cached."""
        if self._refs is None:
            self._refs = [
                sample_ref.SampleRef(element, self.version, self._set.project_root)
                for element in self._root.iter(Tag.SAMPLE_REF)
            ]
        yield from self._refs

    def check(self) -> list[sample_ref.SampleRef]:
        """Return every sample reference that resolves to no file on disk."""
        missing = []
        for parsed in self.iterate():
            resolution = parsed.resolution
            if resolution is Resolution.PACK:
                # Live loads this out of an installed Pack; no path on this
                # machine can confirm or deny it, so calling it missing would
                # be a guess dressed as a finding.
                logger.debug("%sSample %s comes from Pack %s", G, parsed.name, parsed.live_pack)
            elif resolution is Resolution.LOADS:
                logger.debug(
                    "%sSample %s found: Relative %s, Absolute %s",
                    G,
                    parsed.name,
                    parsed.relative,
                    parsed.absolute,
                )
            elif resolution in (Resolution.ONLY_BY_ABSOLUTE, Resolution.MISSING):
                missing.append(parsed)
        return missing

    def fix(
        self,
        db: SampleCatalog,
        collector: relink.Collector | None = None,
        refused: list[relink.Refusal] | None = None,
    ) -> list[relink.Relink]:
        """Point every missing sample reference at the file the sample database finds for it.

        With a ``collector`` the file found is collected into the project the
        way Live's collect and save does: into the reference's own project
        folder when it had one, else Samples/Imported. The copy itself is only
        made when the collector runs, which is when the set is saved. A
        reference left as it was although a file was found goes into ``refused``.
        """
        refused = [] if refused is None else refused
        relinks: list[relink.Relink] = []
        missing = 0
        located: dict[relink.Evidence, pathlib.Path | None] = {}
        for parsed in self.iterate():
            resolution = parsed.resolution
            if resolution is Resolution.PACK:
                # Addressed by Pack id, not by path: there is nothing here to
                # repair and rewriting it to a path would break what works.
                logger.debug("%sSkipping Pack reference %s (%s)", Y, parsed.name, parsed.live_pack)
                continue
            if resolution is Resolution.ONLY_BY_ABSOLUTE and parsed.absolute is not None:
                # The file is where the absolute path says; point Live there.
                missing += 1
                old = str(parsed.relative)
                parsed.point_absolute(parsed.absolute)
                relinks.append(relink.Relink(old, str(parsed.absolute)))
                continue
            if resolution is not Resolution.MISSING:
                continue
            missing += 1
            # A reference may store only a relative path or a name; search from whatever it has.
            original = parsed.absolute or parsed.relative or pathlib.Path(parsed.name)
            # Builtin pack content relinks itself when Live opens the set.
            if is_factory_pack_path(original.parent):
                logger.debug("%sSkipping builtin pack content: %s", Y, original)
                continue

            evidence = relink.Evidence(
                parsed.name, original, size=parsed.size, mtime=parsed.last_modified, crc=parsed.crc
            )
            # The same sample is often referenced many times in one set.
            if evidence not in located:
                located[evidence] = relink.locate(db, evidence, f"sample {parsed.name} at {original}", refused)
            found = located[evidence]
            if found is not None and self._point_at(parsed, found, collector):
                relinks.append(relink.Relink(str(original), str(found)))
        logger.info(
            "%sOriginal missing sample count: %s, Samples fixed: %s, Couldn't fix: %s",
            G if len(relinks) == missing else R,
            missing,
            len(relinks),
            missing - len(relinks),
        )
        return relinks

    def _point_at(self, parsed: sample_ref.SampleRef, found: pathlib.Path, collector: relink.Collector | None) -> bool:
        """Rewrite ``parsed`` to ``found``, collected into the project when there is a collector."""
        if collector is None:
            parsed.point_absolute(found)
            return True
        folder: pathlib.PurePath = relink.COLLECT_FOLDER
        if parsed.get_relative_type() == sample_ref.RelativePathType.PROJECT and parsed.relative is not None:
            folder = parsed.relative.parent
        destination = collector.plan(found, folder)
        if destination is None:
            return False
        # A renamed file: the reference's own name and path follow it, not only the copy.
        absolute = found if found.name != parsed.name else None
        parsed.point_relative(
            pathlib.PurePath(folder) / destination.name, sample_ref.RelativePathType.PROJECT, absolute=absolute
        )
        return True
