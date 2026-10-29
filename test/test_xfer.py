"""Xfer's plugins: Serum 2's map, the container both halves of its VST3 preset use,
the files its patch plays, and Serum's Audio Unit.

Two fixtures are Serum 2 2.1.4's own init patch, read headless off the installed
VST3; the third is a patch from a set with its one sample path made generic (see
``state_fixtures/serum2/manifest.json``).
"""

from __future__ import annotations

import hashlib
import pathlib
import plistlib
import struct
import zlib

import pytest
import zstandard
from plugin_patches import SERUM_2_SAMPLE, serum_2_sample

from abletoolz.formats.fxb import FxpChunkProgram
from abletoolz.plugins.identity.ids import AuId, PluginKind, Vst3Id
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.framings import Family, detect
from abletoolz.plugins.state.measured import StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import FileRef, FormatState, PluginMap
from abletoolz.plugins.state.xfer import SERUM_2_AU, SERUM_2_VST3, XFER_JSON_MAGIC, XferJson

FIXTURES = pathlib.Path(__file__).parent / "state_fixtures" / "serum2"
VST3 = PluginKind.VST3


def fixture(suffix: str) -> bytes:
    return (FIXTURES / f"serum2-init.vst3.{suffix}").read_bytes()


def serum_2() -> PluginMap:
    found = CATALOG.by_map_id("xfer.serum-2")
    assert found is not None
    return found


def container(header: bytes, body: bytes, *, codec: int = 2) -> bytes:
    """An XferJson buffer around ``header`` and ``body``, laid out as Serum 2 writes it."""
    return XFER_JSON_MAGIC + struct.pack("<Q", len(header)) + header + struct.pack("<II", 64, codec) + body


ZSTD_BODY = b"\x28\xb5\x2f\xfd" + b"frame"


def hashed_header(body: bytes, component: str = "processor") -> bytes:
    digest = hashlib.md5(body, usedforsecurity=False).hexdigest()
    return f'{{"component":"{component}","hash":"{digest}","productVersion":"2.1.4"}}'.encode()


# -- finding it ----------------------------------------------------------------


def test_a_serum_2_device_finds_its_own_map_not_serums() -> None:
    """The class id every Serum 2 device in the owner's sets stores, and its moduleinfo.json declares."""
    assert SERUM_2_VST3 == Vst3Id((1448297816, 1718833267, 1701999981, 540147712))
    assert CATALOG.find(SERUM_2_VST3) is serum_2()
    serum = CATALOG.find(Vst3Id((1448301656, 1718835315, 1701999981, 0)))
    assert serum is not None and serum is not serum_2()


def test_serum_2_is_a_vst3_and_an_au_measured_in_the_host_rig() -> None:
    """No VST2 ships, and hosted evidence is not a listen."""
    plugin = serum_2()
    assert set(plugin.formats) == {VST3, PluginKind.AU}
    measured = plugin.plugin_format(VST3).measured
    assert measured.rung is StateRung.VERBATIM
    assert measured.evidence == (StateEvidence.HOSTED,)
    assert not measured.predictable


# -- the container ---------------------------------------------------------------


@pytest.mark.parametrize(("suffix", "component"), [("state", "processor"), ("controller", "controller")])
def test_both_halves_of_the_init_patch_read_and_write_back_unchanged(suffix: str, component: str) -> None:
    raw = fixture(suffix)
    state = XferJson.parse(raw)
    assert state.component == component
    assert state.product_version == "2.1.4"
    assert state.document["product"] == "Serum2"
    assert state.encode() == raw


def test_the_header_hash_is_the_md5_of_the_compressed_body() -> None:
    state = XferJson.parse(fixture("state"))
    assert hashlib.md5(state.body, usedforsecurity=False).hexdigest() == state.document["hash"]


def test_the_controller_carries_the_preset_name() -> None:
    """Which is what an empty controller half loses: Serum 2 answers " - Init - " without one."""
    assert XferJson.parse(fixture("controller")).document["presetName"] == " - Init - "


def test_a_serum_2_state_is_json_to_the_family_detector() -> None:
    assert detect(fixture("state")) is Family.JSON


