"""Time the relink lookup's catalog query by filename: python -m tools.benchmark_sample_lookup."""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import time

from abletoolz.samples.catalog import SampleCatalog, SampleRecord


def query_time_ms(catalog: SampleCatalog, queries: list[tuple[str, list[str]]], repeats: int) -> float:
    elapsed = []
    for _ in range(repeats):
        started = time.perf_counter()
        for name, expected in queries:
            actual = list(catalog.candidates(name))
            if actual != expected:
                raise AssertionError(f"Unexpected candidates for {name}: {actual}")
        elapsed.append((time.perf_counter() - started) * 1000)
    return statistics.median(elapsed)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=100_000)
    parser.add_argument("--queries", type=int, default=100)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    count, query_count, repeats = int(args.records), int(args.queries), int(args.repeats)
    if min(count, query_count, repeats) < 1:
        parser.error("records, queries and repeats must be positive")
    records: dict[str, SampleRecord] = {}
    for number in range(count):
        path = pathlib.Path("samples") / f"sample-{number}.wav"
        records[str(path)] = {"name": path.name, "size": number}
    queries = []
    for query in range(query_count):
        number = query * count // query_count
        path = pathlib.Path("samples") / f"sample-{number}.wav"
        queries.append((path.name, [str(path)]))
    queries.append(("absent.wav", []))

    started = time.perf_counter()
    catalog = SampleCatalog.from_records(records)
    build_seconds = time.perf_counter() - started
    with catalog:
        print(
            json.dumps(
                {
                    "benchmark": "in_memory_filename_lookup",
                    "records": count,
                    "queries_per_batch": len(queries),
                    "repeats": repeats,
                    "index_build_seconds": build_seconds,
                    "indexed_median_batch_ms": query_time_ms(catalog, queries, repeats),
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
