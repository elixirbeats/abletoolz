"""NI's item container, its patch frames and segment paths, and what is particular to Battery and Maschine.

What every plugin with file paths does -- name its files, relink one, refuse a file it does not name --
is in ``test_file_paths``.
"""

from __future__ import annotations

import pathlib
import struct

import pytest
from plugin_patches import (
    BATTERY,
    KICK,
    KIT,
    MASCHINE,
    RECORD_LENGTH,
    SNARE,
    battery,
    file_paths,
    frame,
    item,
    maschine_project,
    maschine_strings,
    record,
    utf16_string,
)

from abletoolz.plugins.state import ni_container
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.plugin_map import FileRef

# -- the container -------------------------------------------------------------


@pytest.mark.parametrize("compressed", [True, False], ids=["compressed", "stored"])
def test_a_state_serializes_back_byte_for_byte(compressed: bool) -> None:
    state = battery([KIT, KICK], compressed=compressed)
    tree = ni_container.parse(state)
    assert len(list(tree.items())) == 3
    assert ni_container.serialize(tree) == state


def test_a_compressed_body_that_does_not_decode_is_opaque_and_kept_as_it_is() -> None:
    garbage = bytes(range(200, 256)) * 3
    data = struct.pack("<IBII", 1, 1, 4000, len(garbage)) + garbage
    state = item(frame(b"DSIN", 0x74, b"", frame(b"DSIN", ni_container.SUBTREE_TYPE, data)))
    tree = ni_container.parse(state)
    assert tree.opaque() == 1
    assert ni_container.serialize(tree) == state
    assert file_paths(BATTERY).read(state) == ()


def test_a_state_whose_sizes_do_not_add_up_is_refused() -> None:
    state = battery([KICK])
    with pytest.raises(StateTransformError):
        ni_container.parse(state[:-1])
    with pytest.raises(StateTransformError):
        ni_container.parse(state + b"\x00")
    assert not ni_container.is_container(b"not a container" * 4)


def test_kontakt_frames_carry_a_domain_of_their_own() -> None:
    state = item(frame(b"4KIN", 3, b"kontakt", frame(b"DSIN", 0x65, b"base")))
    tree = ni_container.parse(state)
    assert [(f.domain, f.type) for f in tree.frame.chain()] == [(b"4KIN", 3), (b"DSIN", 0x65), (b"DSIN", 1)]
    assert ni_container.serialize(tree) == state


def test_a_frame_chain_deeper_than_any_nesting_measured_is_refused() -> None:
    """A damaged chain is one device's refusal, not a RecursionError that fails the whole set."""
    nested = frame(b"DSIN", 2, b"")
    for _ in range(1200):
        nested = frame(b"DSIN", 2, b"", nested)
    with pytest.raises(StateTransformError, match="derives through"):
        ni_container.parse(item(nested))


def frame_data(state: bytes, kind: int = ni_container.PATCH_TYPE) -> bytes:
    """The data of the first frame of type ``kind`` anywhere in a state."""
    return next(f.data for i in ni_container.parse(state).items() for f in i.frame.chain() if f.type == kind)


# -- segment paths -------------------------------------------------------------


def test_a_segment_path_reads_back_as_windows_spells_it() -> None:
    encoded = ni_container.encode_segment_path(KICK)
    assert struct.unpack_from("<I", encoded)[0] == 9
    (found,) = ni_container.segment_paths(b"junk" + encoded + b"more")
    assert found == ni_container.SegmentPath(4, 4 + len(encoded), KICK)


def test_a_folder_is_not_a_file() -> None:
    folder = ni_container.encode_segment_path("C:\\Library\\Kits\\x.nbkt")
    folder = folder[:-17]  # drop the file segment: what is left is a folder list with a stale count
    assert list(ni_container.segment_paths(folder)) == []


@pytest.mark.parametrize("path", ["/Users/me/kick.wav", "kick.wav", "\\\\server\\share\\kick.wav", "C:"])
def test_only_a_windows_drive_path_has_a_measured_spelling(path: str) -> None:
    with pytest.raises(StateTransformError):
        ni_container.encode_segment_path(path)


# -- Battery ---------------------------------------------------------------------


def test_a_battery_rewrite_lands_in_the_compressed_patch_with_every_length_recomputed() -> None:
    """The patch frame's records stay whole around a path of another length, and so does its tail."""
    state = battery([KIT, KICK])
    moved = pathlib.Path("D:/Moved Library With A Longer Name/Kick 1.wav")
    written = file_paths(BATTERY).rewrite(state, FileRef(KICK), moved)

    assert ni_container.serialize(ni_container.parse(written)) == written
    before, after = frame_data(state), frame_data(written)
    records = ni_container.patch_records(after)
    assert records is not None and [record_id for record_id, _ in records] == [3, 0x4B]
    assert records[0][1] == b"program"
    old = ni_container.encode_segment_path(KICK)
    new = ni_container.encode_segment_path(str(pathlib.PureWindowsPath(moved)))
    assert len(after) == len(before) + len(new) - len(old)
    assert after[-8:] == before[-8:]


def test_a_path_outside_a_patch_frame_is_refused_rather_than_spliced() -> None:
    state = item(frame(b"DSIN", 0x99, ni_container.encode_segment_path(KICK)))
    assert file_paths(BATTERY).read(state) == (FileRef(KICK, "sample"),)
    with pytest.raises(StateTransformError, match="unmeasured"):
        file_paths(BATTERY).rewrite(state, FileRef(KICK), pathlib.Path("C:/Elsewhere/Kick 1.wav"))


# -- Maschine ---------------------------------------------------------------------