def test_a_synthesized_container_reads() -> None:
    raw = container(hashed_header(ZSTD_BODY), ZSTD_BODY)
    state = XferJson.parse(raw)
    assert (state.size, state.codec, state.body) == (64, 2, ZSTD_BODY)
    assert state.encode() == raw


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b'XferJson{"osc": 1}', "opens with XferJson and a NUL"),
        (XFER_JSON_MAGIC + b"\x01", "ends before its header length"),
        (XFER_JSON_MAGIC + struct.pack("<Q", 1000) + b"{}", "runs past its end"),
        (container(b"not json", ZSTD_BODY), "is not JSON"),
        (container(b"[1]", ZSTD_BODY), "not a JSON object"),
        (container(hashed_header(ZSTD_BODY), ZSTD_BODY, codec=1), "codec 1"),
        (container(hashed_header(b"plain"), b"plain"), "codec 2"),
        (container(hashed_header(ZSTD_BODY), ZSTD_BODY + b"!"), "does not match the hash"),
    ],
    ids=[
        "no NUL",
        "truncated",
        "header too long",
        "header not JSON",
        "header not an object",
        "codec",
        "no zstd",
        "hash",
    ],
)
def test_a_buffer_its_lengths_or_hash_do_not_account_for_is_refused(raw: bytes, message: str) -> None:
    with pytest.raises(StateTransformError, match=message):
        XferJson.parse(raw)


def test_a_body_that_inflates_past_its_declared_size_is_refused_one_byte_past_it() -> None:
    body = zstandard.ZstdCompressor().compress(bytes(1000))
    with pytest.raises(StateTransformError, match="decodes to 11 bytes, not 10"):
        XferJson(hashed_header(body), 10, 2, body).decoded()


def test_a_body_declared_larger_than_any_patch_is_refused_before_it_is_inflated() -> None:
    with pytest.raises(StateTransformError, match="more than any patch holds"):
        XferJson(hashed_header(ZSTD_BODY), 2**31, 2, ZSTD_BODY).decoded()


# -- the map ---------------------------------------------------------------------


def test_the_processor_state_is_the_patch_and_the_controller_half_is_written_empty() -> None:
    """Measured: an empty controller state leaves the sound and every parameter where they were."""
    patch = serum_2().lift(FormatState(fixture("state"), fixture("controller")), VST3)
    assert patch == fixture("state")
    assert serum_2().lower(patch, VST3) == FormatState(patch, b"")


def test_a_controller_state_is_not_a_patch() -> None:
    with pytest.raises(StateTransformError, match="matches no recorded layout"):
        serum_2().lift(FormatState(fixture("controller")), VST3)


def test_a_serum_1_patch_is_not_a_serum_2_patch() -> None:
    """A Serum 1 chunk is a zlib stream; Serum 2 left it unread, so the map refuses it too."""
    with pytest.raises(StateTransformError, match="matches no recorded layout"):
        serum_2().lift(FormatState(b"\x78\x01" + bytes(32)), VST3)


# -- the files a Serum 2 patch plays ------------------------------------------------
# Reading, relinking and refusing a file the patch does not name are the shared
# contract in test_file_paths; what is particular to Serum 2 is here.


def test_the_init_patch_plays_no_file_by_path() -> None:
    """Its wavetable and noise are library files, relative to Serum 2's own folder, which move with it."""
    paths = serum_2().file_paths
    assert paths is not None
    assert paths.read(fixture("state")) == ()


def test_the_spectral_oscillator_s_images_are_indexed_too() -> None:
    paths = serum_2().file_paths
    assert paths is not None and ".png" in paths.suffixes


def test_a_relink_changes_only_the_path_and_leaves_a_patch_the_map_takes() -> None:
    paths = serum_2().file_paths
    assert paths is not None
    relinked = paths.rewrite(serum_2_sample(), FileRef(SERUM_2_SAMPLE), pathlib.Path("C:/x.wav"))
    assert serum_2().lift(FormatState(relinked), VST3) == relinked

    before, after = XferJson.parse(serum_2_sample()).decoded(), XferJson.parse(relinked).decoded()
    old, new = SERUM_2_SAMPLE.encode(), str(pathlib.Path("C:/x.wav")).encode()
    # The old path takes a two byte text head (24 bytes or more), the new one a one byte head.
    assert after == before.replace(bytes([0x78, len(old)]) + old, bytes([0x60 | len(new)]) + new)


# -- Serum's Audio Unit ----------------------------------------------------------

