"""Random util functions."""
import datetime
import logging
import os
import pathlib
from typing import Optional, Tuple
from xml.etree import ElementTree

import pydantic

from abletoolz import decode_encode
from abletoolz.misc import BACKUP_DIR, B, ElementNotFound, R, Y, get_element

logger = logging.getLogger(__name__)


def format_date(timestamp: float) -> str:
    """Return easy to read date."""
    return datetime.datetime.fromtimestamp(timestamp).strftime("%m/%d/%Y %H:%M:%S")


def create_backup(pathlib_obj: pathlib.Path) -> None:
    """Move file to backup directory, does not replace previous files moved there."""
    backup_dir = pathlib_obj.parent / BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    ending_int = 1
    while True:
        backup_path = backup_dir / (pathlib_obj.stem + "__" + str(ending_int) + pathlib_obj.suffix)
        if pathlib.Path(backup_path).exists():
            ending_int += 1
        else:
            logger.info(
                "%sMoving original file to backup directory:\n%s --> %s",
                B,
                pathlib_obj,
                backup_path,
            )
            # Rename creates a new pathlib object with the new path,
            # so pathlib_obj will still point to the original path when the file is saved.
            pathlib_obj.rename(backup_path)
            return


def parse_mac_data(byte_data: bytes, abs_hash_path: str, debug: bool = False) -> Optional[str]:
    """Parse hex data for absolute path of file on MacOS.

    This hex byte data seems to be some type of struct with data_length_header:data:NULL sections of data, but other
    areas don't follow that pattern at all. This function attempts to grab all 'valid' data_length_header:data:NULL
    sections. The last two are always filepath, and the volume the file is on.
    """
    last_possible_index = len(byte_data) - 1
    itm_lst = []
    i = 6  # First 5 bytes are always a header containing the total amount of bytes.
    while i <= last_possible_index:
        if byte_data[i] == 0 or byte_data[i] == 0xFF:
            i += 1
            continue
        potential_length = byte_data[i]
        potential_end = i + potential_length + 1  # Potential ending NULL byte.
        if (
            potential_end < last_possible_index
            and 0 not in byte_data[i + 1 : potential_end]
            and byte_data[potential_end] == 0
        ):
            if debug:
                logger.info(
                    "\t%si: %s, value at i: %s, potential (e)ndex: %s, data: %s",
                    Y,
                    i,
                    byte_data[i],
                    potential_end,
                    byte_data[i + 1 : potential_end],
                )
            itm_lst.append(byte_data[i + 1 : potential_end])
            i = potential_end + 1
            continue
        i += 1
    try:
        return f"{itm_lst[-1].decode()}{itm_lst[-2].decode()}" if len(itm_lst) >= 2 else None
    except UnicodeDecodeError as e:
        logger.error("\n\n%s Couldn't decode: %s, Error: %s\n\n", R, abs_hash_path, e)
    return None


def parse_windows_data(byte_data: bytes, abs_hash_path: str) -> Optional[str]:
    r"""Parse hex byte data for absolute path of file on Windows.

    Windows hex bytes are utf-16 encoded with \x00 bytes in-between each character.
    """
    try:
        return byte_data.decode("utf-16").replace("\x00", "")  # Remove ending NULL byte.
    except UnicodeDecodeError as e:
        logger.error("\n\n%s Couldn't decode: %s, Error: %s\n\n", R, abs_hash_path, e)
    return None


def parse_hex_path(text: str) -> Optional[str]:
    """Take raw hex string from XML entry and parses."""
    if not text:
        return None
    # Strip new lines and tabs from raw text to have one long hex string.
    abs_hash_path = text.replace("\t", "").replace("\n", "")
    byte_data = bytearray.fromhex(abs_hash_path)
    if byte_data[0:3] == b"\x00" * 3:  # Header only on mac projects.
        # self.set_os = SetOperatingSystem.MAC_OS
        return parse_mac_data(byte_data, abs_hash_path)
    else:
        # self.set_os = SetOperatingSystem.WINDOWS_OS
        return parse_windows_data(byte_data, abs_hash_path)


