"""Files a plugin's own patch points at: found through the plugin's map, read and relinked.

The contract every plugin with file paths keeps is written once here, as tables over the patches in
``plugin_patches``; each vendor's own test file keeps what is particular to that plugin. Every patch is
synthesized from the measured layouts. Nothing in this file comes out of a real set.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import shutil
import struct
import wave
from typing import Any

import pytest
from conftest import SKELETONS
from plugin_patches import (
    BATTERY,
    CMI_V,
    ENSEMBLE,
    FACTORY,
    IRIS,
    KEYS,
    KEYS_SAMPLE,
    KICK,
    KIT,
    KONTAKT,
    KONTAKT_16OUT,
    LOOP,
    MASCHINE,
    MASCHINE_FX,
    MPC,
    PROJECT,
    REAKTOR,
    RESOURCE,
    SERATO,
    SERUM_2,
    SERUM_2_SAMPLE,
    SINE,
    SNARE,
    SWAMP,
    TEXTURE,
    TEXTURE_SNARE,
    XFADE,
    battery,
    cmi_v,
    file_paths,
    iris,
    maschine_project,
    maschine_strings,
    mpc,
    reaktor_plain,
    reaktor_url,
    serato,
    serum_2_sample,
    texture_bare,
    texture_wrapped,
    xfade,
)

from abletoolz.live_set import AbletonSet
from abletoolz.live_set.plugins import dump_device
from abletoolz.plugins.container.device import PluginDevice, PluginDevices
from abletoolz.plugins.conversion import as_format, write
from abletoolz.plugins.identity.ids import PluginKind, StoredId, Vst2Id, Vst3Id
from abletoolz.plugins.identity.vst3_parameters import no_parameters
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.plugin_map import FileRef
from abletoolz.plugins.state.serato import NO_SAMPLE
from abletoolz.plugins.state.xfadelooper import PATH_END, PATH_FIELD_SIZE, PATH_OFFSET
from abletoolz.samples.catalog import SampleCatalog
from abletoolz.samples.relink import Collector, Refusal

# -- which plugins say their patch names files -----------------------------------------

# Every plugin map with file paths, and whether abletoolz rewrites them yet.
WITH_PATHS = {
    "Battery 4": True,
    "CMI V": True,
    "Kontakt 5": True,
    "Kontakt 5 16out": True,
    "MPC Essentials": False,
    "Maschine 2": True,
    "Maschine 2 FX": True,
    "Reaktor 6": True,
    "Serato Sample": True,
    "Serum 2": True,
    "Texture": True,
    "iZotope Iris": False,
    "xfadelooper": True,
}


def test_only_plugins_whose_patch_names_files_say_so() -> None:
    found = {plugin_map.name: plugin_map.file_paths.rewrites for plugin_map in CATALOG if plugin_map.file_paths}
    assert found == WITH_PATHS


def test_every_plugin_with_paths_names_its_suffixes_in_lowercase() -> None:
    for plugin_map in CATALOG:
        if plugin_map.file_paths is not None:
            assert plugin_map.file_paths.suffixes, plugin_map.name
            assert all(suffix.startswith(".") and suffix == suffix.lower() for suffix in plugin_map.file_paths.suffixes)


def test_the_plugin_maps_name_kit_instrument_and_audio_suffixes() -> None:
    suffixes = CATALOG.file_suffixes()
    assert {".mxgrp", ".mxsnd", ".nbkt", ".nki", ".ncw", ".wav", ".aif"} <= suffixes


def test_a_plugin_whose_rewrite_is_unmeasured_refuses_one_up_front() -> None:
    for plugin_map in CATALOG:
        if plugin_map.file_paths is not None and not plugin_map.file_paths.rewrites:
            with pytest.raises(StateTransformError):
                plugin_map.file_paths.rewrite(b"", FileRef("C:/x"), pathlib.Path("C:/y"))


# -- what a patch names ------------------------------------------------------------------

MASCHINE_MEDIA = ["C:\\Kits\\Drums.mxgrp", "Group", "C:\\Audio\\hit.wav", "C:\\Kits\\Drums.mxgrp", "/Users/a.aif"]
MASCHINE_REFS = (
    FileRef("C:\\Kits\\Drums.mxgrp", "kit"),
    FileRef("C:\\Audio\\hit.wav", "sample"),
    FileRef("/Users/a.aif", "sample"),
)

# (stored id, patch, every file it names, once each and in order). Every plugin with paths has a row.
READS = [
    pytest.param(
        SERATO,
        serato({"File": "D:/Samples/kick.wav", "Length": 1.5}),
        (FileRef("D:/Samples/kick.wav", "sample", 1.5),),
        id="Serato Sample, with the sample's length",
    ),
    pytest.param(
        XFADE,
        xfade("C:\\loops\\a.wav", residue=b"old\\longer.wav"),
        (FileRef("C:\\loops\\a.wav"),),
        id="xfadelooper, and not the stale residue after it",
    ),
    pytest.param(MASCHINE, maschine_strings(MASCHINE_MEDIA), MASCHINE_REFS, id="Maschine 2, kits and samples"),
    pytest.param(MASCHINE_FX, maschine_strings(MASCHINE_MEDIA), MASCHINE_REFS, id="Maschine 2 FX"),
    pytest.param(
        BATTERY,
        battery([KIT, KICK, KICK]),
        (FileRef(KIT, "kit"), FileRef(KICK, "sample")),
        id="Battery 4, kit and samples",
    ),
    pytest.param(
        KONTAKT,
        battery([KEYS, KEYS_SAMPLE]),
        (FileRef(KEYS, "instrument"), FileRef(KEYS_SAMPLE, "sample")),
        id="Kontakt 5, its library files",
    ),
    pytest.param(
        KONTAKT_16OUT,
        battery([KEYS, KEYS_SAMPLE]),
        (FileRef(KEYS, "instrument"), FileRef(KEYS_SAMPLE, "sample")),
        id="Kontakt 5 16out",
    ),
    pytest.param(TEXTURE, texture_bare(TEXTURE_SNARE), (FileRef(TEXTURE_SNARE),), id="Texture, a user sample"),
    pytest.param(TEXTURE, texture_wrapped(TEXTURE_SNARE), (FileRef(TEXTURE_SNARE),), id="Texture in its VstW bank"),
    pytest.param(REAKTOR, reaktor_url(ENSEMBLE), (FileRef(ENSEMBLE, "ensemble"),), id="Reaktor 6, as a file URL"),
    pytest.param(REAKTOR, reaktor_plain(ENSEMBLE), (FileRef(ENSEMBLE, "ensemble"),), id="Reaktor 6, as a plain path"),
    pytest.param(
        CMI_V, cmi_v(LOOP, FACTORY, RESOURCE, LOOP), (FileRef(LOOP),), id="CMI V, the user's sample and not its own"
    ),
    pytest.param(IRIS, iris(SWAMP, SINE), (FileRef(SWAMP), FileRef(SINE)), id="iZotope Iris, each layer's sample"),
    pytest.param(MPC, mpc(PROJECT), (FileRef(PROJECT, "project"),), id="MPC Essentials, its project"),
    pytest.param(SERUM_2, serum_2_sample(), (FileRef(SERUM_2_SAMPLE),), id="Serum 2, a sample oscillator's file"),
]


def test_every_plugin_with_paths_has_a_patch_in_the_table() -> None:
    """A plugin whose reading nothing exercises is a layout nobody has read."""
    named = set()
    for case in READS:
        stored = case.values[0]
        assert isinstance(stored, (int, Vst3Id))
        plugin_map = CATALOG.find(Vst2Id(stored) if isinstance(stored, int) else stored)
        assert plugin_map is not None
        named.add(plugin_map.name)
    assert named == set(WITH_PATHS)


@pytest.mark.parametrize(("stored", "patch", "refs"), READS)
def test_a_patch_names_each_file_it_points_at_once(
    stored: int | StoredId, patch: bytes, refs: tuple[FileRef, ...]
) -> None:
    assert file_paths(stored).read(patch) == refs


NOTHING = [
    pytest.param(SERATO, serato(None), id="Serato Sample with no sample loaded"),
    pytest.param(XFADE, xfade(""), id="xfadelooper with an empty path"),
    pytest.param(XFADE, xfade("C:\\a.wav", version=b"\x00\x09\x09\x00"), id="xfadelooper of an unmeasured layout"),
    pytest.param(MASCHINE, maschine_strings(["C:\\Program Files\\notes.txt"]), id="Maschine text that is not media"),
    pytest.param(MASCHINE, maschine_strings(["C:\\Kits\\Drums.mxgrp"], magic=b"xxxx"), id="a foreign container"),
    # The framing decides a candidate, not the text: a path whose length prefix is wrong is not a
    # reference, however much it looks like one.
    pytest.param(
        MASCHINE,
        maschine_strings([]) + struct.pack("<I", 3) + "C:\\Kits\\Drums.mxgrp".encode("utf-16-le"),
        id="a Maschine path whose length prefix is wrong",
    ),
    pytest.param(TEXTURE, texture_bare(None), id="Texture playing its own sound"),
    pytest.param(TEXTURE, texture_wrapped(None), id="Texture playing its own sound in its VstW bank"),
    pytest.param(REAKTOR, b"\x014RIN\x11\x00\x00\x00\x00\x00", id="Reaktor with no ensemble"),
    pytest.param(CMI_V, cmi_v(FACTORY), id="CMI V playing a factory sound"),
    pytest.param(MPC, b"\x00" * 16, id="MPC Essentials with no project"),
]


@pytest.mark.parametrize(("unique_id", "patch"), NOTHING)
def test_a_patch_that_points_at_no_file_of_the_user_s_names_nothing(unique_id: int, patch: bytes) -> None:
    assert file_paths(unique_id).read(patch) == ()


# -- relinking one ------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Relink:
    """A patch, the file in it to move, where to, and how the patch names it afterwards."""

    unique_id: int | StoredId
    patch: bytes
    old: str
    new: pathlib.Path
    reads_as: FileRef
    restores: bool = False
    """Rewriting back to the old path gives the patch back byte for byte."""


XFADE_MOVED = pathlib.Path("C:/loops/b.wav")
CMI_MOVED = pathlib.Path("E:\\Moved\\bass loop.wav")
TEXTURE_MOVED = pathlib.Path("D:\\A Much Longer Folder\\Moved Snare.wav")
REAKTOR_MOVED = pathlib.Path("E:\\My Ensembles\\Monark & friends (moved).ens")
SERUM_2_MOVED = pathlib.Path("/Volumes/Samples/Drums/Drum_Loop_170 (bounced).wav")
REAKTOR_READS_AS = FileRef("E:/My Ensembles/Monark & friends (moved).ens", "ensemble")

RELINKS = {
    "Serato Sample": Relink(
        SERATO,
        serato({"File": "D:/old/kick.wav", "Length": 1.5}, prefix=b"\x01", suffix=b"\x00"),
        "D:/old/kick.wav",
        pathlib.Path("/Volumes/Samples/kick.wav"),
        FileRef("/Volumes/Samples/kick.wav", "sample", 1.5),
    ),
    # The plugin is given the path as this platform spells it.
    "xfadelooper": Relink(
        XFADE, xfade("C:\\loops\\a.wav"), "C:\\loops\\a.wav", XFADE_MOVED, FileRef(str(XFADE_MOVED)), True
    ),
    "Battery 4, to a longer path": Relink(
        BATTERY,
        battery([KIT, KICK]),
        KICK,
        pathlib.Path("D:/Moved Library With A Longer Name/Kick 1.wav"),
        FileRef("D:\\Moved Library With A Longer Name\\Kick 1.wav", "sample"),
    ),
    "Kontakt 5": Relink(
        KONTAKT,
        battery([KEYS, KEYS_SAMPLE]),
        KEYS_SAMPLE,
        pathlib.Path("E:/Keys/samples/keys_01.ncw"),
        FileRef("E:\\Keys\\samples\\keys_01.ncw", "sample"),
    ),
    "Maschine 2": Relink(
        MASCHINE,
        maschine_project(SNARE, SNARE),
        SNARE,
        pathlib.Path("E:\\S\\Snare 1.wav"),
        FileRef("E:\\S\\Snare 1.wav", "sample"),
    ),
    "Texture": Relink(
        TEXTURE, texture_bare(TEXTURE_SNARE), TEXTURE_SNARE, TEXTURE_MOVED, FileRef(str(TEXTURE_MOVED)), True
    ),
    "Texture in its VstW bank": Relink(
        TEXTURE, texture_wrapped(TEXTURE_SNARE), TEXTURE_SNARE, TEXTURE_MOVED, FileRef(str(TEXTURE_MOVED)), True
    ),
    "Reaktor 6, as a file URL": Relink(REAKTOR, reaktor_url(ENSEMBLE), ENSEMBLE, REAKTOR_MOVED, REAKTOR_READS_AS, True),
    "Reaktor 6, as a plain path": Relink(
        REAKTOR, reaktor_plain(ENSEMBLE), ENSEMBLE, REAKTOR_MOVED, REAKTOR_READS_AS, True
    ),
    "CMI V": Relink(CMI_V, cmi_v(LOOP, FACTORY, LOOP), LOOP, CMI_MOVED, FileRef(str(CMI_MOVED)), True),
    # The body is compressed again; the fixture was compressed by the same rewrite, so it restores.
    "Serum 2": Relink(SERUM_2, serum_2_sample(), SERUM_2_SAMPLE, SERUM_2_MOVED, FileRef(str(SERUM_2_MOVED)), True),
}


def moved(case: Relink) -> bytes:
    file_paths_ = file_paths(case.unique_id)
    (ref,) = [ref for ref in file_paths_.read(case.patch) if ref.path == case.old]
    return file_paths_.rewrite(case.patch, ref, case.new)


@pytest.mark.parametrize("case", RELINKS.values(), ids=RELINKS.keys())
def test_a_rewrite_points_the_patch_at_the_new_file_and_leaves_the_others(case: Relink) -> None:
    before = file_paths(case.unique_id).read(case.patch)
    after = tuple(case.reads_as if ref.path == case.old else ref for ref in before)
    assert file_paths(case.unique_id).read(moved(case)) == after


RESTORES = {name: case for name, case in RELINKS.items() if case.restores}


@pytest.mark.parametrize("case", RESTORES.values(), ids=RESTORES.keys())
def test_rewriting_the_old_path_back_restores_the_patch_byte_for_byte(case: Relink) -> None:
    """Every count and length the rewrite recomputed comes back to what the plugin wrote."""
    assert file_paths(case.unique_id).rewrite(moved(case), case.reads_as, pathlib.Path(case.old)) == case.patch


# (relink, a path too long for the field, how the refusal says so)
FIXED_WIDTH = [
    pytest.param("xfadelooper", "/" + "x" * PATH_FIELD_SIZE, "too long", id="xfadelooper's path field"),
    pytest.param("CMI V", "C:/" + "x" * 300 + ".wav", "too long", id="CMI V's voice path field"),
    pytest.param("Maschine 2", "D:\\" + "x" * 600 + "\\Snare 1.wav", "does not fit", id="Maschine's record"),
]


@pytest.mark.parametrize(("name", "too_long", "match"), FIXED_WIDTH)
def test_a_fixed_width_path_keeps_the_patch_s_length_and_refuses_a_path_too_long_for_it(
    name: str, too_long: str, match: str
) -> None:
    """Nothing around a fixed field moves; a longer path would be silently truncated into one pointing nowhere."""
    case = RELINKS[name]
    assert len(moved(case)) == len(case.patch)
    with pytest.raises(StateTransformError, match=match):
        moved(dataclasses.replace(case, new=pathlib.Path(too_long)))


# (stored id, patch, a file it does not name)
NOT_NAMED = [
    pytest.param(SERATO, serato({"File": "a.wav"}), "b.wav", id="Serato Sample"),
    pytest.param(BATTERY, battery([KICK]), "C:\\Elsewhere\\Snare.wav", id="Battery 4"),
    pytest.param(MASCHINE, maschine_project(SNARE), "C:\\Elsewhere\\Snare.wav", id="Maschine 2"),
    pytest.param(TEXTURE, texture_bare(TEXTURE_SNARE), "C:\\Elsewhere\\kick.wav", id="Texture"),
    pytest.param(REAKTOR, reaktor_url(ENSEMBLE), "D:/Elsewhere/Form.ens", id="Reaktor 6"),
    pytest.param(CMI_V, cmi_v(LOOP), "C:\\Elsewhere\\kick.wav", id="CMI V"),
    pytest.param(SERUM_2, serum_2_sample(), "C:\\Elsewhere\\kick.wav", id="Serum 2"),
]


@pytest.mark.parametrize(("stored", "patch", "other"), NOT_NAMED)
def test_a_rewrite_of_a_file_the_patch_does_not_name_is_refused(
    stored: int | StoredId, patch: bytes, other: str
) -> None:
    with pytest.raises(StateTransformError, match="does not point at"):
        file_paths(stored).rewrite(patch, FileRef(other), pathlib.Path("C:/x.wav"))


# -- Serato Sample and xfadelooper -------------------------------------------------------


def test_a_serato_rewrite_changes_only_the_path_and_keeps_bytes_around_the_json() -> None:
    case = RELINKS["Serato Sample"]
    written = moved(case)
    assert written[:1] == b"\x01" and written[-1:] == b"\x00"
    assert json.loads(written[1:-1])["project"]["Version"] == 35


def test_an_xfadelooper_rewrite_keeps_every_parameter_and_clears_the_stale_residue() -> None:
    patch = xfade("C:\\loops\\a.wav", residue=b"residue")
    (ref,) = file_paths(XFADE).read(patch)
    written = file_paths(XFADE).rewrite(patch, ref, XFADE_MOVED)
    assert written[:PATH_OFFSET] == patch[:PATH_OFFSET] and written[PATH_END:] == patch[PATH_END:]
    assert b"residue" not in written


def test_the_longest_xfadelooper_path_leaves_one_byte_for_the_terminator() -> None:
    patch = xfade("C:\\a.wav")
    (ref,) = file_paths(XFADE).read(patch)
    longest = pathlib.Path("/" + "x" * (PATH_FIELD_SIZE - 2))
    assert file_paths(XFADE).read(file_paths(XFADE).rewrite(patch, ref, longest)) == (FileRef(str(longest)),)


# -- in a set: the sample flags --------------------------------------------------


def project_set(tmp_path: pathlib.Path) -> AbletonSet:
    """The 11.3.42 fixture inside a project folder."""
    project = tmp_path / "Song Project"
    (project / "Ableton Project Info").mkdir(parents=True)
    shutil.copy(SKELETONS / "11.3.42.als", project / "Song.als")
    live_set = AbletonSet(project / "Song.als")
    assert live_set.parse()
    assert live_set.project_root == project
    return live_set


def plugin_device(live_set: AbletonSet, name: str, unique_id: int, state: bytes) -> PluginDevice:
    """The set's Serum device, made into the plugin ``name`` holding ``state``."""
    device = next(item for item in PluginDevices(live_set.root) if item.identity.name == "Serum_x64")
    device.set_name(name)
    device.set_unique_id(unique_id)
    device.set_state(state)
    return device


