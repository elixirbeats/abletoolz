"""Fixtures shared across the suite.

``locations`` is the computer every test runs on: a scratch config folder, no
plugin folders, no Live database and no User Library, on Windows. A test that
needs plugins installed or another OS says so with ``dataclasses.replace`` and
hands the result in. Anything that falls back to ``Locations.this_computer()``
gets the same scratch computer, so no test reads or writes the real one.
``child_env`` does the same for a test that runs abletoolz as a child process.
"""

from __future__ import annotations

import gzip
import math
import os
import pathlib
import plistlib
import sqlite3
import struct
from contextlib import closing
from xml.etree import ElementTree as ET

import pytest

from abletoolz import cli
from abletoolz.formats import live_document
from abletoolz.live_set import AbletonSet
from abletoolz.live_set.clips import ClipLocation, MidiClipRef
from abletoolz.machine import Locations
from abletoolz.plugins.config import AbletoolzConfig
from abletoolz.plugins.container.device import PluginDevice, PluginDevices
from abletoolz.plugins.identity.ids import AuId
from abletoolz.plugins.state.fabfilter import EditorState
from abletoolz.samples import database
from abletoolz.samples.catalog import SampleRecord


@pytest.fixture(autouse=True)
def locations(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> Locations:
    """A computer with nothing installed and a config folder of its own, standing in for this one."""
    here = Locations(platform="win32", config_dir=tmp_path_factory.mktemp("config"))
    monkeypatch.setattr(Locations, "this_computer", staticmethod(lambda: here))
    # abletoolz.samples reads its default database path once, at import.
    monkeypatch.setattr(database, "DEFAULT_DB_PATH", here.sample_db)
    return here


@pytest.fixture
def child_env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """An environment for a ``python -m abletoolz`` child whose home, config and program folders are all scratch.

    A child process reads its locations off the environment, not off the
    ``locations`` fixture, so this is what keeps it off the real config folder,
    plugin folders and Live database.
    """
    home = str(tmp_path_factory.mktemp("home"))
    folders = ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "ProgramFiles", "XDG_CONFIG_HOME")
    return {**os.environ, **dict.fromkeys(folders, home)}


# -- live sets ----------------------------------------------------------------
# What the live_set and CLI tests share: a skeleton parsed where it lies or as a
# copy to write, the saved file read back past abletoolz, and the CLI in-process.

SKELETONS = pathlib.Path(__file__).parent / "version_fixtures" / "skeletons"


def skeleton_set(key: str) -> AbletonSet:
    """A version skeleton parsed where it lies, for tests that only read it."""
    ableton_set = AbletonSet(SKELETONS / f"{key}.als")
    assert ableton_set.parse()
    return ableton_set


def copy_set(directory: pathlib.Path, key: str = "11.3.42", name: str = "set.als") -> pathlib.Path:
    """A copy of a version skeleton at ``directory/name``, to be written."""
    copy = directory / name
    copy.write_bytes((SKELETONS / f"{key}.als").read_bytes())
    return copy


def writable_set(key: str, tmp_path: pathlib.Path) -> AbletonSet:
    """A writable copy of a version skeleton, parsed."""
    ableton_set = AbletonSet(copy_set(tmp_path, key, f"{key}.als"))
    assert ableton_set.parse()
    return ableton_set


def raw_root(path: pathlib.Path) -> ET.Element:
    """Parse a saved .als straight from disk, bypassing abletoolz entirely."""
    return ET.fromstring(gzip.decompress(path.read_bytes()).decode("utf-8"))


def first_session_clip(ableton_set: AbletonSet) -> MidiClipRef:
    return next(clip for clip in ableton_set.clips.midi() if clip.location is ClipLocation.SESSION)


def run_cli(*argv: str, config: AbletoolzConfig | None = None, locations: Locations | None = None) -> int:
    """The command line ``argv``'s exit code, run with ``config`` (an empty one by default) for config.yaml.

    ``locations`` default to the suite's own.
    """
    return cli.main(argv, config=config or AbletoolzConfig(), locations=locations)


# -- sample databases -----------------------------------------------------------


def stored_paths(db_path: pathlib.Path) -> list[str]:
    """Every path the database at ``db_path`` holds, in the order it was stored."""
    with closing(sqlite3.connect(db_path)) as connection:
        return [str(row[0]) for row in connection.execute("SELECT path FROM samples ORDER BY rowid")]


