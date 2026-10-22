"""Known Live loader failures must be rejected before publishing a document."""

from __future__ import annotations

import gzip
import pathlib
from xml.etree import ElementTree as ET

import pytest

from abletoolz.formats import FormatError, live_document
from abletoolz.live_set.document import AbletonSet


def audio_document(*, warped: bool = False, markers: bool = True) -> ET.Element:
    root = ET.Element("Ableton")
    clip = ET.SubElement(root, "AudioClip", Id="7")
    ET.SubElement(clip, "Name", Value="Original reference")
    ET.SubElement(clip, "IsWarped", Value=str(warped).lower())
    array = ET.SubElement(clip, "WarpMarkers")
    if markers:
        ET.SubElement(array, "WarpMarker", Id="0", SecTime="0", BeatTime="0")
        ET.SubElement(array, "WarpMarker", Id="1", SecTime="2", BeatTime="4")
    return root


@pytest.mark.parametrize("warped", [False, True])
def test_encoding_refuses_empty_warp_markers_even_for_unwarped_audio(warped: bool) -> None:
    root = audio_document(warped=warped, markers=False)
    with pytest.raises(FormatError, match="Original reference.*Empty warp marker array"):
        live_document.encode(root)


def test_encoding_refuses_missing_warp_markers() -> None:
    root = audio_document()
    clip = root.find("AudioClip")
    assert clip is not None
    markers = clip.find("WarpMarkers")
    assert markers is not None
    clip.remove(markers)
    with pytest.raises(FormatError, match="Empty warp marker array"):
        live_document.encode(root)


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("create_new", [False, True])
def test_byte_writers_validate_before_touching_disk(tmp_path: pathlib.Path, compressed: bool, create_new: bool) -> None:
    target = tmp_path / "audition.als"
    original = live_document.encode(audio_document())
    if not create_new:
        target.write_bytes(original)
    document = live_document.to_xml_bytes(audio_document(markers=False))
    if compressed:
        document = gzip.compress(document)
    writer = live_document.write_new if create_new else live_document.write
    with pytest.raises(FormatError, match="Empty warp marker array"):
        writer(target, document)
    if create_new:
        assert list(tmp_path.iterdir()) == []
    else:
        assert target.read_bytes() == original
        assert sorted(path.name for path in tmp_path.iterdir()) == [target.name]


def test_save_rejects_invalid_audio_before_moving_the_original_to_backup(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "original.als"
    original = live_document.encode(audio_document())
    target.write_bytes(original)
    document = AbletonSet(target)
    document._root = audio_document(markers=False)
    with pytest.raises(FormatError, match="Empty warp marker array"):
        document.save_set()
    assert target.read_bytes() == original
    assert not (tmp_path / "abletoolz_backup").exists()
    assert document.path == target


def test_validation_preserves_valid_markers_and_allows_diagnostic_reads(tmp_path: pathlib.Path) -> None:
    root = audio_document()
    expected = ET.tostring(root)
    encoded = live_document.encode(root)
    assert ET.tostring(live_document.parse(encoded)) == expected
    assert ET.tostring(root) == expected
    broken = live_document.to_xml_bytes(audio_document(markers=False))
    assert live_document.parse(broken).find("AudioClip/WarpMarkers") is not None
    path = tmp_path / "valid.alc"
    live_document.write_new(path, encoded)
    assert ET.tostring(live_document.read(path)) == expected


def test_atomic_publisher_still_accepts_json_reports(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "audition.als.recovery.json"
    report = b'{"written": "audition.als"}'
    live_document.write_new(path, report)
    assert path.read_bytes() == report
    with pytest.raises(FileExistsError):
        live_document.write_new(path, b"{}")
    assert path.read_bytes() == report