def get_sample_size(file_ref: ElementTree.Element) -> int:
    for file_size_str in ["OriginalFileSize", "FileSize"]:
        file_size = file_ref.findall(f".//{file_size_str}")
        # if file_size is None:
        #     raise ElementNotFound("Couldn't get sample size!")
        if len(file_size):
            try:
                return int(file_size[0].get("Value", ""))
            except ValueError as exc:
                raise ElementNotFound from exc
    logger.error("Couldn't find filesize!")
    return 0


def path_separator_type(path_str: str) -> str:
    """Get OS path string separator."""
    if "\\" in path_str:
        return "\\"
    elif "/" in path_str:
        return "/"
    else:
        raise Exception(f"Couldn't parse OS path type! {path_str}")


def check_relative_path(
    name: str,
    sample_element: ElementTree.Element,
    project_root_folder: pathlib.Path,
) -> Tuple[Optional[pathlib.Path], Optional[pathlib.Path]]:
    """Constructs absolute path from project root and relative path stored in set."""
    if not project_root_folder:
        return None, None
    relative_path_enabled = get_element(sample_element, "FileRef.HasRelativePath", attribute="Value")
    relative_path_type = get_element(sample_element, "FileRef.RelativePathType", attribute="Value")
    if relative_path_enabled == "true" and relative_path_type == "3":
        relative_path_element = get_element(sample_element, "FileRef.RelativePath")
        sub_directory_path = []
        for path in relative_path_element:
            sub_directory_path.append(path.get("Dir"))
        from_project_root = f"{os.path.sep.join(sub_directory_path)}{os.path.sep}{name}"
        full_path = project_root_folder / os.path.sep.join(sub_directory_path) / name
        return full_path, from_project_root
    return None, None


