"""Live's file checksum: the algorithm, its size limit, and one value Live itself wrote."""

from __future__ import annotations

import pathlib
import sqlite3
from contextlib import closing

from fastcrc.crc16 import umts

from abletoolz.samples import database
from abletoolz.samples.catalog import SampleCatalog
from abletoolz.samples.crc import MAX_CRC_SIZE, file_crc
from abletoolz.samples.relink import Evidence, find

# A 48-byte mono 8-bit wav (two silent samples) written by scipy. Live 11.2.10
# stored OriginalCrc 38134 for it.
TINY_WAV = bytes.fromhex(
    "524946462800000057415645666d74201000000001000100010000000400000004002000646174610400000000000000"
)


def test_crc16_check_value() -> None:
    """CRC-16/UMTS's catalogue check value over ASCII "123456789"."""
    assert umts(b"123456789") == 0xFEE8
    assert umts(b"") == 0


def test_file_crc_matches_live(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "tiny.wav"
    path.write_bytes(TINY_WAV)
    assert file_crc(path) == 38134


def test_file_crc_ignores_bytes_past_limit(tmp_path: pathlib.Path) -> None:
    head = bytes(range(256)) * (MAX_CRC_SIZE // 256)
    short = tmp_path / "short.wav"
    long = tmp_path / "long.wav"
    short.write_bytes(head)
    long.write_bytes(head + b"tail that Live never reads")
    assert file_crc(long) == file_crc(short) == umts(head)


def test_file_crc_covers_last_byte_inside_limit(tmp_path: pathlib.Path) -> None:
    head = bytes(MAX_CRC_SIZE)
    a = tmp_path / "a.wav"
    b = tmp_path / "b.wav"
    a.write_bytes(head)
    b.write_bytes(head[:-1] + b"\x01")
    assert file_crc(a) != file_crc(b)


def test_database_records_crc_and_backfills_old_records(tmp_path: pathlib.Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    sample = library / "tiny.wav"
    sample.write_bytes(TINY_WAV)
    db_path = tmp_path / "db.sqlite3"

    database.create_or_update_db([str(library)], db_path=db_path)
    with database.load_db(db_path) as db:
        assert db.candidates(sample.name)[str(sample.resolve())]["crc"] == 38134

    # A record written before the crc existed gets one on the next update.
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("UPDATE samples SET crc=NULL")
    with database.load_db(db_path) as db:
        assert "crc" not in db.candidates(sample.name)[str(sample.resolve())]
    database.create_or_update_db([str(library)], db_path=db_path)
    with database.load_db(db_path) as db:
        assert db.candidates(sample.name)[str(sample.resolve())]["crc"] == 38134


def test_a_stored_crc_picks_the_same_bytes_over_the_closer_folder(tmp_path: pathlib.Path) -> None:
    near = tmp_path / "samples" / "kick.wav"
    far = tmp_path / "elsewhere" / "deeper" / "kick.wav"
    for path in (near, far):
        path.parent.mkdir(parents=True)
        path.write_bytes(bytes(100))
    records = {
        str(near): {"name": "kick.wav", "size": 100, "crc": 1},
        str(far): {"name": "kick.wav", "size": 100, "crc": 2},
    }
    db = SampleCatalog.from_records(records)
    assert find(db, Evidence("kick.wav", near, size=100, crc=2)).found == far
    # No stored crc: the folder heuristics decide, as before.
    assert find(db, Evidence("kick.wav", near, size=100)).candidates == (near, far)
    # A crc only counts beside a matching size.
    assert Evidence("kick.wav", near, size=100, crc=2).same_bytes(records[str(far)])
    assert not Evidence("kick.wav", near, size=99, crc=2).same_bytes(records[str(far)])