def test_maschine_refuses_to_rewrite_what_is_not_an_item_container() -> None:
    patch = maschine_strings(["C:\\Kits\\Drums.mxgrp"])
    (ref,) = file_paths(MASCHINE).read(patch)
    with pytest.raises(StateTransformError):
        file_paths(MASCHINE).rewrite(patch, ref, pathlib.Path("/Kits/Drums.mxgrp"))


def test_a_maschine_rewrite_to_a_longer_path_keeps_every_record_its_size_and_counts_what_it_holds() -> None:
    moved = "D:\\Moved\\A Much Longer Folder Name\\Snare 1.wav"
    state = maschine_project(SNARE, SNARE)
    written = file_paths(MASCHINE).rewrite(state, FileRef(SNARE), pathlib.Path(moved))

    assert len(written) == len(state)
    assert file_paths(MASCHINE).read(written) == (FileRef(moved, "sample"),)
    data = frame_data(written)
    first = data.index(b"\x02" + struct.pack("<H", RECORD_LENGTH))
    used = struct.unpack_from("<Q", data, first + 7)[0]
    assert used == len(utf16_string("Sample") + utf16_string(moved) + utf16_string("Drums"))


KICK_ROOT = "D:/Loop Loft/"
KICK_RELATIVE = "Joey Waronker Drums Samples/Kick_01_JoeyWaronker.wav"
KICK_UTF16 = "D:\\Loop Loft\\Joey Waronker Drums Samples\\Kick_01_JoeyWaronker.wav"


def portable(text: str) -> bytes:
    raw = text.encode("utf-8")
    return bytes([1, len(raw)]) + raw


def sampler(relative: str, root: str, gap: bytes = b"") -> bytes:
    """A sampler's reference as Maschine loads it: relative path, maybe a few zero bytes, then the root."""
    return b"\x01\x01\x00" + portable(relative) + gap + portable(root) + b"\x00\x01\x01\x00"


@pytest.mark.parametrize("gap", [b"", b"\x00\x00"], ids=["adjacent", "two zero bytes apart"])
def test_maschine_names_a_sample_by_the_root_and_relative_path_it_loads(gap: bytes) -> None:
    state = maschine_project(KICK_UTF16, samplers=sampler(KICK_RELATIVE, KICK_ROOT, gap))
    assert file_paths(MASCHINE).read(state) == (FileRef(KICK_ROOT + KICK_RELATIVE, "sample"),)


@pytest.mark.parametrize("gap", [b"", b"\x00\x00"], ids=["adjacent", "two zero bytes apart"])
def test_a_maschine_rewrite_moves_what_it_loads_and_what_it_remembers(gap: bytes) -> None:
    state = maschine_project(KICK_UTF16, samplers=sampler(KICK_RELATIVE, KICK_ROOT, gap))
    moved = pathlib.Path("C:/Ableton Projects/Samples/Imported/Kick_01_JoeyWaronker.wav")
    written = file_paths(MASCHINE).rewrite(state, FileRef(KICK_ROOT + KICK_RELATIVE), moved)

    expected = "C:/Ableton Projects/Samples/Imported/Kick_01_JoeyWaronker.wav"
    assert file_paths(MASCHINE).read(written) == (FileRef(expected, "sample"),)
    assert portable("Kick_01_JoeyWaronker.wav") + gap + portable("C:/Ableton Projects/Samples/Imported/") in written
    assert utf16_string(str(pathlib.PureWindowsPath(moved))) in written
    data = frame_data(written)
    assert struct.unpack_from("<Q", data, 12)[0] == len(data) - 28
    assert struct.unpack_from("<I", data, 20)[0] == len(data) - 32


def test_a_maschine_kit_relinked_to_a_mac_path_is_stored_with_slashes_and_reads_back() -> None:
    """Backslashes on a Mac path left a kit reference nothing finds, which read back as no reference at all."""
    kit = "C:\\Kits\\Drums.mxgrp"
    state = maschine_project(kit)
    written = file_paths(MASCHINE).rewrite(state, FileRef(kit), pathlib.Path("/Users/me/Kits/Drums.mxgrp"))
    assert file_paths(MASCHINE).read(written) == (FileRef("/Users/me/Kits/Drums.mxgrp", "kit"),)


def test_a_maschine_frame_whose_lengths_are_not_the_measured_layout_is_refused() -> None:
    """Overwriting two numbers that mean something else there would corrupt the project."""
    archive = b"\x01\x16serialization::archive" + b"\x00" * 9
    archive += record(utf16_string("Sample") + utf16_string(SNARE) + utf16_string("Drums")) + b"\x00" * 16
    stream = struct.pack("<I", len(archive) + 1) + archive
    data = struct.pack("<IIIQ", 1, 1, 1, len(stream)) + stream + bytes(4) + b"\x0d\x62\x65\x85"
    state = item(frame(b"DSIN", ni_container.PATCH_TYPE, data))
    with pytest.raises(StateTransformError, match="measured layout"):
        file_paths(MASCHINE).rewrite(state, FileRef(SNARE), pathlib.Path("D:/Elsewhere/Snare 1.wav"))


def test_a_maschine_path_outside_a_record_is_refused() -> None:
    archive = b"\x01\x16serialization::archive" + b"\x00" * 9 + utf16_string(SNARE) + b"\x00" * 16
    stream = struct.pack("<I", len(archive)) + archive
    data = struct.pack("<IIIQ", 1, 1, 1, len(stream)) + stream + bytes(8)
    state = item(frame(b"DSIN", ni_container.PATCH_TYPE, data))
    with pytest.raises(StateTransformError, match="No record"):
        file_paths(MASCHINE).rewrite(state, FileRef(SNARE), pathlib.Path("D:/Elsewhere/Snare 1.wav"))