def serato_device(live_set: AbletonSet, song: dict[str, Any] | None) -> PluginDevice:
    return plugin_device(live_set, "Serato Sample", SERATO, serato(song))


def database(*files: pathlib.Path) -> SampleCatalog:
    return SampleCatalog.from_records({str(path): {"name": path.name, "size": path.stat().st_size} for path in files})


def test_check_samples_names_a_missing_plugin_sample_and_an_empty_patch(tmp_path: pathlib.Path) -> None:
    live_set = project_set(tmp_path)
    serato_device(live_set, {"File": "D:/Gone/kick.wav", "Length": 1.0})
    (missing,) = live_set.plugin_files.check()
    assert (missing.device, missing.ref, missing.rewrites) == (
        "Serato Sample",
        FileRef("D:/Gone/kick.wav", "sample", 1.0),
        True,
    )
    assert missing.name == "kick.wav"

    other = project_set(tmp_path / "other")
    serato_device(other, None)
    # An empty patch is worth a note, and is not a missing sample.
    assert other.plugin_files.check() == []
    (empty,) = other.plugin_files.notes()
    assert empty.ref is None and empty.note == NO_SAMPLE


def test_a_plugin_sample_that_is_on_disk_is_not_reported(tmp_path: pathlib.Path) -> None:
    sample = tmp_path / "kick.wav"
    sample.write_bytes(b"RIFF")
    live_set = project_set(tmp_path)
    serato_device(live_set, {"File": sample.as_posix()})
    assert live_set.plugin_files.check() == []


