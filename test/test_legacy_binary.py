from __future__ import annotations

import struct
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from abletoolz.experimental.legacy import convert_file
from abletoolz.experimental.legacy.legacy_document import ClassType, DecodeError, decode
from abletoolz.formats import live_document
from abletoolz.live_set.undo import history, restore
from abletoolz.schema.upgrade import upgrade


def kind(name: str, revision: int = 0) -> bytes:
    encoded = name.encode("ascii")
    return bytes((0, revision, len(encoded))) + encoded


def string(value: str) -> bytes:
    encoded = value.encode("utf-16le")
    return struct.pack("<I", len(encoded) // 2) + encoded


def definition(name: str, fields: list[tuple[str, bytes]], revision: int = 0) -> bytes:
    return kind(name, revision) + struct.pack("<i", len(fields)) + b"".join(string(n) + t for n, t in fields)


def document(definitions: list[bytes], data: bytes) -> bytes:
    return (
        bytes.fromhex("ab1e5678039b000000")
        + kind("LiveDocument")
        + struct.pack("<i", len(definitions))
        + b"".join(definitions)
        + data
    )


def test_list_allocation_is_not_length_and_sparse_ids_survive() -> None:
    root = definition("LiveDocument", [("Items", kind("List"))])
    container = kind("List") + struct.pack("<i", -1)
    item = definition("Item", [("Number", b"\x11")])
    values = (
        struct.pack("<I", 19)
        + kind("Item")
        + struct.pack("<Ii", 3, -12)
        + kind("Item")
        + struct.pack("<Ii", 17, 42)
        + kind("")
    )
    result = decode(document([root, container, item], values)).root.field("Items")
    assert result.allocation == 19
    assert [e.identifier for e in result.entries] == [3, 17]
    assert [e.node.field("Number").atom for e in result.entries] == [-12, 42]


def test_array_count_and_element_type_are_preserved() -> None:
    root = definition("LiveDocument", [("Items", kind("Array"))])
    container = kind("Array") + struct.pack("<i", -3)
    result = decode(document([root, container], struct.pack("<I", 3) + b"\x11" + struct.pack("<iii", 4, -5, 6)))
    array = result.root.field("Items")
    assert array.element_type == 0x11
    assert [e.node.atom for e in array.entries] == [4, -5, 6]
    assert all(e.identifier is None for e in array.entries)


def test_revisions_are_part_of_class_identity() -> None:
    root = definition("LiveDocument", [("Old", kind("Thing", 0)), ("New", kind("Thing", 1))])
    old = definition("Thing", [("Value", b"\x11")], 0)
    new = definition("Thing", [("Value", b"\x17")], 1)
    result = decode(document([root, old, new], struct.pack("<id", 12, 2.5)))
    assert result.root.field("Old").field("Value").atom == 12
    assert result.root.field("New").field("Value").atom == 2.5


def test_type_reference_primitive_is_not_a_uuid() -> None:
    result = decode(document([definition("LiveDocument", [("DeviceId", b"\x20")])], kind("Operator", 3)))
    assert result.root.field("DeviceId").atom == ClassType(3, "Operator")


@pytest.mark.parametrize("value", [2, 255])
def test_invalid_boolean_is_rejected(value: int) -> None:
    with pytest.raises(DecodeError, match="invalid boolean"):
        decode(document([definition("LiveDocument", [("Enabled", b"\x10")])], bytes((value,))))


def test_unsupported_primitive_is_rejected() -> None:
    with pytest.raises(DecodeError, match="unsupported primitive"):
        decode(document([definition("LiveDocument", [("Unknown", b"\xff")])], b""))


def test_trailing_bytes_are_rejected() -> None:
    with pytest.raises(DecodeError, match="trailing bytes"):
        decode(document([definition("LiveDocument", [])], b"extra"))


def test_unknown_dialect_is_rejected() -> None:
    data = bytearray(document([definition("LiveDocument", [])], b""))
    data[4] = 5
    with pytest.raises(DecodeError, match="unsupported document dialect"):
        decode(bytes(data))


def minimal_set() -> bytes:
    """Synthetic serialization fixture, not evidence of a native Live save."""
    definitions = [
        definition("LiveDocument", [("Tracks", kind("RemoteableList")), ("MasterTrack", kind("MasterTrack"))]),
        kind("RemoteableList") + struct.pack("<i", -1),
        definition("MasterTrack", [("MasterChain", kind("MasterTrackDeviceChain"))]),
        definition("MasterTrackDeviceChain", [("MasterDevice", kind("MasterTrackDevice"))]),
        definition("MasterTrackDevice", [("Tempo", kind("TimeableFloat"))]),
        definition("TimeableFloat", [("ArrangerAutomation", kind("ListAutomation"))]),
        definition("ListAutomation", [("Events", kind("RemoteableArray"))]),
        kind("RemoteableArray") + struct.pack("<i", -3),
        definition("FloatEvent", [("Time", b"\x17"), ("Value", b"\x12")]),
    ]
    values = struct.pack("<I", 0) + kind("") + struct.pack("<I", 1) + kind("FloatEvent")
    return document(definitions, values + struct.pack("<df", -63072000, 120))


def test_converting_in_place_backs_up_the_binary_set_and_undo_puts_it_back(tmp_path: Path) -> None:
    source = tmp_path / "old.als"
    original = minimal_set()
    source.write_bytes(original)
    result = convert_file(source)
    assert result.live_set.path == source.resolve()
    assert live_document.read(source).get("MinorVersion") == "8.2_260"
    (backup,) = (tmp_path / "abletoolz_backup").iterdir()
    assert backup.read_bytes() == original
    (last,) = history()
    restore(last)
    assert source.read_bytes() == original


def test_binary_to_new_xml_round_trip(tmp_path: Path) -> None:
    source, destination = tmp_path / "old.als", tmp_path / "new.als"
    original = minimal_set()
    source.write_bytes(original)
    result = convert_file(source, destination)
    root = live_document.read(destination)
    assert root.get("MinorVersion") == "8.2_260"
    assert result.live_set.path == destination
    assert source.read_bytes() == original
    event = root.find("LiveSet/MasterTrack/MasterChain/Mixer/Tempo/ArrangerAutomation/Events/FloatEvent")
    assert event is not None and float(event.attrib["Value"]) == 120
    assert result.report.warnings
    assert destination.read_bytes()[:10] == bytes.fromhex("1f8b08000000000002ff")


@pytest.mark.parametrize("mode", ["existing", "source", "symlink"])
def test_destination_collision_preserves_bytes(tmp_path: Path, mode: str) -> None:
    source, destination = tmp_path / "old.als", tmp_path / "new.als"
    source.write_bytes(minimal_set())
    if mode == "source":
        destination = source
    elif mode == "symlink":
        destination.symlink_to(source)
    else:
        destination.write_bytes(b"keep this")
    before = destination.read_bytes()
    with pytest.raises(FileExistsError):
        convert_file(source, destination)
    assert destination.read_bytes() == before


@pytest.mark.parametrize("cut", [0, 4, 15, -1])
def test_truncated_input_never_publishes(tmp_path: Path, cut: int) -> None:
    source, destination = tmp_path / "old.als", tmp_path / "new.als"
    source.write_bytes(minimal_set()[:cut])
    with pytest.raises(DecodeError):
        convert_file(source, destination)
    assert not destination.exists()


def test_upgrade_distinguishes_name_member_from_removed_name_class() -> None:
    root = ET.fromstring(
        '<Ableton MinorVersion="8.1_226"><LiveSet><Tracks><MidiTrack><Name>'
        '<UserName Value="Keep me" /></Name></MidiTrack></Tracks></LiveSet></Ableton>'
    )
    upgrade(root, "8.2_260")
    name = root.find(".//Name/UserName")
    assert name is not None and name.get("Value") == "Keep me"


def test_fieldless_records_cannot_outgrow_the_input() -> None:
    root = definition("LiveDocument", [("Items", kind("Array"))])
    container = kind("Array") + struct.pack("<i", -3)
    empty = definition("Empty", [])
    with pytest.raises(DecodeError, match="node limit exceeded"):
        decode(document([root, container, empty], struct.pack("<I", 1_999_000) + kind("Empty")))


def test_missing_field_is_a_decode_error() -> None:
    with pytest.raises(DecodeError, match="no field Tracks"):
        decode(document([definition("LiveDocument", [])], b"")).root.field("Tracks")


_PLAIN_CLIP_FIELDS = """CurrentStart CurrentEnd Loop Annotation ColorIndex LaunchMode LaunchQuantisation
ScrollerTimePreserver TimeSelection Legato Ram Disabled VelocityAmount FollowTime FollowActionA FollowActionB
FollowChanceA FollowChanceB Grid FreezeStart FreezeEnd StepForwardBang StepBackwardBang BankSelectCoarse
BankSelectFine ProgramChange NoteEditorFoldInZoom NoteEditorFoldInScroll NoteEditorFoldOutZoom
NoteEditorFoldOutScroll""".split()


def clip_set(pointee: int = 7, signature: tuple[int, int] = (4, 2)) -> bytes:
    """Synthetic set: one MIDI track and one groove clip with one note and one modulation."""
    clip_fields = [
        ("Time", b"\x17"),
        ("Name", b"\x14"),
        ("TimeSignature", kind("RemoteableTimeSignature")),
        ("GrooveSettings", kind("ClipGrooveSettings")),
        ("Notes", kind("MidiNotes")),
        ("ModulationList", kind("ModulationList")),
        *((name, b"\x11") for name in _PLAIN_CLIP_FIELDS),
    ]
    definitions = [
        definition(
            "LiveDocument",
            [
                ("Tracks", kind("RemoteableList")),
                ("MasterTrack", kind("MasterTrack")),
                ("GroovePool", kind("GroovePool")),
            ],
        ),
        kind("RemoteableList") + struct.pack("<i", -1),
        kind("RemoteableArray") + struct.pack("<i", -3),
        definition("MidiTrack", [("Name", kind("DeviceChainContainerName"))]),
        definition("DeviceChainContainerName", [("EffectiveName", b"\x14"), ("UserName", b"\x14")]),
        definition("MasterTrack", [("MasterChain", kind("MasterTrackDeviceChain"))]),
        definition("MasterTrackDeviceChain", [("MasterDevice", kind("MasterTrackDevice"))]),
        definition("MasterTrackDevice", [("Tempo", kind("TimeableFloat"))]),
        definition(
            "TimeableFloat",
            [("ArrangerAutomation", kind("ListAutomation")), ("ModulationTarget", kind("TimeableModulationTarget"))],
        ),
        definition("TimeableModulationTarget", [("Id", b"\x11")]),
        definition("ListAutomation", [("Events", kind("RemoteableArray"))]),
        definition("FloatEvent", [("Time", b"\x17"), ("Value", b"\x12")]),
        definition("GroovePool", [("Grooves", kind("RemoteableList"))]),
        definition("Groove", [("Name", b"\x14"), ("Clip", kind("RemoteableSlot"))]),
        definition("RemoteableSlot", [("Value", kind("MidiClip", 9))]),
        definition("MidiClip", clip_fields, 9),
        definition("RemoteableTimeSignature", [("Numerator", b"\x11"), ("Denominator", b"\x11"), ("Time", b"\x11")]),
        definition("ClipGrooveSettings", [("GrooveId", b"\x11")]),
        definition("MidiNotes", [("KeyTracks", kind("RemoteableList"))]),
        definition("KeyTrack", [("MidiKey", b"\x11"), ("Notes", kind("RemoteableArray"))]),
        definition(
            "MidiNoteEvent",
            [
                ("Time", b"\x17"),
                ("Duration", b"\x17"),
                ("Velocity", b"\x12"),
                ("OffVelocity", b"\x12"),
                ("IsEnabled", b"\x10"),
            ],
        ),
        definition("ModulationList", [("Modulations", kind("RemoteableList"))]),
        definition("Modulation", [("ModulationTarget", kind("PtrBase"))]),
        definition("PtrBase", [("PointeeId", b"\x11")]),
    ]
    note = kind("MidiNoteEvent") + struct.pack("<ddff?", 0.5, 0.25, 100, 64, True)
    notes = struct.pack("<I", 1) + kind("KeyTrack") + struct.pack("<IiI", 0, 60, 1) + note + kind("")
    modulations = struct.pack("<I", 1) + kind("Modulation") + struct.pack("<Ii", 0, pointee) + kind("")
    clip = (
        struct.pack("<d", 0)
        + string("Lead")
        + struct.pack("<iiii", *signature, 0, 2)
        + notes
        + modulations
        + bytes(4 * len(_PLAIN_CLIP_FIELDS))
    )
    tracks = struct.pack("<I", 6) + kind("MidiTrack") + struct.pack("<I", 5) + string("1 MIDI") + string("") + kind("")
    master = struct.pack("<I", 1) + kind("FloatEvent") + struct.pack("<dfi", -63072000, 120, 7)
    grooves = struct.pack("<I", 3) + kind("Groove") + struct.pack("<I", 2) + string("Swing") + clip + kind("")
    return document(definitions, tracks + master + grooves)


def test_midi_clip_projection_keeps_ids_notes_and_signature(tmp_path: Path) -> None:
    source, destination = tmp_path / "old.als", tmp_path / "new.als"
    source.write_bytes(clip_set())
    convert_file(source, destination)
    root = live_document.read(destination)
    track = root.find("LiveSet/Tracks/MidiTrack")
    assert track is not None and track.get("Id") == "5"
    name = track.find("Name")
    assert name is not None and name.find("EffectiveName") is None
    user = name.find("UserName")
    assert user is not None and user.get("Value") == "1 MIDI"
    groove = root.find("LiveSet/GroovePool/Grooves/Groove")
    assert groove is not None and groove.get("Id") == "2"
    clip = next(groove.iter("MidiClip"))
    pitch = clip.find("Notes/KeyTracks/KeyTrack/MidiKey")
    assert pitch is not None and pitch.get("Value") == "60"
    event = clip.find("Notes/KeyTracks/KeyTrack/Notes/MidiNoteEvent")
    assert event is not None
    assert event.attrib == {
        "Time": "0.5",
        "Duration": "0.25",
        "Velocity": "100.0",
        "OffVelocity": "64.0",
        "IsEnabled": "true",
    }
    signature = clip.find("TimeSignature")
    assert signature is not None
    assert [(e.tag, e.get("Value")) for e in signature] == [("Numerator", "4"), ("Denominator", "4"), ("Time", "0")]
    assert [e.get("Value") for e in clip.iter("PointeeId")] == ["7"]
    assert clip.find("StepForwardBang") is None


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (clip_set(pointee=8), "Unmapped modulation references"),
        (clip_set(signature=(3, 4)), "witnessed legacy 4/4"),
    ],
)
def test_midi_clip_projection_refuses_what_it_cannot_map(tmp_path: Path, data: bytes, message: str) -> None:
    source, destination = tmp_path / "old.als", tmp_path / "new.als"
    source.write_bytes(data)
    with pytest.raises(ValueError, match=message):
        convert_file(source, destination)
    assert not destination.exists()


def test_converted_set_finds_its_project_from_the_destination(tmp_path: Path) -> None:
    project, elsewhere = tmp_path / "Old Project", tmp_path / "elsewhere"
    (project / "Ableton Project Info").mkdir(parents=True)
    elsewhere.mkdir()
    source = project / "old.als"
    source.write_bytes(minimal_set())
    assert convert_file(source, project / "new.als").live_set.project_root == project
    assert convert_file(source, elsewhere / "new.als").live_set.project_root is None