def stored_records(db_path: pathlib.Path) -> dict[str, SampleRecord]:
    """Every record the database at ``db_path`` holds, read through the catalog's own lookup by name."""
    with closing(sqlite3.connect(db_path)) as connection:
        names = [str(row[0]) for row in connection.execute("SELECT DISTINCT name FROM samples")]
    with database.load_db(db_path) as catalog:
        return {path: dict(record) for name in names for path, record in catalog.candidates(name).items()}


# -- plugin tests -------------------------------------------------------------
# What the plugin tests share: the devices in the version skeletons, a
# Live plugin database in the schema measured on Live 12.4.5b, and plugin files
# shaped like the ones a scan reads. Imported by name (``from conftest import``).


def skeleton(key: str) -> ET.Element:
    """The document of one version skeleton, read fresh."""
    return live_document.read(SKELETONS / f"{key}.als")


def device_named(root: ET.Element, name: str) -> PluginDevice:
    """The plugin device in ``root`` whose identity has ``name``."""
    return next(device for device in PluginDevices(root) if device.identity.name == name)


def serum_as(root: ET.Element, name: str, unique_id: int, state: bytes | None = None) -> PluginDevice:
    """A skeleton's Serum_x64 VST2 device renamed and renumbered as another plugin's, holding ``state`` if given."""
    device = device_named(root, "Serum_x64")
    device.set_name(name)
    device.set_unique_id(unique_id)
    if state is not None:
        device.set_state(state)
    return device


def parse_editor_state(payload: bytes) -> EditorState:
    """Read a FabFilter editor state back, so a test can hold what ``EditorState.encode`` writes against a set.

    The layout is the one :class:`EditorState` documents. Reading stops at the
    end of the controller list, so a VST3 ``ControllerState`` reads with its
    trailer still after it.
    """

    def string(at: int) -> tuple[str, int]:
        (length,) = struct.unpack_from("<I", payload, at)
        return payload[at + 4 : at + 4 + length].decode("latin1"), at + 4 + length

    (version,) = struct.unpack_from("<I", payload, 4)
    preset_name, at = string(8)
    instance_index, first = struct.unpack_from("<iI", payload, at)
    label, at = string(at + 8)
    second, magic, third, count = struct.unpack_from("<I4sII", payload, at)
    assert magic == b"CuSV", f"{magic!r} where the controller list should start"
    at += 16
    controllers: list[tuple[str, str]] = []
    for _ in range(count):
        name, at = string(at)
        value, at = string(at)
        controllers.append((name, value))
    return EditorState(
        magic=payload[:4],
        version=version,
        preset_name=preset_name,
        instance_index=instance_index,
        label=label,
        controllers=tuple(controllers),
        carried=(first, second, third),
    )


# Class ids as Live's database spells them, dashed. The first is Serum's, measured.
SERUM_CID = "56535458-6673-5873-6572-756d00000000"
SERUM_FIELDS = (1448301656, 1718835315, 1701999981, 0)
OTHER_CID = "0000000a-0000-000b-0000-000c0000000d"
OTHER_FIELDS = (10, 11, 12, 13)

# Live 12.4.5b, Live-plugins-1.db, transcribed exactly.
_LIVE_DATABASE_SCHEMA = """
CREATE TABLE version (version INT, platform INT);
CREATE TABLE plugin_domains (id TEXT, module_id INTEGER DEFAULT 0, relpath TEXT, enabled INTEGER DEFAULT 0);
CREATE TABLE plugin_modules (
    module_id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT, arch INTEGER DEFAULT 0,
    processor INTEGER DEFAULT 0, scanstate INTEGER DEFAULT 0, fingerprint TEXT);
CREATE TABLE plugins (
    plugin_id INTEGER PRIMARY KEY AUTOINCREMENT, module_id INTEGER DEFAULT 0, dev_identifier TEXT,
    name TEXT, vendor TEXT, version TEXT, sdk_version TEXT, flags INTEGER DEFAULT 0,
    scanstate INTEGER DEFAULT 0, subcategories TEXT, enabled INTEGER DEFAULT 0);
"""

# One plugin row: (name, dev_identifier, module, domain, domain enabled).
type DatabaseRow = tuple[str, str, pathlib.Path, str, bool]