def test_fix_points_the_plugin_at_the_file_the_database_found(tmp_path: pathlib.Path) -> None:
    found = tmp_path / "Library" / "kick.wav"
    found.parent.mkdir()
    found.write_bytes(b"RIFF")
    live_set = project_set(tmp_path)
    device = serato_device(live_set, {"File": "D:/Gone/kick.wav", "Length": 1.0})

    (fix,) = live_set.plugin_files.fix(database(found))

    assert (fix.old, fix.new) == ("D:/Gone/kick.wav", str(found))
    assert file_paths(SERATO).read(device.saved_state.state) == (FileRef(found.as_posix(), "sample", 1.0),)
    assert live_set.plugin_files.check() == []


def test_fix_with_collect_copies_the_file_into_the_project_and_points_at_the_copy(tmp_path: pathlib.Path) -> None:
    found = tmp_path / "Library" / "kick.wav"
    found.parent.mkdir()
    found.write_bytes(b"RIFF")
    live_set = project_set(tmp_path)
    device = serato_device(live_set, {"File": "D:/Gone/kick.wav"})

    collector = Collector(tmp_path / "Song Project")
    live_set.plugin_files.fix(database(found), collector)

    copy = tmp_path / "Song Project" / "Samples" / "Imported" / "kick.wav"
    assert file_paths(SERATO).read(device.saved_state.state) == (FileRef(copy.as_posix()),)
    # Nothing is copied until the set is saved.
    assert not copy.exists()
    collector.run()
    assert copy.read_bytes() == b"RIFF"


