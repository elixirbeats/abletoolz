"""The sample catalog: indexed sample records in SQLite.

The ``samples`` table's layout lives here and only here: the schema, how a
record maps onto its columns, and the reads and writes that use them.
"""

from __future__ import annotations

import json
import pathlib
import re
import sqlite3
import threading
from collections.abc import Mapping
from contextlib import closing
from types import MappingProxyType
from typing import Self

type SampleValue = str | int | float
type SampleRecord = Mapping[str, SampleValue]
type DatabaseT = Mapping[str, SampleRecord]

FIELDS = ("name", "size", "crc", "last_modified", "last_modified_ns", "crc_version")
SCHEMA_VERSION = 1
_COLUMNS = ",".join((*FIELDS, "extra"))
_STORE = (
    f"INSERT INTO samples(path,root_id,{_COLUMNS}) VALUES ({','.join('?' * (len(FIELDS) + 3))}) "
    "ON CONFLICT(path) DO UPDATE SET "
    + ",".join(f"{column}=excluded.{column}" for column in ("root_id", *FIELDS, "extra"))
)


def _record_from_row(row: tuple[object, ...]) -> dict[str, SampleValue]:
    extra = row[-1]
    if not isinstance(extra, str):
        raise ValueError("Invalid sample metadata")
    record: dict[str, SampleValue] = json.loads(extra)
    for field, value in zip(FIELDS, row[:-1], strict=True):
        if isinstance(value, (str, int, float)):
            record[field] = value
    return record


def _record_values(record: SampleRecord) -> tuple[SampleValue | None, ...]:
    extra = {key: value for key, value in record.items() if key not in FIELDS}
    return (*(record.get(field) for field in FIELDS), json.dumps(extra, ensure_ascii=False))


def read_stored(connection: sqlite3.Connection, path: str) -> tuple[dict[str, SampleValue], int | None] | None:
    """The record stored for ``path`` and the id of the root it was scanned under; None when nothing is."""
    row = connection.execute(f"SELECT {_COLUMNS},root_id FROM samples WHERE path=?", (path,)).fetchone()
    return None if row is None else (_record_from_row(row[:-1]), row[-1])


def store(connection: sqlite3.Connection, path: str, root_id: int | None, record: SampleRecord) -> None:
    """Write ``record`` for ``path``, scanned under ``root_id``, over whatever was stored for it."""
    connection.execute(_STORE, (path, root_id, *_record_values(record)))


def initialize(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, SCHEMA_VERSION):
        raise ValueError(f"Unsupported sample database schema: {version}")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    had_search = connection.execute("SELECT 1 FROM sqlite_master WHERE name='sample_search'").fetchone() is not None
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS roots (
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS samples (
            path TEXT PRIMARY KEY,
            root_id INTEGER REFERENCES roots(id),
            name TEXT,
            size INTEGER,
            crc INTEGER,
            last_modified REAL,
            last_modified_ns INTEGER,
            crc_version TEXT,
            extra TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS samples_name_size_crc ON samples(name, size, crc);
        CREATE INDEX IF NOT EXISTS samples_size_crc ON samples(size, crc);
        CREATE INDEX IF NOT EXISTS samples_root ON samples(root_id);
        CREATE VIRTUAL TABLE IF NOT EXISTS sample_search USING fts5(
            name, path, content='samples', content_rowid='rowid',
            tokenize='unicode61 remove_diacritics 2'
        );
        CREATE TRIGGER IF NOT EXISTS samples_search_insert AFTER INSERT ON samples BEGIN
            INSERT INTO sample_search(rowid,name,path) VALUES (new.rowid,new.name,new.path);
        END;
        CREATE TRIGGER IF NOT EXISTS samples_search_delete AFTER DELETE ON samples BEGIN
            INSERT INTO sample_search(sample_search,rowid,name,path) VALUES ('delete',old.rowid,old.name,old.path);
        END;
        CREATE TRIGGER IF NOT EXISTS samples_search_update AFTER UPDATE OF name,path ON samples
        WHEN old.name IS NOT new.name OR old.path IS NOT new.path BEGIN
            INSERT INTO sample_search(sample_search,rowid,name,path) VALUES ('delete',old.rowid,old.name,old.path);
            INSERT INTO sample_search(rowid,name,path) VALUES (new.rowid,new.name,new.path);
        END;
        PRAGMA user_version = 1;
    """)
    if not had_search:
        with connection:
            connection.execute("INSERT INTO sample_search(sample_search) VALUES ('rebuild')")


class SampleCatalog:
    """A read-only SQLite snapshot shared safely by concurrent set workers.

    Open a database file with :meth:`open`; :meth:`from_records` builds the
    same schema in memory, so a test or a hand-picked set of files runs the
    same queries a scanned library does.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._lock = threading.RLock()
        self._connection = connection
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            self._connection.close()
            raise ValueError(f"Unsupported sample database schema: {version}")
        self._connection.execute("BEGIN")
        self._connection.execute("SELECT count(*) FROM samples").fetchone()

    @classmethod
    def open(cls, path: pathlib.Path) -> Self:
        """Open the database at ``path`` read-only, pinned to the revision it holds now."""
        return cls(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False))

    @classmethod
    def from_records(cls, records: DatabaseT) -> Self:
        """A catalog of exactly ``records``, held in memory; a copy, so later changes to them do not show."""
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        initialize(connection)
        with connection:
            for path, record in records.items():
                store(connection, path, None, record)
        return cls(connection)

    def backup_to(self, path: pathlib.Path) -> None:
        """Copy this pinned revision for workers that need separate SQLite connections."""
        if path.exists():
            raise FileExistsError(path)
        with self._lock, closing(sqlite3.connect(path)) as destination:
            self._connection.backup(destination)

    def candidates(self, name: str, *, size: int | None = None, crc: int | None = None) -> dict[str, SampleRecord]:
        """Every record named ``name`` and, given ``size`` and ``crc``, every record storing both, in one query.

        The second half finds a file renamed since the reference was saved.
        """
        query = f"SELECT path,{_COLUMNS} FROM samples WHERE name=?"
        values: list[str | int] = [name]
        if size is not None and crc is not None:
            query += " OR (size=? AND crc=?)"
            values += [size, crc]
        with self._lock:
            rows = self._connection.execute(query + " ORDER BY rowid", values).fetchall()
        return {str(row[0]): MappingProxyType(_record_from_row(row[1:])) for row in rows}

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def search(self, text: str, *, limit: int = 50) -> tuple[str, ...]:
        """Find filename and folder words with prefix matching, ranked by relevance."""
        words = re.findall(r"\w+", text, flags=re.UNICODE)
        if not words or limit <= 0:
            return ()
        query = " AND ".join(f'"{word}"*' for word in words)
        with self._lock:
            rows = self._connection.execute(
                """SELECT samples.path FROM sample_search JOIN samples ON samples.rowid=sample_search.rowid
                   WHERE sample_search MATCH ? ORDER BY bm25(sample_search,4.0,1.0),samples.rowid LIMIT ?""",
                (query, limit),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