class SampleRef(pydantic.BaseModel):
    """Sample ref model.

    Parse and recreate XML elements in these formats:

    11+ Example:
        <RelativePathType Value="3" />
        <RelativePath Value="Samples/Imported/2051 BASS.wav" />
        <Path Value="D:/AbletonSync/Drum N Bass/Samples/Imported/2051 BASS.wav" />
        <Type Value="1" />
        <LivePackName Value="" />
        <LivePackId Value="" />
        <OriginalFileSize Value="58394528" />
        <OriginalCrc Value="7866" />

    9 - 10 Example:
        <HasRelativePath Value="true" />
        <RelativePathType Value="3" />
        <RelativePath>
            <RelativePathElement Id="0" Dir="Samples" />
            <RelativePathElement Id="1" Dir="Processed" />
            <RelativePathElement Id="2" Dir="Freeze" />
        </RelativePath>
        <Name Value="Freeze 2-Drum Rack.wav" />
        <Type Value="1" />
        <Data>
            46003A005C004500780070006F00720074005C00410062006C00650074006F006E00200050007200
            6F006A0065006300740073005C0063006800610072006700650064002000500072006F006A006500
            630074005C00530061006D0070006C00650073005C00500072006F00630065007300730065006400
            5C0046007200650065007A0065005C0046007200650065007A006500200032002D00440072007500
            6D0020005200610063006B002E007700610076000000
        </Data>
        <RefersToFolder Value="false" />
        <SearchHint>
            <PathHint>
                <RelativePathElement Id="0" Dir="Ableton Projects" />
                <RelativePathElement Id="1" Dir="charged Project" />
                <RelativePathElement Id="2" Dir="Samples" />
                <RelativePathElement Id="3" Dir="Processed" />
                <RelativePathElement Id="4" Dir="Freeze" />
            </PathHint>
            <FileSize Value="0" />
            <Crc Value="0" />
            <MaxCrcSize Value="0" />
            <HasExtendedInfo Value="false" />
        </SearchHint>
    """

    name: str
    size: int
    last_modified: int
    crc: int
    relative_type_element: ElementTree.Element
    sample_ref: ElementTree.Element
    absolute_element: ElementTree.Element
    relative_element: ElementTree.Element
    version_tuple: Tuple[int, int, int]

    absolute: Optional[pathlib.Path] = None
    relative: Optional[pathlib.Path] = None
    project_root: Optional[pathlib.Path] = None

    class Config:
        """Pydantic config."""

        arbitrary_types_allowed = True

    @classmethod
    def from_element(
        cls,
        sample_ref: ElementTree.Element,
        version_tuple: Tuple[int, int, int],
        project_root_folder: pathlib.Path,
    ) -> "SampleRef":
        """Parse ElementTree into class.

        11 > switched to simple string paths, before 11 used binary encoded data, which
        differs for Windows and MacOs.
        """
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
            name = file_ref.find("Name").get("Value", "")
            try:
                crc = file_ref.findall(".//Crc")[0].get("Value")
            except IndexError:
                crc = 0
                ElementTree.dump(file_ref)
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
        return self.relative and (self.project_root / self.relative).exists()

    def get_original_file_ref(self) -> ElementTree.Element:
        return get_element(self.sample_ref, "SourceContext.SourceContext.OriginalFileRef.FileRef")

    def set_absolute(self, path: pathlib.Path) -> None:
        """Set absolute path for sample ref xml element."""
        if self.version_tuple < (11, 0, 0):
            # Get indentation level from current xml data.
            _, levels = decode_encode.xml_to_string(self.absolute_element.text)
            # Set new value.
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

            return

        self.absolute_element.set("Value", str(path))

    def set_relative(self, path: str) -> None:
        """Set relative path from project root.

        Pre 11
            <HasRelativePath Value="true" />
            <RelativePath>
                <RelativePathElement Dir="Samples" />
                <RelativePathElement Dir="Imported" />
            </RelativePath>

        Post 11
            <RelativePathType Value="3" />
            <RelativePath Value="Samples/Imported/2051 BASS.wav" />
        """
        if self.version_tuple < (11, 0, 0):
            # Clear out old entries
            old = [e for e in self.relative_element]
            tails = [x.tail for x in old]
            for e in old:
                self.relative_element.remove(e)
            for i, folder in enumerate(path.split("/")[:-1]):
                element = ElementTree.Element("RelativePathElement", attrib=dict(Dir=folder))
                element.tail = tails[i]
                self.relative_element.append(element)
            return
        self.relative_element.set("Value", path)

    def get_relative_value(self) -> pathlib.Path:
        """Get path from relative xml element."""
        if self.version_tuple < (11, 0, 0):
            sub_directory_path = []
            for path in self.relative_element:
                sub_directory_path.append(path.get("Dir"))
            return pathlib.Path("/".join(sub_directory_path))
        return pathlib.Path(self.relative_element.get("Value")).parent

    def set_relative_type(self, type_int: int) -> None:
        """Set relative path type.

        <RelativePathType Value="3" />
        1 or 0 is absolute (?)
        3 is relative from project root
        """
        if self.version_tuple < (11, 0, 0):
            try:
                has_rel_ele = get_element(self.sample_ref, "FileRef.HasRelativePath")
                if type_int == 3:
                    has_rel_ele.set("Value", "true")
                elif type_int in {0, 1}:
                    has_rel_ele.set("Value", "false")
            except ElementNotFound:
                pass
        self.relative_type_element.set("Value", str(type_int))

    def get_relative_type(self) -> int:
        """Get relative path type (integer)."""
        return int(self.relative_type_element.get("Value"))

    def clear_search_hints(self) -> None:
        """Remove search hints, which are the sample paths to folders in abletons browser."""
        # search_hints = self.sample_ref.findall("SearchHint")
        for search_hint in self.sample_ref.iter("SearchHint"):
            refs = [e for e in search_hint]
            for ref in refs:
                search_hint.remove(ref)