def test_fix_reaches_a_serato_sample_vst3_device_too(tmp_path: pathlib.Path) -> None:
    found = tmp_path / "kick.wav"
    found.write_bytes(b"RIFF")
    live_set = project_set(tmp_path)
    device = serato_device(live_set, {"File": "D:/Gone/kick.wav"})
    serato_map = CATALOG.find(Vst2Id(SERATO))
    assert serato_map is not None
    write(device, as_format(device, serato_map, PluginKind.VST3, parameters=no_parameters))
    assert device.kind is PluginKind.VST3

    (fix,) = live_set.plugin_files.fix(database(found))

    assert fix.new == str(found)
    assert file_paths(SERATO).read(device.saved_state.state) == (FileRef(found.as_posix()),)


def test_fix_leaves_a_patch_whose_rewrite_is_refused_as_it_was(tmp_path: pathlib.Path) -> None:
    found = tmp_path / "Drums.mxgrp"
    found.write_bytes(b"kit")
    live_set = project_set(tmp_path)
    device = plugin_device(live_set, "Maschine 2", MASCHINE, maschine_strings(["C:\\Kits\\Drums.mxgrp"]))
    before = device.saved_state.state
    refused: list[Refusal] = []

    assert live_set.plugin_files.fix(database(found), refused=refused) == []
    assert device.saved_state.state == before
    (refusal,) = refused
    assert "Drums.mxgrp" in refusal.what


