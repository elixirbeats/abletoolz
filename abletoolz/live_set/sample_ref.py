"""Sample reference parsing and mutation.

Live 11 switched FileRef from hex-encoded binary paths (OS-dependent) to plain
string paths; the reader and every writer here dispatch on the set version via
``@versioned`` — the undecorated body is the pre-11 form.

Callers rewrite a reference only through ``point_absolute`` and
``point_relative``, which write a path together with the ``RelativePathType``
that makes Live use it; a path written without its type is the stale-reference
bug both of them exist to rule out.
"""

from __future__ import annotations

import enum
import logging
import pathlib
from xml.etree import ElementTree as ET

from abletoolz.elements import get_element
from abletoolz.errors import ElementNotFound
from abletoolz.formats import hex_text, path_data
from abletoolz.schema import Attr, Tag, path
from abletoolz.schema.versioning import Version, versioned

logger = logging.getLogger(__name__)

# The FileRef a SampleRef keeps of where the sample first came from.
_ORIGINAL_FILE_REF = path(Tag.SOURCE_CONTEXT, Tag.SOURCE_CONTEXT, Tag.ORIGINAL_FILE_REF, Tag.FILE_REF)
_ORIGINAL_FILE_DATA = path(_ORIGINAL_FILE_REF, Tag.DATA)


def get_sample_size(file_ref: ET.Element) -> int:
    """Read the stored source-file size; OriginalFileSize post-11, FileSize before."""
    for file_size_str in [Tag.ORIGINAL_FILE_SIZE, Tag.FILE_SIZE]:
        file_size = file_ref.findall(f".//{file_size_str}")
        if len(file_size):
            try:
                return int(file_size[0].get(Attr.VALUE, ""))
            except ValueError as exc:
                raise ElementNotFound from exc
    # Plenty of old sets simply never stored a size; matching falls back to mtime.
    logger.debug("No stored file size in FileRef")
    return 0


class RelativePathType(enum.IntEnum):
    """How Live finds a sample: the two ``RelativePathType`` values abletoolz writes.

    A project reference is found through its ``RelativePath`` inside the set's
    project folder; an absolute one through its ``Path``. Sets hold other
    values too (5, 6, 7 in the finished DnB songs) that abletoolz reads and
    never writes.
    """

    ABSOLUTE = 1
    PROJECT = 3


class Resolution(enum.StrEnum):
    """How Live will find a reference's sample, judged from this machine."""

    UNASSIGNED = "unassigned"  # an empty sample slot: nothing to find
    PACK = "pack"  # out of an installed Pack; no path here confirms or denies it
    ONLY_BY_ABSOLUTE = "only_by_absolute"  # a project reference found only through its absolute path
    LOADS = "loads"
    MISSING = "missing"