SERUM_AU = AuId.of("aumu", "XfsX", "XFER")
SERUM_CHUNK = zlib.compress(bytes(range(256)) * 4)


def serum() -> PluginMap:
    found = CATALOG.find(SERUM_AU)
    assert found is not None and found.id == "xfer.serum"
    return found


def au_state(chunk: bytes = SERUM_CHUNK, fx_id: bytes = b"XfsX") -> bytes:
    """A Serum AU preset laid out as the two in the owner's sets are: a VST2 program under ``vstdata``."""
    return plistlib.dumps(
        {
            "ProgramNumber": 0,
            "manufacturer": SERUM_AU.identifier[2],
            "name": " - Init - ",
            "subtype": SERUM_AU.identifier[1],
            "type": SERUM_AU.identifier[0],
            "version": 1,
            "vstdata": FxpChunkProgram(fx_id, 1, 315, b" - Init - ", chunk).encode(),
        }
    )


def test_a_mac_set_s_serum_crosses_to_the_vst3_as_its_vst2_chunk() -> None:
    assert serum().cross(FormatState(au_state()), PluginKind.AU, VST3) == FormatState(SERUM_CHUNK, b"")
    assert serum().plugin_format(PluginKind.AU).measured.evidence == (StateEvidence.HOSTED,)


def test_serum_is_never_written_as_an_au() -> None:
    """Its registered component flags are not recorded, and a set repeats them."""
    assert not serum().can_write(PluginKind.AU)
    with pytest.raises(StateTransformError, match="registration is not recorded"):
        serum().lower(SERUM_CHUNK, PluginKind.AU)


def test_the_au_program_reads_back_through_the_framing() -> None:
    framing = serum().plugin_format(PluginKind.AU).framing
    assert framing.lift(framing.lower(SERUM_CHUNK)) == SERUM_CHUNK


def test_a_program_for_another_plugin_is_refused() -> None:
    with pytest.raises(StateTransformError, match="fxID"):
        serum().lift(FormatState(au_state(fx_id=b"Othr")), PluginKind.AU)


def test_an_au_preset_without_vstdata_is_refused() -> None:
    info = plistlib.loads(au_state())
    del info["vstdata"]
    with pytest.raises(StateTransformError, match="no vstdata"):
        serum().lift(FormatState(plistlib.dumps(info)), PluginKind.AU)


# -- Serum 2's Audio Unit --------------------------------------------------------


def au_init() -> bytes:
    """The installed Serum 2 AU's own init preset, read headless on a Mac (see the manifest)."""
    return (FIXTURES / "serum2-init.au.classinfo").read_bytes()


def test_a_serum_2_au_device_finds_serum_2_s_map() -> None:
    assert CATALOG.find(AuId.of("aumu", "Xf2X", "XFER")) is serum_2()


def test_the_au_keeps_the_vst3_s_processor_state_and_crosses_to_the_vst3() -> None:
    crossed = serum_2().cross(FormatState(au_init()), PluginKind.AU, VST3)
    state = XferJson.parse(crossed.state)
    assert (state.component, state.product_version) == ("processor", "2.1.5")
    assert crossed.controller == b""


def test_a_vst3_patch_is_written_as_the_smallest_preset_the_au_restored() -> None:
    """Measured on the Mac: the AU took this preset, kept the sample path and went looking for the file."""
    assert serum_2().can_write(PluginKind.AU)
    written = serum_2().lower(serum_2_sample(), PluginKind.AU)
    info = plistlib.loads(written.state)
    assert info == {
        "type": SERUM_2_AU.identifier[0],
        "subtype": SERUM_2_AU.identifier[1],
        "manufacturer": SERUM_2_AU.identifier[2],
        "version": 0,
        "name": "",
        "Processor State": serum_2_sample(),
    }
    assert serum_2().lift(written, PluginKind.AU) == serum_2_sample()


def test_a_plan_names_each_format_by_hand() -> None:
    assert [plugin_format.plan_name for plugin_format in serum_2().formats.values()] == ["serum_2_vst3", "serum_2_au"]
    assert serum().plugin_format(PluginKind.AU).plan_name == "serum_au"


def test_serum_2_s_au_registration_is_xfer_s_with_no_sandbox_flag() -> None:
    registration = serum_2().plugin_format(PluginKind.AU).registration
    assert registration is not None and (registration.manufacturer, registration.flags) == ("Xfer Records", 0)
