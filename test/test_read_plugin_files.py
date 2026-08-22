"""Reading plugin metadata off disk: macOS bundles, Mach-O headers, and who reads what.

Synthetic and platform independent. A bundle here is a directory with an
Info.plist and a handful of header bytes where the binary goes, which is enough
for every question the scanner asks -- so what a Mac would find in a folder is
checked from any machine.
"""

from __future__ import annotations

import pathlib
import plistlib
import struct

import pytest

from abletoolz.plugin_parsers.read_plugin_files import scan_mac_bundles, scan_plugin_dirs

ARM64 = 0x0100000C
X86_64 = 0x01000007

THIN_64 = b"\xcf\xfa\xed\xfe"
THIN_64_SWAPPED = b"\xfe\xed\xfa\xcf"
FAT = b"\xca\xfe\xba\xbe"
FAT_64 = b"\xca\xfe\xba\xbf"


def thin(cpu_type: int, magic: bytes = THIN_64) -> bytes:
    """A single-architecture Mach-O header, big endian when the magic is swapped."""
    order = ">" if magic.startswith(b"\xfe") else "<"
    return magic + struct.pack(f"{order}I", cpu_type) + b"\x00" * 24


def fat(*cpu_types: int, magic: bytes = FAT) -> bytes:
    """A universal Mach-O header: a count, then one record per architecture."""
    size = 32 if magic == FAT_64 else 20
    records = b"".join(struct.pack(">I", cpu_type) + b"\x00" * (size - 4) for cpu_type in cpu_types)
    return magic + struct.pack(">I", len(cpu_types)) + records


def make_bundle(
    root: pathlib.Path,
    name: str,
    *,
    plist: dict[str, object] | None = None,
    raw_plist: bytes | None = None,
    binary: bytes | None = None,
    executable: str = "Binary",
) -> pathlib.Path:
    """A mac-shaped plugin bundle; no Info.plist is written when both plist args are None."""
    bundle = root / name
    contents = bundle / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    if raw_plist is not None:
        (contents / "Info.plist").write_bytes(raw_plist)
    elif plist is not None:
        with (contents / "Info.plist").open("wb") as file:
            plistlib.dump(plist, file)
    if binary is not None:
        (contents / "MacOS" / executable).write_bytes(binary)
    return bundle


def pro_q(root: pathlib.Path, name: str = "FabFilter Pro-Q 3.vst3") -> pathlib.Path:
    """A bundle shaped like a shipped plugin: named, versioned, universal."""
    return make_bundle(
        root,
        name,
        plist={
            "CFBundleName": "FabFilter Pro-Q 3",
            "CFBundleExecutable": "FabFilter Pro-Q 3",
            "CFBundleShortVersionString": "3.24",
            "CFBundleIdentifier": "com.fabfilter.Pro-Q-3",
        },
        binary=fat(X86_64, ARM64),
        executable="FabFilter Pro-Q 3",
    )


# -- what a bundle answers --------------------------------------------------


def test_a_vst3_bundle_is_read_as_a_vst3(tmp_path: pathlib.Path) -> None:
    bundle = pro_q(tmp_path)
    (record,) = scan_mac_bundles(tmp_path)

    assert record["format"] == "VST3"
    assert record["name"] == "FabFilter Pro-Q 3"
    assert record["version"] == "3.24"
    assert record["arch"] == "universal"
    assert record["path"] == str(bundle)
    assert record["module"] == str(bundle / "Contents" / "MacOS" / "FabFilter Pro-Q 3")


def test_a_vst_bundle_is_read_as_a_vst2(tmp_path: pathlib.Path) -> None:
    """The mac VST2 is a bundle too, so a folder of them is not an empty folder."""
    make_bundle(tmp_path, "Effectrix.vst", plist={"CFBundleName": "Effectrix"}, binary=thin(ARM64))
    (record,) = scan_mac_bundles(tmp_path)

    assert record["format"] == "VST2"
    assert record["name"] == "Effectrix"
    assert record["arch"] == "arm64"


def test_a_component_bundle_is_left_to_the_audio_unit_index(tmp_path: pathlib.Path) -> None:
    """An AU is known by its type/subtype/manufacturer codes, which a scan record can't hold."""
    make_bundle(tmp_path, "Thing.component", plist={"CFBundleName": "Thing"}, binary=thin(ARM64))
    assert scan_mac_bundles(tmp_path) == []


def test_the_display_name_is_preferred_to_the_bundle_name(tmp_path: pathlib.Path) -> None:
    """Both are declared and they differ; a set stores the one Live shows."""
    make_bundle(tmp_path, "PQ3.vst3", plist={"CFBundleDisplayName": "Pro-Q 3", "CFBundleName": "PQ3"})
    assert scan_mac_bundles(tmp_path)[0]["name"] == "Pro-Q 3"


def test_a_bundle_that_declares_no_name_is_known_by_its_directory(tmp_path: pathlib.Path) -> None:
    make_bundle(tmp_path, "Thing.vst3", plist={"CFBundleIdentifier": "com.example.thing"})
    record = scan_mac_bundles(tmp_path)[0]

    assert record["name"] == "Thing"
    assert record["version"] == ""


def test_the_bundle_version_falls_back_to_the_build_version(tmp_path: pathlib.Path) -> None:
    make_bundle(tmp_path, "Thing.vst3", plist={"CFBundleVersion": "1.2.3.4"})
    assert scan_mac_bundles(tmp_path)[0]["version"] == "1.2.3.4"


def test_a_directory_without_an_info_plist_is_not_a_bundle(tmp_path: pathlib.Path) -> None:
    """Windows ships single-file .vst3s that land in shared folders; they are not bundles."""
    make_bundle(tmp_path, "Thing.vst3")
    (tmp_path / "Single.vst3").write_bytes(b"MZ")
    assert scan_mac_bundles(tmp_path) == []