class SampleRef:
    """One SampleRef element: parsed paths plus handles for rewriting them.

    Pre-11 shape: ``FileRef`` holds ``Name``, hex-encoded ``Data`` (absolute
    path), ``HasRelativePath`` + ``RelativePath`` with per-segment
    ``RelativePathElement`` children, and ``SearchHint``. 11+ shape: plain
    ``Path``/``RelativePath`` string values with ``OriginalFileSize``/
    ``OriginalCrc``.

    ``project_root`` is the folder a project reference is found in, the set's
    own; without one, only the absolute path can show that a sample exists.
    """

    # None only on a pre-11 Pack reference, which stores no path element.
    absolute_element: ET.Element | None
    absolute: pathlib.Path | None
    relative: pathlib.Path | None
    name: str
    # The checksum ``abletoolz.samples.crc`` computes, or None when the set
    # stores none or zero. Live often writes OriginalCrc 0 together with
    # OriginalFileSize 0 (6,038 of 64,184 references measured; the two always
    # appear together), so a crc counts only beside a non-zero size.
    crc: int | None

    def __init__(self, sample_ref: ET.Element, version: Version, project_root: pathlib.Path | None = None) -> None:
        """Parse one SampleRef element of a set saved by ``version``."""
        self.sample_ref = sample_ref
        self.version_tuple = version
        self.project_root = project_root
        file_ref = get_element(sample_ref, Tag.FILE_REF)
        # Not every reference stores a date. Measured over 811 sets: 98 refs in
        # 14 of them have no LastModDate -- Pack references, which address a
        # sample by Pack id rather than by file, and FL Studio imports, which
        # kept a path but none of the file's metadata. All 14 are sets a
        # third-party generator wrote; Live wrote a date on all 76,906 of its
        # references in the library. Matching falls back to size, as for a
        # missing Crc.
        last_modified = get_element(sample_ref, Tag.LAST_MOD_DATE, attribute=Attr.VALUE, silent_error=True)
        self.last_modified = int(last_modified) if last_modified is not None else None
        self.size = get_sample_size(file_ref)
        self.relative_type_element = get_element(file_ref, Tag.RELATIVE_PATH_TYPE)
        self.relative_element = get_element(file_ref, Tag.RELATIVE_PATH)
        # The Pack a reference names, when it names one. Empty on refs that carry
        # the element with no value -- FL Studio imports do -- so only a non-empty
        # name, with no absolute path beside it, means Live resolves this by Pack.
        self.live_pack = get_element(file_ref, Tag.LIVE_PACK_NAME, attribute=Attr.VALUE, silent_error=True)
        self._read(file_ref)
        if self.crc == 0:
            logger.warning("Sample %s stores CRC 0; checksum matching is disabled for this reference.", self.name)
            self.crc = None

    @property
    def version(self) -> Version:
        """Set version, for @versioned dispatch."""
        return self.version_tuple

    @versioned
    def _read(self, file_ref: ET.Element) -> None:
        """Pre-11: the hex ``Data`` blob, ``Name``, and the ``RelativePathElement`` folders."""
        # A Pack reference stores no Data: the Pack's id is the whole
        # address and there is no path on this machine to read or rewrite.
        self.absolute_element = get_element(file_ref, Tag.DATA, silent_error=True)
        blob = path_data.read(self.absolute_element.text or "") if self.absolute_element is not None else None
        absolute = (blob.path or "") if blob is not None else ""
        self.absolute = pathlib.Path(absolute) if absolute else None
        name_element = file_ref.find(Tag.NAME)
        if name_element is not None:
            self.name = name_element.get(Attr.VALUE, "")
        else:
            # Transitional 10.1 shape: 11-style fields (Path, no Name) with legacy Data alongside.
            self.name = pathlib.Path(absolute).name if absolute else ""
        try:
            crc_element = next(
                element for tag in (Tag.ORIGINAL_CRC, Tag.CRC) for element in file_ref.findall(f".//{tag}")
            )
        except StopIteration:
            self.crc = None
            logger.debug("No Crc in FileRef: %s", ET.tostring(file_ref, encoding="unicode"))
        else:
            # Legitimately absent on plenty of old sets.
            crc = crc_element.get(Attr.VALUE)
            self.crc = int(crc) if crc is not None and self.size else None
        # Late Live 10 clip FileRefs already use the 11-style shape: RelativePathType +
        # RelativePath present, HasRelativePath gone. Absent means trust RelativePathType.
        has_relative = get_element(file_ref, Tag.HAS_RELATIVE_PATH, attribute=Attr.VALUE, silent_error=True)
        self.relative = None
        if self.get_relative_type() == RelativePathType.PROJECT and has_relative in ("true", None):
            folders: list[str] = []
            for element in self.relative_element:
                folder = element.get(Attr.DIR)
                if folder is None:
                    raise ElementNotFound(
                        f"RelativePathElement missing Dir attribute: {ET.tostring(element, encoding='unicode')}"
                    )
                folders.append(folder)
            self.relative = pathlib.Path(*folders, self.name)

    @_read.since((11, 0, 0))  # type: ignore[no-redef]  # @versioned pattern: same name registers the 11+ override
    def _read(self, file_ref: ET.Element) -> None:
        """11+: plain ``Path`` and ``RelativePath`` string values."""
        self.absolute_element = get_element(file_ref, Tag.PATH)
        absolute = self.absolute_element.get(Attr.VALUE)
        if absolute is None:
            raise ElementNotFound("FileRef.Path missing Value attribute")
        crc = get_element(file_ref, Tag.ORIGINAL_CRC).get(Attr.VALUE)
        if crc is None:
            raise ElementNotFound("FileRef.OriginalCrc missing Value attribute")
        self.crc = int(crc) if self.size else None
        relative = self.relative_element.get(Attr.VALUE)
        if relative is None:
            raise ElementNotFound("FileRef.RelativePath missing Value attribute")
        self.absolute = pathlib.Path(absolute) if absolute else None
        self.relative = pathlib.Path(relative) if relative else None
        self.name = pathlib.Path(absolute or relative).name if absolute or relative else ""

    @property
    def unassigned(self) -> bool:
        """An empty sample slot has no current file to check or relink."""
        return not any((self.name, self.absolute, self.relative, self.live_pack))

    @property
    def pack_resolved(self) -> bool:
        """Whether Live resolves this reference through an installed Pack.

        A Pack reference names the Pack and the file inside it and stores no
        absolute path, so it cannot be checked for existence and must not be
        rewritten to a path. Measured on generated sets built from Core Library
        content, where the FileRef carries ``LivePackName``/``LivePackId`` and
        ``RelativePathType`` 5 in place of ``Data``. A reference that kept its
        ``Data`` is an ordinary file reference whatever else it names, which
        keeps out FL Studio imports (an empty ``LivePackName`` beside a real path).
        """
        return self.absolute_element is None and bool(self.live_pack)

    @property
    def absolute_exists(self) -> bool:
        return self.absolute is not None and self.absolute.exists()

    @property
    def relative_exists(self) -> bool:
        if self.relative is None or self.project_root is None:
            return False
        return (self.project_root / self.relative).exists()

    @property
    def resolution(self) -> Resolution:
        """Where this reference stands, the one classification the check and the fixer share.

        Checked in this order because each answer settles the ones after it: a
        Pack reference has no path to test, and a project reference found only
        through its absolute path exists on disk yet Live reports it missing.
        """
        if self.unassigned:
            return Resolution.UNASSIGNED
        if self.pack_resolved:
            return Resolution.PACK
        if self.found_only_by_absolute_path:
            return Resolution.ONLY_BY_ABSOLUTE
        if self.absolute_exists or self.relative_exists:
            return Resolution.LOADS
        return Resolution.MISSING

    def point_absolute(self, path: pathlib.Path, *, size: int | None = None) -> None:
        """Make Live find the sample through its absolute ``Path``.

        The path and ``RelativePathType`` 1 are written together: a reference
        left typed as a project reference is still looked for in the project.
        ``size`` also rewrites the stored file size, when given.
        """
        self._set_absolute(path)
        self._set_relative_type(RelativePathType.ABSOLUTE)
        if size is not None:
            self._set_size(size)

    def point_relative(
        self,
        relative: pathlib.PurePath,
        kind: int,
        *,
        absolute: pathlib.Path | None = None,
        size: int | None = None,
    ) -> None:
        """Make Live find the sample through ``RelativePath``, of type ``kind``.

        ``RelativePath`` and its type are written together because Live resolves
        ``RelativePath`` first: a stale one beside a new ``Path`` makes Live load
        the old sample (observed in Live with an ``.alc`` clip). ``kind`` is a
        ``RelativePathType`` value -- ``PROJECT``, or another root such as the
        User Library (6) an ``.alc`` clip uses. ``absolute`` rewrites ``Path``
        too, the fallback Live uses when the relative path does not resolve;
        ``size`` rewrites the stored file size.
        """
        if absolute is not None:
            self._set_absolute(absolute)
        self._set_relative(relative.as_posix())
        self._set_relative_type(kind)
        if size is not None:
            self._set_size(size)

    def _set_size(self, size: int) -> None:
        """Rewrite the stored file size, the same element ``get_sample_size`` reads."""
        file_ref = get_element(self.sample_ref, Tag.FILE_REF)
        for tag in (Tag.ORIGINAL_FILE_SIZE, Tag.FILE_SIZE):
            element = file_ref.find(f".//{tag}")
            if element is not None:
                element.set(Attr.VALUE, str(size))
                self.size = size
                return
        raise ElementNotFound("FileRef stores no file size to rewrite")

    def _set_name(self, name: str) -> None:
        name_element = self.sample_ref.find(path(Tag.FILE_REF, Tag.NAME))
        if name_element is not None:
            name_element.set(Attr.VALUE, name)
        self.name = name

    @versioned
    def _set_absolute(self, path: pathlib.Path) -> None:
        """Pre-11: rewrite the hex Data blob (and its OriginalFileRef twin)."""
        if self.absolute_element is None:
            raise ElementNotFound("FileRef has no Data element; a Pack reference stores no path to rewrite")
        payload = path_data.encode(str(path))
        self.absolute_element.text = hex_text.encode(payload, hex_text.depth(self.absolute_element.text or ""))
        modern_path = self.sample_ref.find(f"{Tag.FILE_REF}/{Tag.PATH}")
        if modern_path is not None:
            modern_path.set(Attr.VALUE, path.as_posix())
        self.absolute = path
        self._set_name(path.name)
        second_ref = self.sample_ref.find(_ORIGINAL_FILE_DATA)
        if second_ref is not None:
            second_ref.text = hex_text.encode(payload, hex_text.depth(second_ref.text or ""))
        original_path = self.sample_ref.find(f"{_ORIGINAL_FILE_REF}/{Tag.PATH}")
        if original_path is not None:
            original_path.set(Attr.VALUE, path.as_posix())
        original_name = self.sample_ref.find(f"{_ORIGINAL_FILE_REF}/{Tag.NAME}")
        if original_name is not None:
            original_name.set(Attr.VALUE, path.name)

    @_set_absolute.since((11, 0, 0))  # type: ignore[no-redef]  # @versioned pattern: same name registers the 11+ override
    def _set_absolute(self, path: pathlib.Path) -> None:
        """11+: plain string value, spelled with forward slashes as Live spells it.

        Measured 2026-09-27: 26,762 of 26,764 paths in 100 Live 11 and 12 sets
        use forward slashes on Windows; the two that did not were written by
        abletoolz.
        """
        if self.absolute_element is None:
            raise ElementNotFound("FileRef has no Path element to rewrite")
        self.absolute_element.set(Attr.VALUE, path.as_posix())
        self.absolute = path
        self._set_name(path.name)

    @versioned
    def _set_relative(self, path: str) -> None:
        """Pre-11: rebuild the per-segment RelativePathElement children."""
        tails = [element.tail for element in self.relative_element]
        del self.relative_element[:]
        for i, folder in enumerate(path.split("/")[:-1]):
            element = ET.Element(Tag.RELATIVE_PATH_ELEMENT, attrib={Attr.ID: str(i), Attr.DIR: folder})
            # New path may be deeper than the old one; reuse the last known tail.
            element.tail = tails[i] if i < len(tails) else tails[-1] if tails else None
            self.relative_element.append(element)
        self.relative = pathlib.Path(path)
        self._set_name(self.relative.name)

    @_set_relative.since((11, 0, 0))  # type: ignore[no-redef]  # @versioned pattern: same name registers the 11+ override
    def _set_relative(self, path: str) -> None:
        """11+: plain string value."""
        self.relative_element.set(Attr.VALUE, path)
        self.relative = pathlib.Path(path)
        self._set_name(self.relative.name)

    @versioned
    def _set_relative_type(self, type_int: int) -> None:
        """Pre-11: keep HasRelativePath in sync where the set still carries it."""
        try:
            has_rel_ele = get_element(self.sample_ref, path(Tag.FILE_REF, Tag.HAS_RELATIVE_PATH))
        except ElementNotFound:
            has_rel_ele = None  # transitional 10.1 refs dropped the element
        if has_rel_ele is not None:
            if type_int == 3:
                has_rel_ele.set(Attr.VALUE, "true")
            elif type_int in {0, 1}:
                has_rel_ele.set(Attr.VALUE, "false")
        self.relative_type_element.set(Attr.VALUE, str(type_int))

    @_set_relative_type.since((11, 0, 0))  # type: ignore[no-redef]  # @versioned pattern: same name registers the 11+ override
    def _set_relative_type(self, type_int: int) -> None:
        self.relative_type_element.set(Attr.VALUE, str(type_int))

    @property
    def found_only_by_absolute_path(self) -> bool:
        """A project reference whose relative path is gone while its absolute path still exists.

        Live looks for a project reference inside the project, and reported
        such samples missing (2026-09-27, sets written outside the project
        they came from). The fixer repoints them to the absolute path.
        """
        return (
            self.absolute_exists and not self.relative_exists and self.get_relative_type() == RelativePathType.PROJECT
        )

    def get_relative_type(self) -> int:
        """Get relative path type (integer)."""
        value = self.relative_type_element.get(Attr.VALUE)
        if value is None:
            raise ElementNotFound("RelativePathType missing Value attribute")
        return int(value)
