"""Sample reference parsing and mutation.

Live 11 switched FileRef from hex-encoded binary paths (OS-dependent) to plain
string paths; every read/write pair here dispatches on the set version via
``@versioned`` — the undecorated body is the pre-11 form.
"""

from __future__ import annotations

import enum
import logging
import os
import pathlib
from xml.etree import ElementTree as ET

import pydantic

from abletoolz.formats import hex_text, path_data
from abletoolz.misc import ElementNotFound, get_element
from abletoolz.schema import Attr, Tag, path
from abletoolz.versioning import Version, versioned

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


def check_relative_path(
    name: str,
    sample_element: ET.Element,
    project_root_folder: pathlib.Path,
) -> tuple[pathlib.Path | None, str | None]:
    """Constructs absolute path from project root and relative path stored in set."""
    if not project_root_folder:
        return None, None
    # Late Live 10 clip FileRefs already use the 11-style shape: RelativePathType +
    # RelativePath present, HasRelativePath gone. Absent means trust RelativePathType.
    relative_path_enabled = get_element(
        sample_element, path(Tag.FILE_REF, Tag.HAS_RELATIVE_PATH), attribute=Attr.VALUE, silent_error=True
    )
    relative_path_type = get_element(sample_element, path(Tag.FILE_REF, Tag.RELATIVE_PATH_TYPE), attribute=Attr.VALUE)
    if relative_path_type == "3" and relative_path_enabled in ("true", None):
        relative_path_element = get_element(sample_element, path(Tag.FILE_REF, Tag.RELATIVE_PATH))
        sub_directory_path: list[str] = []
        for path_element in relative_path_element:
            dir_value = path_element.get(Attr.DIR)
            if dir_value is None:
                raise ElementNotFound(
                    f"RelativePathElement missing Dir attribute: {ET.tostring(path_element, encoding='unicode')}"
                )
            sub_directory_path.append(dir_value)
        from_project_root = f"{os.path.sep.join(sub_directory_path)}{os.path.sep}{name}"
        full_path = project_root_folder / os.path.sep.join(sub_directory_path) / name
        return full_path, from_project_root
    return None, None


class RelativePathType(enum.IntEnum):
    """How Live finds a sample: the two ``RelativePathType`` values abletoolz writes.

    A project reference is found through its ``RelativePath`` inside the set's
    project folder; an absolute one through its ``Path``. Sets hold other
    values too (5, 6, 7 in the finished DnB songs) that abletoolz reads and
    never writes.
    """

    ABSOLUTE = 1
    PROJECT = 3