def test_a_broken_plist_costs_a_bundle_its_name_not_its_record(tmp_path: pathlib.Path) -> None:
    """One unreadable plist must not lose the plugin, nor the ones beside it."""
    make_bundle(tmp_path, "Broken.vst3", raw_plist=b"\x00not a plist at all", binary=thin(ARM64))
    pro_q(tmp_path)
    by_name = {record["name"]: record for record in scan_mac_bundles(tmp_path)}

    assert by_name["Broken"]["arch"] == "arm64"
    assert by_name["FabFilter Pro-Q 3"]["version"] == "3.24"


# -- which binary, and what it says -----------------------------------------


def test_the_executable_the_plist_names_is_the_one_read(tmp_path: pathlib.Path) -> None:
    """Bundles carry helper files beside the plugin; the plist says which one runs."""
    bundle = make_bundle(
        tmp_path, "Thing.vst3", plist={"CFBundleExecutable": "Thing"}, binary=thin(ARM64), executable="Thing"
    )
    (bundle / "Contents" / "MacOS" / "AAA Helper").write_bytes(thin(X86_64))
    record = scan_mac_bundles(tmp_path)[0]

    assert record["module"] == str(bundle / "Contents" / "MacOS" / "Thing")
    assert record["arch"] == "arm64"


def test_a_bundle_naming_no_executable_reads_what_it_has(tmp_path: pathlib.Path) -> None:
    make_bundle(tmp_path, "Thing.vst3", plist={"CFBundleName": "Thing"}, binary=thin(X86_64), executable="Thing")
    assert scan_mac_bundles(tmp_path)[0]["arch"] == "x86_64"


@pytest.mark.parametrize(
    ("binary", "arch"),
    [
        (thin(ARM64), "arm64"),
        (thin(X86_64), "x86_64"),
        (thin(7), "x86"),
        (thin(ARM64, magic=THIN_64_SWAPPED), "arm64"),
        (fat(X86_64, ARM64), "universal"),
        (fat(X86_64, ARM64, magic=FAT_64), "universal"),
        (fat(ARM64), "arm64"),
        (fat(ARM64, ARM64), "arm64"),
        (b"MZ" + b"\x00" * 32, "unknown"),
        (b"\xcf\xfa", "unknown"),
        (fat(X86_64)[:6], "unknown"),
    ],
)
def test_the_mach_o_header_reports_what_it_was_built_for(tmp_path: pathlib.Path, binary: bytes, arch: str) -> None:
    make_bundle(tmp_path, "Thing.vst3", plist={"CFBundleExecutable": "Thing"}, binary=binary, executable="Thing")
    assert scan_mac_bundles(tmp_path)[0]["arch"] == arch


def test_a_bundle_with_no_binary_at_all_is_still_a_plugin(tmp_path: pathlib.Path) -> None:
    """Architecture is unknown, but the name and the path are what a set is matched against."""
    make_bundle(tmp_path, "Thing.vst3", plist={"CFBundleName": "Thing"})
    record = scan_mac_bundles(tmp_path)[0]

    assert record["arch"] == "unknown"
    assert "module" not in record


# -- who reads what ---------------------------------------------------------


def windows_plugin(folder: pathlib.Path, name: str = "Serum_x64.dll") -> pathlib.Path:
    """The smallest file the PE header reader calls a 64-bit module."""
    image = bytearray(0x80)
    image[0:2] = b"MZ"
    image[0x3C:0x40] = (0x40).to_bytes(4, "little")
    image[0x40:0x44] = b"PE\x00\x00"
    image[0x58:0x5A] = (0x20B).to_bytes(2, "little")
    (folder / name).write_bytes(bytes(image))
    return folder / name


def test_a_mac_reads_bundles_and_files_both(tmp_path: pathlib.Path) -> None:
    """A Windows plugin folder on a shared drive is a real thing to point a scan at."""
    pro_q(tmp_path)
    windows_plugin(tmp_path)
    found = {record["name"]: record["arch"] for record in scan_plugin_dirs([tmp_path], platform="darwin")}

    assert found == {"FabFilter Pro-Q 3": "universal", "Serum_x64": "x64"}


def test_windows_reads_files_only(tmp_path: pathlib.Path) -> None:
    pro_q(tmp_path)
    windows_plugin(tmp_path)
    assert [record["name"] for record in scan_plugin_dirs([tmp_path], platform="win32")] == ["Serum_x64"]


def test_an_os_with_no_reader_scans_nothing(tmp_path: pathlib.Path) -> None:
    pro_q(tmp_path)
    windows_plugin(tmp_path)
    assert scan_plugin_dirs([tmp_path], platform="linux") == []


def test_a_bundle_two_readers_reach_is_described_by_the_binary_this_os_loads(tmp_path: pathlib.Path) -> None:
    """A bundle carrying a Windows module answers to the file reader too, and must not read as x64."""
    bundle = pro_q(tmp_path)
    windows_plugin(bundle / "Contents", "FabFilter Pro-Q 3.dll")
    found = [record for record in scan_plugin_dirs([tmp_path], platform="darwin") if record["path"] == str(bundle)]

    assert [record["arch"] for record in found] == ["universal"]


def test_a_directory_scanned_twice_is_read_once(tmp_path: pathlib.Path) -> None:
    """Config can name a folder the standard locations already cover."""
    pro_q(tmp_path)
    assert len(scan_plugin_dirs([tmp_path, tmp_path], platform="darwin")) == 1


def test_a_missing_directory_is_skipped(tmp_path: pathlib.Path) -> None:
    assert scan_plugin_dirs([tmp_path / "not there"], platform="darwin") == []