def test_fix_refuses_a_name_several_files_share_when_the_patch_cannot_tell_them_apart(
    tmp_path: pathlib.Path,
) -> None:
    first, second = tmp_path / "A" / "kick.wav", tmp_path / "B" / "kick.wav"
    for path, body in [(first, b"RIFF1"), (second, b"RIFF22")]:
        path.parent.mkdir()
        path.write_bytes(body)
    live_set = project_set(tmp_path)
    device = serato_device(live_set, {"File": "D:/Gone/kick.wav"})
    before = device.saved_state.state
    refused: list[Refusal] = []

    assert live_set.plugin_files.fix(database(first, second), refused=refused) == []
    assert device.saved_state.state == before
    (refusal,) = refused
    assert str(first) in refusal.why and str(second) in refusal.why


def test_fix_takes_the_one_file_whose_length_the_patch_recorded(tmp_path: pathlib.Path) -> None:
    short, long = tmp_path / "A" / "kick.wav", tmp_path / "B" / "kick.wav"
    for path, frames in [(short, 4410), (long, 44100)]:
        path.parent.mkdir()
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(44100)
            out.writeframes(b"\0\0" * frames)
    live_set = project_set(tmp_path)
    serato_device(live_set, {"File": "D:/Gone/kick.wav", "Length": 1.0})

    (fix,) = live_set.plugin_files.fix(database(short, long))

    assert fix.new == str(long)


