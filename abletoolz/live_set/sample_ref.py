"""Sample reference parsing and mutation.

Live 11 switched FileRef from hex-encoded binary paths (OS-dependent) to plain
string paths; every read/write pair here dispatches on the set version via
``@versioned`` — the undecorated body is the pre-11 form.
"""

from __future__ import annotations

import logging
import os
import pathlib
from xml.etree import ElementTree as ET

import pydantic

from abletoolz import decode_encode
from abletoolz.misc import ElementNotFound, get_element
from abletoolz.utils import parse_hex_path
from abletoolz.versioning import Version, versioned

logger = logging.getLogger(__name__)


def get_sample_size(file_ref: ET.Element) -> int:
    """Read the stored source-file size; OriginalFileSize post-11, FileSize before."""
    for file_size_str in ["OriginalFileSize", "FileSize"]:
        file_size = file_ref.findall(f".//{file_size_str}")
        if len(file_size):
            try:
                return int(file_size[0].get("Value", ""))
            except ValueError as exc:
                raise ElementNotFound from exc
    # Plenty of old sets simply never stored a size; matching falls back to mtime.
    logger.debug("No stored file size in FileRef")
    return 0


def check_relative_path(
    name: str,
    sample_element: ET.Element,
    project_root_folder: pathlib.Path,
) -> tuple[pathlib.Path | None, pathlib.Path | None]:
    """Constructs absolute path from project root and relative path stored in set."""
    if not project_root_folder:
        return None, None
    # Late Live 10 clip FileRefs already use the 11-style shape: RelativePathType +
    # RelativePath present, HasRelativePath gone. Absent means trust RelativePathType.
    relative_path_enabled = get_element(sample_element, "FileRef.HasRelativePath", attribute="Value", silent_error=True)
    relative_path_type = get_element(sample_element, "FileRef.RelativePathType", attribute="Value")
    if relative_path_type == "3" and relative_path_enabled in ("true", None):
        relative_path_element = get_element(sample_element, "FileRef.RelativePath")
        sub_directory_path = []
        for path in relative_path_element:
            sub_directory_path.append(path.get("Dir"))
        from_project_root = f"{os.path.sep.join(sub_directory_path)}{os.path.sep}{name}"
        full_path = project_root_folder / os.path.sep.join(sub_directory_path) / name
        return full_path, from_project_root
    return None, None


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
    last_modified: int
    crc: int
    relative_type_element: ET.Element
    sample_ref: ET.Element
    absolute_element: ET.Element
    relative_element: ET.Element
    version_tuple: Version

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
        last_modified = sample_ref.find("LastModDate").get("Value")
        file_ref = sample_ref.find("FileRef")
        file_size = get_sample_size(file_ref)
        relative_type_element = file_ref.find("RelativePathType")
        if version_tuple >= (11, 0, 0):
            absolute_element = file_ref.find("Path")
            crc = file_ref.find("OriginalCrc").get("Value")
            absolute = file_ref.find("Path").get("Value")
            relative_element = file_ref.find("RelativePath")
            relative = file_ref.find("RelativePath").get("Value")
            name = pathlib.Path(absolute).name
        else:
            absolute_element = file_ref.find("Data")
            absolute = parse_hex_path(absolute_element.text)
            name_element = file_ref.find("Name")
            if name_element is not None:
                name = name_element.get("Value", "")
            else:
                # Transitional 10.1 shape: 11-style fields (Path, no Name) with legacy Data alongside.
                name = pathlib.Path(absolute).name if absolute else ""
            try:
                crc = file_ref.findall(".//Crc")[0].get("Value")
            except IndexError:
                crc = 0
                logger.debug("No Crc in FileRef: %s", ET.tostring(file_ref, encoding="unicode"))
            relative, _ = check_relative_path(name, sample_ref, project_root_folder)
            relative_element = file_ref.find("RelativePath")

        return cls(
            name=name,
            size=file_size,
            last_modified=last_modified,
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
    def absolute_exists(self) -> bool:
        return self.absolute and self.absolute.exists()

    @property
    def relative_exists(self) -> bool:
        return self.relative and self.project_root and (self.project_root / self.relative).exists()

    def get_original_file_ref(self) -> ET.Element:
        return get_element(self.sample_ref, "SourceContext.SourceContext.OriginalFileRef.FileRef")

    @versioned
    def set_absolute(self, path: pathlib.Path) -> None:
        """Pre-11: rewrite the hex Data blob (and its OriginalFileRef twin)."""
        # Get indentation level from current xml data.
        _, levels = decode_encode.xml_to_string(self.absolute_element.text)
        hex_string = decode_encode.string_to_hex(str(path))
        formatted_xml = decode_encode.string_to_xml(hex_string, levels=levels)
        self.absolute_element.text = formatted_xml
        try:
            second_ref = get_element(self.sample_ref, "SourceContext.SourceContext.OriginalFileRef.FileRef.Data")
        except ElementNotFound:
            return
        _, levels = decode_encode.xml_to_string(second_ref.text)
        formatted_xml = decode_encode.string_to_xml(hex_string, levels=levels)
        second_ref.text = formatted_xml

    @set_absolute.since((11, 0, 0))
    def set_absolute(self, path: pathlib.Path) -> None:
        """11+: plain string value."""
        self.absolute_element.set("Value", str(path))

    @versioned
    def set_relative(self, path: str) -> None:
        """Pre-11: rebuild the per-segment RelativePathElement children."""
        old = [e for e in self.relative_element]
        tails = [x.tail for x in old]
        for e in old:
            self.relative_element.remove(e)
        for i, folder in enumerate(path.split("/")[:-1]):
            element = ET.Element("RelativePathElement", attrib={"Id": str(i), "Dir": folder})
            # New path may be deeper than the old one; reuse the last known tail.
            element.tail = tails[i] if i < len(tails) else tails[-1] if tails else None
            self.relative_element.append(element)

    @set_relative.since((11, 0, 0))
    def set_relative(self, path: str) -> None:
        """11+: plain string value."""
        self.relative_element.set("Value", path)

    @versioned
    def get_relative_value(self) -> pathlib.Path:
        """Pre-11: join the RelativePathElement Dir segments."""
        sub_directory_path = []
        for path in self.relative_element:
            sub_directory_path.append(path.get("Dir"))
        return pathlib.Path("/".join(sub_directory_path))

    @get_relative_value.since((11, 0, 0))
    def get_relative_value(self) -> pathlib.Path:
        """11+: the string value's parent directory."""
        return pathlib.Path(self.relative_element.get("Value")).parent

    @versioned
    def set_relative_type(self, type_int: int) -> None:
        """Pre-11: keep HasRelativePath in sync where the set still carries it."""
        try:
            has_rel_ele = get_element(self.sample_ref, "FileRef.HasRelativePath")
        except ElementNotFound:
            has_rel_ele = None  # transitional 10.1 refs dropped the element
        if has_rel_ele is not None:
            if type_int == 3:
                has_rel_ele.set("Value", "true")
            elif type_int in {0, 1}:
                has_rel_ele.set("Value", "false")
        self.relative_type_element.set("Value", str(type_int))

    @set_relative_type.since((11, 0, 0))
    def set_relative_type(self, type_int: int) -> None:
        self.relative_type_element.set("Value", str(type_int))

    def get_relative_type(self) -> int:
        """Get relative path type (integer)."""
        return int(self.relative_type_element.get("Value"))

    def clear_search_hints(self) -> None:
        """Remove search hints, which are the sample paths to folders in abletons browser."""
        for search_hint in self.sample_ref.iter("SearchHint"):
            refs = [e for e in search_hint]
            for ref in refs:
                search_hint.remove(ref)
