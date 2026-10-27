"""Empty slots and generated legacy references found by the library dry run."""

from __future__ import annotations

import copy
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from abletoolz.errors import ElementNotFound
from abletoolz.formats import hex_text, path_data
from abletoolz.live_set.document import AbletonSet
from abletoolz.live_set.sample_ref import SampleRef
from abletoolz.samples.catalog import SampleCatalog
from abletoolz.samples.crc import file_crc


def empty_slot() -> ET.Element:
    return ET.fromstring("""<SampleRef><FileRef>
        <RelativePathType Value="0"/><RelativePath Value=""/><Path Value=""/>
        <Type Value="1"/><LivePackName Value=""/><OriginalFileSize Value="0"/><OriginalCrc Value="0"/>
        </FileRef><LastModDate Value="1377730028"/>
        <SourceContext><SourceContext><OriginalFileRef><FileRef>
        <Path Value="D:/old/shaker.wav"/>
        </FileRef></OriginalFileRef></SourceContext></SourceContext></SampleRef>""")


def test_empty_current_slot_does_not_recover_its_historical_source(tmp_path: Path) -> None:
    live = AbletonSet.from_root(ET.Element("Ableton", Creator="Ableton Live 11.2.10"), tmp_path / "unsaved.als")
    element = empty_slot()
    live.root.append(element)
    before = ET.tostring(live.root)
    (reference,) = live.samples.iterate()
    assert reference.unassigned
    assert reference.absolute is None and reference.name == ""
    assert live.samples.check() == []
    assert not live.samples.fix({})
    assert ET.tostring(live.root) == before


def test_missing_path_attribute_is_still_a_malformed_reference(tmp_path: Path) -> None:
    element = empty_slot()
    path = element.find("FileRef/Path")
    assert path is not None
    path.attrib.clear()
    with pytest.raises(ElementNotFound, match="missing Value"):
        SampleRef(element, (11, 2, 10), tmp_path)


def test_relative_only_sample_can_be_relinked(tmp_path: Path) -> None:
    element = empty_slot()
    relative = element.find("FileRef/RelativePath")
    assert relative is not None
    relative.set("Value", "Samples/Imported/kick.wav")
    replacement = tmp_path / "Library" / "kick.wav"
    replacement.parent.mkdir()
    replacement.write_bytes(b"RIFF")
    size = element.find("FileRef/OriginalFileSize")
    assert size is not None
    size.set("Value", str(replacement.stat().st_size))
    root = ET.Element("Ableton", Creator="Ableton Live 11.2.10")
    root.append(element)
    live = AbletonSet.from_root(root, tmp_path / "unsaved.als", project_root=tmp_path)
    (reference,) = live.samples.iterate()
    assert not reference.unassigned
    assert reference.name == "kick.wav"
    assert live.samples.fix(
        SampleCatalog.from_records({str(replacement): {"name": replacement.name, "size": reference.size}})
    )
    assert reference.absolute == replacement


@pytest.mark.parametrize("transitional", [False, True])
@pytest.mark.parametrize("inline", [False, True])
def test_legacy_paths_and_original_twins_relink_without_indentation(
    tmp_path: Path, transitional: bool, inline: bool
) -> None:
    replacement = tmp_path / "renamed_café.wav"
    replacement.write_bytes(b"RIFF" + bytes(range(128)))
    size, crc = replacement.stat().st_size, file_crc(replacement)
    source = str(tmp_path / "gone" / "old.wav")
    file_ref = ET.Element("FileRef")
    ET.SubElement(file_ref, "RelativePathType", Value="1")
    ET.SubElement(file_ref, "RelativePath")
    ET.SubElement(file_ref, "Name", Value="old.wav")
    ET.SubElement(file_ref, "HasRelativePath", Value="false")
    payload = path_data.encode(source)
    ET.SubElement(file_ref, "Data").text = payload.hex() if inline else hex_text.encode(payload, 4)
    ET.SubElement(file_ref, "OriginalFileSize" if transitional else "FileSize", Value=str(size))
    ET.SubElement(file_ref, "OriginalCrc" if transitional else "Crc", Value=str(crc))
    if transitional:
        ET.SubElement(file_ref, "Path", Value=source)
    element = ET.Element("SampleRef")
    element.append(file_ref)
    ET.SubElement(element, "LastModDate", Value="0")
    context = ET.SubElement(ET.SubElement(element, "SourceContext"), "SourceContext")
    ET.SubElement(context, "OriginalFileRef").append(copy.deepcopy(file_ref))
    root = ET.Element("Ableton", Creator="Ableton Live 10.1.3")
    root.append(element)
    live = AbletonSet.from_root(root, tmp_path / "unsaved.als", project_root=tmp_path)
    (reference,) = live.samples.iterate()
    assert reference.crc == crc
    catalog = SampleCatalog.from_records({str(replacement): {"name": replacement.name, "size": size, "crc": crc}})
    assert live.samples.fix(catalog)
    assert reference.absolute == replacement
    reparsed = SampleRef(element, live.version_tuple, tmp_path)
    assert reparsed.name == replacement.name and reparsed.absolute == replacement
    for current in (file_ref, context.find("OriginalFileRef/FileRef")):
        assert current is not None
        data = current.find("Data")
        assert data is not None
        decoded = path_data.read(data.text or "")
        assert decoded is not None and decoded.path == str(replacement)
        name = current.find("Name")
        assert name is not None and name.get("Value") == replacement.name
        if transitional:
            stored_path = current.find("Path")
            assert stored_path is not None and stored_path.get("Value") == replacement.as_posix()


def test_transitional_checksum_uses_the_same_metadata_family_as_size(tmp_path: Path) -> None:
    element = ET.fromstring("""<SampleRef><FileRef>
        <RelativePathType Value="1"/><RelativePath/><Data>43003A005C0061002E007700610076000000</Data>
        <OriginalFileSize Value="20"/><OriginalCrc Value="1234"/>
        <FileSize Value="10"/><Crc Value="5678"/>
        </FileRef></SampleRef>""")
    reference = SampleRef(element, (10, 1, 3), tmp_path)
    assert (reference.size, reference.crc) == (20, 1234)