def test_a_patch_that_cannot_be_read_is_a_note_and_never_a_crash(tmp_path: pathlib.Path) -> None:
    live_set = project_set(tmp_path)
    plugin_device(live_set, "xfadelooper", XFADE, b"no layout anybody measured")

    assert live_set.plugin_files.check() == []
    (note,) = live_set.plugin_files.notes()
    assert note.ref is None and note.note is not None and "could not be read" in note.note
    assert live_set.plugin_files.fix(database()) == []


def test_a_dump_names_the_plugin_map_and_the_files_its_patch_points_at(tmp_path: pathlib.Path) -> None:
    live_set = project_set(tmp_path)
    device = serato_device(live_set, {"File": "D:/Gone/kick.wav"})
    dump = dump_device(device, "Drums")
    assert "Serato Sample (vst) on Drums" in dump
    assert "Plugin map: Serato Sample [serato.sample]" in dump
    assert "bytes, JSON" in dump
    assert "File: sample D:/Gone/kick.wav" in dump


def maschine_group(tmp_path: pathlib.Path) -> tuple[AbletonSet, pathlib.Path, pathlib.Path]:
    """A set whose Maschine patch points at a missing group file and a missing sample, and where both went."""
    hit, group = tmp_path / "Library" / "hit.wav", tmp_path / "Library" / "Drums.mxgrp"
    hit.parent.mkdir()
    hit.write_bytes(b"RIFF")
    group.write_bytes(b"group")
    live_set = project_set(tmp_path)
    plugin_device(live_set, "Maschine 2", MASCHINE, maschine_project("C:\\Gone\\Drums.mxgrp", "C:\\Gone\\hit.wav"))
    return live_set, hit, group


def test_each_file_a_patch_points_at_is_looked_up_in_the_database_for_its_type(tmp_path: pathlib.Path) -> None:
    """Audio is found in the sample database and a file only the plugin loads in the plugin files database."""
    live_set, hit, group = maschine_group(tmp_path)
    assert live_set.plugin_files.fix(database(group), plugin_files_db=database(hit)) == []
    fixes = live_set.plugin_files.fix(database(hit), plugin_files_db=database(group))
    assert sorted(fix.new for fix in fixes) == sorted([str(group), str(hit)])


def test_collecting_copies_a_plugins_audio_but_never_its_plugin_files(tmp_path: pathlib.Path) -> None:
    live_set, hit, group = maschine_group(tmp_path)
    collector = Collector(tmp_path / "Song Project")

    live_set.plugin_files.fix(database(hit), collector, plugin_files_db=database(group))

    assert list(collector.copies.values()) == [hit]
