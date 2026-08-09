"""Sample reference checking and repair."""

from __future__ import annotations

import logging
import pathlib
import shutil
from collections.abc import Generator
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

from abletoolz.live_set import sample_ref
from abletoolz.misc import G, M, R, Y
from abletoolz.sample_databaser.create_db import DatabaseT, SampleRecord
from abletoolz.sample_matcher import is_factory_pack_path, order_candidates_by_name
from abletoolz.versioning import Version

if TYPE_CHECKING:
    from abletoolz.live_set.document import AbletonSet

logger = logging.getLogger(__name__)


class Samples:
    """Sample references of one set."""

    def __init__(self, live_set: AbletonSet) -> None:
        self._set = live_set
        self._cache: list[sample_ref.SampleRef] = []

    @property
    def version(self) -> Version:
        return self._set.version_tuple

    @property
    def _root(self) -> ET.Element:
        return self._set.root

    def iterate(self) -> Generator[sample_ref.SampleRef, None, None]:
        """Iterate through set sample references, parsed once and cached."""
        if self._cache:
            yield from self._cache
            return
        for element in self._root.iter("SampleRef"):
            parsed = sample_ref.SampleRef.from_element(
                element,
                self.version,
                self._set.project_root_folder or pathlib.Path("."),
            )
            self._cache.append(parsed)
            yield parsed

    def check(self) -> list[sample_ref.SampleRef]:
        """Return every sample reference that resolves to no file on disk."""
        missing = []
        for parsed in self.iterate():
            if parsed.absolute_exists or parsed.relative_exists:
                # Sample will load in ableton, no need to do anything.
                logger.debug(
                    "%sSample %s found: Relative %s, Absolute %s",
                    G,
                    parsed.name,
                    parsed.relative,
                    parsed.absolute,
                )
                continue
            missing.append(parsed)
        return missing

    def fix(
        self,
        db: DatabaseT,
        collect_and_save: bool = False,
        force: bool = False,
    ) -> bool:
        """Fix broken sample paths.

        Args:
            db: database loaded from json.
            collect_and_save: copy any found samples into the project folder, the same as ableton's collect
                and save
            force: used with collect_and_save. When the same name sample is found in the project, force replace
                it if the project's current file is a different file size.

        """
        found_samples: DatabaseT = {}
        missing_samples = 0
        fixed_samples = 0
        skip_search = False
        for parsed in self.iterate():
            if parsed.absolute_exists or parsed.relative_exists:
                # Sample will load in ableton, no need to do anything.
                continue
            missing_samples += 1

            # Skip builtin pack content for now. Can revisit this later but these samples probably will fix
            # automatically in ableton on set load.
            if parsed.absolute is None or parsed.absolute.parent is None:
                raise ValueError("Could not parse parent!")

            if is_factory_pack_path(parsed.absolute.parent):
                logger.debug("%sSkipping builtin pack content: %s", Y, parsed.absolute)
                continue

            # There's often the same sample referenced many times in the same set, check previous found first.
            for smp_path, smp_info in found_samples.items():
                if self._fix_one(collect_and_save, parsed, smp_info, smp_path, found_samples, force):
                    fixed_samples += 1
                    skip_search = True
                    break
            if skip_search:
                skip_search = False
                continue
            # Use shared matcher for best candidate selection by name/size/mtime
            original_path = parsed.absolute if parsed.absolute is not None else pathlib.Path(parsed.name)
            ordered_paths = order_candidates_by_name(
                db,
                parsed.name,
                original_path,
                target_length=None,
                target_size=parsed.size,
                target_mtime=parsed.last_modified,
            )
            ordered_candidates = [str(p) for p in ordered_paths] or list(db.keys())

            for smp_path in ordered_candidates:
                smp_info = db.get(smp_path, {})
                if self._fix_one(collect_and_save, parsed, smp_info, smp_path, found_samples, force):
                    fixed_samples += 1
                    break
            else:
                logger.warning(
                    "%sCould not find sample for %s\n%s\n%s",
                    Y,
                    parsed.name,
                    parsed.absolute,
                    parsed.relative,
                )
        logger.info(
            "%sOriginal missing sample count: %s, Samples fixed: %s, Couldn't fix: %s",
            G if fixed_samples == missing_samples else R,
            missing_samples,
            fixed_samples,
            missing_samples - fixed_samples,
        )
        return fixed_samples > 0

    def _fix_one(
        self,
        collect_and_save: bool,
        parsed: sample_ref.SampleRef,
        smp_info: SampleRecord,
        smp_path: str,
        found_samples: DatabaseT,
        force: bool,
    ) -> bool:
        """Attempt to fix sample if matches DB entry.

        size is not always stored in ableton sets unfortunately, but we do usually have last_modified.
        This is not perfect, but the probability of a file name matching and it's last modification time
        matching and being a false positive are quite low.
        """
        if smp_info.get("name") != parsed.name:
            return False
        size_match = parsed.size is not None and smp_info.get("size") == parsed.size
        # The DB stores st_mtime as a json float; sets store whole epoch seconds.
        raw_mtime = smp_info.get("last_modified")
        try:
            last_mod_int = int(float(raw_mtime)) if raw_mtime is not None else None
        except (TypeError, ValueError):
            last_mod_int = None
        modified_match = (
            parsed.last_modified is not None and last_mod_int is not None and parsed.last_modified == last_mod_int
        )
        if not size_match and not modified_match:
            return False

        logger.debug(
            "\n\n%sFound potential match %s, \n[%s]\n%s%s",
            G,
            smp_path,
            smp_info,
            M,
            parsed,
        )
        found_samples[smp_path] = smp_info
        replacement_sample = pathlib.Path(smp_path)
        project_root = self._set.project_root_folder

        if collect_and_save and project_root:
            # Relative type 3 is collected and saved, 1 is absolute path.
            relative_type = parsed.get_relative_type()
            if relative_type == 3:
                rel_path = str(parsed.get_relative_value())
            else:
                rel_path = "Samples/Imported"
            (project_root / rel_path).mkdir(parents=True, exist_ok=True)

            name_value = smp_info.get("name")
            smp_name = name_value if isinstance(name_value, str) and name_value else parsed.name
            copied_sample = project_root / rel_path / smp_name
            if copied_sample.exists() and copied_sample.stat().st_size != parsed.size:
                logger.error(
                    "%sCannot copy sample %s, would replace existing one in project with same name! Skipping...",
                    R,
                    copied_sample,
                )
                return False
            elif copied_sample.exists() and copied_sample.stat().st_size == parsed.size:
                pass
            else:
                shutil.copy(replacement_sample, copied_sample)
            parsed.set_relative(f"{rel_path}/{copied_sample.name}")
            parsed.set_relative_type(3)
        elif collect_and_save and not project_root:
            logger.warning(
                "%sProject root () not found, can't collect and save this sample. Using absolute path instead..",
                Y,
            )
            parsed.set_absolute(replacement_sample)
            parsed.set_relative_type(1)
        else:
            parsed.set_absolute(replacement_sample)
            parsed.set_relative_type(1)
        return True