class SampleRef(pydantic.BaseModel):
    """One SampleRef element: parsed paths plus handles for rewriting them.

    Pre-11 shape: ``FileRef`` holds ``Name``, hex-encoded ``Data`` (absolute
    path), ``HasRelativePath`` + ``RelativePath`` with per-segment
    ``RelativePathElement`` children, and ``SearchHint``. 11+ shape: plain
    ``Path``/``RelativePath`` string values with ``OriginalFileSize``/
    ``OriginalCrc``.
    """

    model_config = pydantic.ConfigDict(arbitrary_types_allowed=True, ignored_types=(versioned,))

    name: str
    size: int
    last_modified: int | None
    crc: int
    relative_type_element: ET.Element
    sample_ref: ET.Element
    absolute_element: ET.Element | None
    relative_element: ET.Element
    version_tuple: Version
    # The Pack a reference names, when it names one. Empty on refs that carry
    # the element with no value -- FL Studio imports do -- so only a non-empty
    # name, with no absolute path beside it, means Live resolves this by Pack.
    live_pack: str | None = None

    absolute: pathlib.Path | None = None
    relative: pathlib.Path | None = None
    project_root: pathlib.Path | None = None

    @property
    def version(self) -> Version:
        """Set version, for @versioned dispatch."""
        return self.version_tuple

    @classmethod
    def from_element(
        cls,
        sample_ref: ET.Element,
        version_tuple: Version,
        project_root_folder: pathlib.Path,
    ) -> SampleRef:
        """Parse ElementTree into class."""
        # Not every reference stores a date. Measured over 811 sets: 98 refs in
        # 14 of them have no LastModDate at all -- Pack references, which
        # address a sample by Pack id rather than by file, and FL Studio
        # imports, which kept a path but none of the file's metadata. All 14 are
        # sets a third-party generator wrote; Live wrote a date on
        # every one of its 76,906 references in the library. Matching already
        # falls back to size the way it does for a missing Crc.
        last_modified_str = get_element(sample_ref, Tag.LAST_MOD_DATE, attribute=Attr.VALUE, silent_error=True)
        file_ref = get_element(sample_ref, Tag.FILE_REF)
        file_size = get_sample_size(file_ref)
        relative_type_element = get_element(file_ref, Tag.RELATIVE_PATH_TYPE)
        # None only on a pre-11 Pack reference, which stores no path element.
        absolute_element: ET.Element | None
        if version_tuple >= (11, 0, 0):
            absolute_element = get_element(file_ref, Tag.PATH)
            absolute = absolute_element.get(Attr.VALUE)
            if not absolute:
                raise ElementNotFound("FileRef.Path missing Value attribute")
            crc_element = get_element(file_ref, Tag.ORIGINAL_CRC)
            crc_str = crc_element.get(Attr.VALUE)
            if crc_str is None:
                raise ElementNotFound("FileRef.OriginalCrc missing Value attribute")
            crc = int(crc_str)
            relative_element = get_element(file_ref, Tag.RELATIVE_PATH)
            relative = relative_element.get(Attr.VALUE)
            if relative is None:
                raise ElementNotFound("FileRef.RelativePath missing Value attribute")
            name = pathlib.Path(absolute).name
        else:
            # A Pack reference stores no Data: the Pack's id is the whole
            # address and there is no path on this machine to read or rewrite.
            absolute_element = get_element(file_ref, Tag.DATA, silent_error=True)
            path_blob = path_data.read(absolute_element.text or "") if absolute_element is not None else None
            absolute = (path_blob.path or "") if path_blob is not None else ""
            name_element = file_ref.find(Tag.NAME)
            if name_element is not None:
                name = name_element.get(Attr.VALUE, "")
            else:
                # Transitional 10.1 shape: 11-style fields (Path, no Name) with legacy Data alongside.
                name = pathlib.Path(absolute).name if absolute else ""
            try:
                crc_element = file_ref.findall(".//" + Tag.CRC)[0]
            except IndexError:
                crc = 0
                logger.debug("No Crc in FileRef: %s", ET.tostring(file_ref, encoding="unicode"))
            else:
                # Legitimately absent on plenty of old sets; 0 matches the IndexError fallback above.
                crc_str = crc_element.get(Attr.VALUE)
                crc = int(crc_str) if crc_str is not None else 0
            # check_relative_path returns (full_path, relative_str); this field wants the
            # relative form (relative_exists joins it back onto project_root itself).
            _, relative = check_relative_path(name, sample_ref, project_root_folder)
            relative_element = get_element(file_ref, Tag.RELATIVE_PATH)

        return cls(
            name=name,
            size=file_size,
            last_modified=int(last_modified_str) if last_modified_str is not None else None,
            live_pack=get_element(file_ref, Tag.LIVE_PACK_NAME, attribute=Attr.VALUE, silent_error=True),
            relative_type_element=relative_type_element,
            relative_element=relative_element,
            sample_ref=sample_ref,
            crc=crc,
            absolute_element=absolute_element,
            absolute=pathlib.Path(absolute) if absolute else None,
            relative=pathlib.Path(relative) if relative else None,
            project_root=project_root_folder,
            version_tuple=version_tuple,
        )

    @property
    def pack_resolved(self) -> bool:
        """Whether Live resolves this reference through an installed Pack.

        A Pack reference names the Pack and the file inside it and stores no
        absolute path at all, so nothing on this filesystem answers for it: it
        cannot be checked for existence and must not be rewritten to a path.
        Measured on generated sets built from Core Library content, where the
        FileRef carries ``LivePackName``/``LivePackId`` and ``RelativePathType``
        5 in place of ``Data``. A reference that kept its ``Data`` is an ordinary
        file reference whatever else it names, which is what keeps FL Studio
        imports -- they carry an empty ``LivePackName`` beside a real path --
        out of this.
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

    def get_original_file_ref(self) -> ET.Element:
        return get_element(self.sample_ref, _ORIGINAL_FILE_REF)

    @versioned
    def set_absolute(self, path: pathlib.Path) -> None:
        """Pre-11: rewrite the hex Data blob (and its OriginalFileRef twin)."""
        if self.absolute_element is None:
            raise ElementNotFound("FileRef has no Data element; a Pack reference stores no path to rewrite")
        payload = path_data.encode(str(path))
        self.absolute_element.text = hex_text.encode(payload, hex_text.depth(self.absolute_element.text or ""))
        try:
            second_ref = get_element(self.sample_ref, _ORIGINAL_FILE_DATA)
        except ElementNotFound:
            return
        second_ref.text = hex_text.encode(payload, hex_text.depth(second_ref.text or ""))

    @set_absolute.since((11, 0, 0))  # type: ignore[no-redef]  # @versioned pattern: same name registers the 11+ override
    def set_absolute(self, path: pathlib.Path) -> None:
        """11+: plain string value, spelled with forward slashes as Live spells it.

        Measured 2026-09-27: 26,762 of 26,764 paths in 100 Live 11 and 12 sets
        use forward slashes on Windows; the two that did not were written by
        abletoolz.
        """
        if self.absolute_element is None:
            raise ElementNotFound("FileRef has no Path element to rewrite")
        self.absolute_element.set(Attr.VALUE, path.as_posix())

    @versioned
    def set_relative(self, path: str) -> None:
        """Pre-11: rebuild the per-segment RelativePathElement children."""
        old = [e for e in self.relative_element]
        tails = [x.tail for x in old]
        for e in old:
            self.relative_element.remove(e)
        for i, folder in enumerate(path.split("/")[:-1]):
            element = ET.Element(Tag.RELATIVE_PATH_ELEMENT, attrib={Attr.ID: str(i), Attr.DIR: folder})
            # New path may be deeper than the old one; reuse the last known tail.
            element.tail = tails[i] if i < len(tails) else tails[-1] if tails else None
            self.relative_element.append(element)

    @set_relative.since((11, 0, 0))  # type: ignore[no-redef]  # @versioned pattern: same name registers the 11+ override
    def set_relative(self, path: str) -> None:
        """11+: plain string value."""
        self.relative_element.set(Attr.VALUE, path)

    @versioned
    def get_relative_value(self) -> pathlib.Path:
        """Pre-11: join the RelativePathElement Dir segments."""
        sub_directory_path: list[str] = []
        for path_element in self.relative_element:
            dir_value = path_element.get(Attr.DIR)
            if dir_value is None:
                raise ElementNotFound(
                    f"RelativePathElement missing Dir attribute: {ET.tostring(path_element, encoding='unicode')}"
                )
            sub_directory_path.append(dir_value)
        return pathlib.Path("/".join(sub_directory_path))

    @get_relative_value.since((11, 0, 0))  # type: ignore[no-redef]  # @versioned pattern: same name registers the 11+ override
    def get_relative_value(self) -> pathlib.Path:
        """11+: the string value's parent directory."""
        value = self.relative_element.get(Attr.VALUE)
        if value is None:
            raise ElementNotFound("RelativePath missing Value attribute")
        return pathlib.Path(value).parent

    @versioned
    def set_relative_type(self, type_int: int) -> None:
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

    @set_relative_type.since((11, 0, 0))  # type: ignore[no-redef]  # @versioned pattern: same name registers the 11+ override
    def set_relative_type(self, type_int: int) -> None:
        self.relative_type_element.set(Attr.VALUE, str(type_int))

    @property
    def found_only_by_absolute_path(self) -> bool:
        """A project reference whose relative path is gone while its absolute path still exists.

        Live looks for a project reference inside the project, and reported
        such samples missing (2026-09-27, sets written outside the project
        they came from). abletoolz used to count them found because the
        absolute path exists; the fixer now repoints them to that path.
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

    def clear_search_hints(self) -> None:
        """Remove search hints, which are the sample paths to folders in abletons browser."""
        for search_hint in self.sample_ref.iter(Tag.SEARCH_HINT):
            refs = [e for e in search_hint]
            for ref in refs:
                search_hint.remove(ref)