def write_live_database(path: pathlib.Path, rows: list[DatabaseRow]) -> pathlib.Path:
    """A Live plugin database file holding ``rows``."""
    connection = sqlite3.connect(path)
    connection.executescript(_LIVE_DATABASE_SCHEMA)
    connection.execute("INSERT INTO version VALUES (1, 1)")
    for index, (name, dev_identifier, module, domain, enabled) in enumerate(rows, start=1):
        connection.execute(
            "INSERT INTO plugin_modules (module_id, path, arch, processor, scanstate, fingerprint)"
            " VALUES (?, ?, 3, 1, 1, '')",
            (index, str(module)),
        )
        connection.execute(
            "INSERT INTO plugin_domains (id, module_id, relpath, enabled) VALUES (?, ?, '', ?)",
            (domain, index, int(enabled)),
        )
        # scanstate and enabled are 1 on every row of the real database.
        connection.execute(
            "INSERT INTO plugins (module_id, dev_identifier, name, vendor, version, sdk_version,"
            " flags, scanstate, subcategories, enabled) VALUES (?, ?, ?, 'Vendor', '1', '1', 0, 1, '', 1)",
            (index, dev_identifier, name),
        )
    connection.commit()
    connection.close()
    return path


def live_database(folder: pathlib.Path, rows: list[DatabaseRow]) -> pathlib.Path:
    """A ``Live Database`` folder in ``folder`` whose plugins database holds ``rows``."""
    database_dir = folder / "Live Database"
    database_dir.mkdir()
    write_live_database(database_dir / "Live-plugins-1.db", rows)
    return database_dir


def installed(folder: pathlib.Path, name: str) -> pathlib.Path:
    """An empty plugin file that exists, which is all a module path check asks."""
    module = folder / name
    module.write_bytes(b"")
    return module


def pe_module(path: pathlib.Path) -> pathlib.Path:
    """The smallest file a PE header reader calls a 64-bit AMD64 module."""
    image = bytearray(0x80)
    image[0:2] = b"MZ"
    struct.pack_into("<I", image, 0x3C, 0x40)
    image[0x40:0x44] = b"PE\x00\x00"
    struct.pack_into("<H", image, 0x44, 0x8664)
    struct.pack_into("<H", image, 0x58, 0x20B)
    path.write_bytes(bytes(image))
    return path


def make_bundle(
    root: pathlib.Path,
    name: str,
    *,
    plist: dict[str, object] | None = None,
    raw_plist: bytes | None = None,
) -> pathlib.Path:
    """A mac-shaped plugin bundle ``root/name``; no Info.plist is written when both plist args are None."""
    bundle = root / name
    contents = bundle / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    if raw_plist is not None:
        (contents / "Info.plist").write_bytes(raw_plist)
    elif plist is not None:
        with (contents / "Info.plist").open("wb") as file:
            plistlib.dump(plist, file)
    return bundle


# -- native plugin saves ------------------------------------------------------
# The Valhalla and Surge fixtures: each format's own readback of one edited
# patch, taken with a headless host per format (see the folder's manifest.json).

AU_FIXTURES = pathlib.Path(__file__).parent / "state_fixtures" / "audio_units"

# The AU identity each fixture's plugin stores.
AU_UNITS = {
    "valhalla-supermassive": AuId.of("aufx", "sMas", "oDin"),
    "valhalla-freqecho": AuId.of("aufx", "FqEh", "oDin"),
    "valhalla-room": AuId.of("aufx", "Ruum", "oDin"),
    "valhalla-shimmer": AuId.of("aufx", "shmr", "oDin"),
    "valhalla-delay": AuId.of("aufx", "dLay", "oDin"),
    "valhalla-plate": AuId.of("aufx", "plat", "oDin"),
    "surge-xt-effects": AuId.of("aufx", "SFXT", "VmbA"),
    "surge-xt": AuId.of("aumu", "SgXT", "VmbA"),
}


def au_fixture(slug: str, suffix: str) -> bytes:
    """One saved state of the plugin ``slug``, in the format ``suffix`` names."""
    return (AU_FIXTURES / f"{slug}.{suffix}").read_bytes()


def pro_q2_bank() -> list[float]:
    """A Pro-Q 2 bank: 24 bands, then its globals."""
    band = [1.0, math.log(100, 3000), 0.5, 0.5, 0.0, 0.125, 1.0]
    return band * 24 + [
        0.0,
        0.25,
        0.0,
        0.5,
        0.5,
        0.5,
        0.0,
        0.0,
        0.0,
        1.0,
        1.0,
        0.0,
        0.5,
        1 / 3,
        0.5,
        0.75,
        0.0,
        1.0,
        2 / 3,
        0.0,
        0.0,
        0.5,
        0.0,
    ]
