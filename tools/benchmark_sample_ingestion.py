"""Benchmark selected sample folders without modifying their audio files."""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import statistics
import time
from collections.abc import Callable
from contextlib import closing

from abletoolz.samples import database
from abletoolz.samples.crc import file_crc
from abletoolz.samples.discovery import DiscoveryBackend, discover_audio_files


def timed[T](action: Callable[[], T]) -> tuple[T, float]:
    start = time.perf_counter()
    result = action()
    return result, time.perf_counter() - start


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", type=pathlib.Path)
    parser.add_argument("--catalog", type=pathlib.Path)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    roots: list[pathlib.Path] = [path.resolve(strict=True) for path in args.roots]
    if any(root == pathlib.Path(root.anchor) for root in roots):
        parser.error("Select sample folders, not filesystem roots")
    collected: dict[str, tuple[int, int, int, int]] = {}
    for backend in ("portable", "auto"):
        kind: DiscoveryBackend = backend
        start = time.perf_counter()
        files = {
            entry.path: (
                entry.metadata.st_size,
                entry.metadata.st_mtime_ns,
                entry.metadata.st_dev,
                entry.metadata.st_ino,
            )
            for root in roots
            for entry in discover_audio_files(root, kind)
        }
        print(
            json.dumps(
                {
                    "phase": "discovery_and_metadata",
                    "backend": backend,
                    "files": len(files),
                    "seconds": time.perf_counter() - start,
                }
            ),
            flush=True,
        )
        if collected and files != collected:
            raise AssertionError("Native and portable file paths or metadata differ")
        collected = files
    all_paths = list(collected)
    chosen = all_paths[:: max(1, len(all_paths) // 1024)][:1024]
    _, seconds = timed(lambda: [file_crc(pathlib.Path(path)) for path in chosen])
    print(json.dumps({"phase": "crc_headers", "files": len(chosen), "seconds": seconds}), flush=True)
    if args.catalog is None:
        return
    catalog_path: pathlib.Path = args.catalog
    if catalog_path.exists():
        parser.error("Choose a new catalog path for a reproducible initial build")
    _, seconds = timed(
        lambda: database.create_or_update_db(
            [str(root) for root in roots],
            catalog_path,
            workers=args.workers,
        )
    )
    print(json.dumps({"phase": "initial_ingestion", "files": len(collected), "seconds": seconds}), flush=True)
    _, seconds = timed(
        lambda: database.create_or_update_db(
            [str(root) for root in roots],
            catalog_path,
            workers=args.workers,
        )
    )
    print(json.dumps({"phase": "unchanged_ingestion", "seconds": seconds}), flush=True)
    catalog, seconds = timed(lambda: database.load_db(catalog_path))
    print(json.dumps({"phase": "open_catalog", "seconds": seconds}), flush=True)
    with closing(sqlite3.connect(catalog_path)) as connection:
        assert {str(row[0]) for row in connection.execute("SELECT path FROM samples")} == set(collected)
    with catalog:
        latencies = []
        for path in chosen:
            name = pathlib.Path(path).name
            record = catalog.candidates(name)[path]
            start = time.perf_counter()
            matches = catalog.candidates(name, size=int(record["size"]), crc=int(record["crc"]))
            assert path in matches
            latencies.append((time.perf_counter() - start) * 1000)
        print(
            json.dumps({"phase": "exact_lookup", "queries": len(latencies), "median_ms": statistics.median(latencies)}),
            flush=True,
        )
        elapsed = []
        for query in ("kick", "snare", "amen", "174"):
            start = time.perf_counter()
            catalog.search(query)
            elapsed.append((time.perf_counter() - start) * 1000)
        print(
            json.dumps({"phase": "text_lookup", "queries": len(elapsed), "median_ms": statistics.median(elapsed)}),
            flush=True,
        )


if __name__ == "__main__":
    main()
