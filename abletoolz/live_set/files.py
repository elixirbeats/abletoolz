"""Every file one set points at: Live's own sample references and the files inside plugin patches.

The sample flags check and relink both kinds together, so this is the one place
that does: the CLI asks it, the console and the run record read what it
answers.
"""

from __future__ import annotations

import dataclasses
import logging
import pathlib
from typing import TYPE_CHECKING

from abletoolz.ansi import Y
from abletoolz.live_set.plugin_files import PluginFile
from abletoolz.live_set.sample_ref import SampleRef
from abletoolz.samples import relink
from abletoolz.samples.catalog import SampleCatalog

if TYPE_CHECKING:
    from abletoolz.live_set.document import AbletonSet

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class MissingFiles:
    """What a check found: missing sample references, missing plugin files, and notes about patches."""

    samples: list[SampleRef]
    plugin_files: list[PluginFile]
    notes: list[PluginFile]

    def names(self) -> list[str]:
        """The missing files by name, one per reference; notes are not missing files."""
        return [ref.name for ref in self.samples] + [found.name for found in self.plugin_files]


@dataclasses.dataclass(frozen=True)
class Relinked:
    """What a relink pass did, and the copies it left planned for when the set is saved."""

    relinks: list[relink.Relink]
    collector: relink.Collector | None
    refused: list[relink.Refusal] = dataclasses.field(default_factory=list)

    @property
    def samples(self) -> list[relink.Relink]:
        return [fix for fix in self.relinks if fix.device is None]

    @property
    def plugin_files(self) -> list[relink.Relink]:
        return [fix for fix in self.relinks if fix.device is not None]


class SetFiles:
    """The files one set points at, checked and relinked together."""

    def __init__(self, live_set: AbletonSet) -> None:
        self._set = live_set

    def check(self) -> MissingFiles:
        return MissingFiles(
            samples=self._set.samples.check(),
            plugin_files=self._set.plugin_files.check(),
            notes=self._set.plugin_files.notes(),
        )

    def relink(self, db: SampleCatalog, plugin_files_db: SampleCatalog | None = None, *, collect: bool) -> Relinked:
        """Point every missing file at the one its database finds, collecting audio into the project if asked.

        Audio, Live's own samples and the audio plugin patches point at, is
        found in ``db`` and collected with ``collect``. Files only a plugin
        loads are found in ``plugin_files_db`` and relinked where they are.
        Database records whose file is gone are skipped, and one warning per
        set says how many, so the user knows to run ``--db-prune``.
        """
        collector = None
        if collect:
            project_root = self._set.project_root
            if project_root is None:
                logger.warning("%sNo project folder for this set; relinking to absolute paths instead", Y)
            else:
                collector = relink.Collector(project_root)
        refused: list[relink.Refusal] = []
        gone: set[pathlib.Path] = set()
        relinks = self._set.samples.fix(db, collector, refused, gone) + self._set.plugin_files.fix(
            db, collector, plugin_files_db, refused, gone
        )
        if gone:
            logger.warning(
                "%s%s file(s) in the sample database no longer exist; run abletoolz --db-prune to remove them",
                Y,
                len(gone),
            )
        return Relinked(relinks, collector, refused + (collector.refused if collector is not None else []))
