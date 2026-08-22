"""Structural diff of Ableton .als schemas across versions.

Collects every distinct tag path (root->...->tag chain, no indices) plus the
attribute names seen at each path, then diffs two files. Content differences
(different devices used, different clip counts) show up as path noise, so the
report groups by top-level subtree and only prints paths, not values.

This is how the rename table in ``doc/VERSION_DIFFS.md`` was harvested; rerun
it against a set saved by a new Live version to find what got renamed next.

Usage:
    python test/tools/schema_census.py OLD.als NEW.als
"""

from __future__ import annotations

import gzip
import pathlib
import sys
from collections import defaultdict
from xml.etree import ElementTree as ET


def load_root(path: pathlib.Path) -> ET.Element:
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return ET.fromstring(raw.decode("utf-8"))


def census(root: ET.Element) -> dict[str, set[str]]:
    """Map of structural path -> set of attribute names seen there."""
    paths: dict[str, set[str]] = defaultdict(set)

    def walk(el: ET.Element, prefix: str) -> None:
        path = f"{prefix}/{el.tag}" if prefix else el.tag
        paths[path].update(el.attrib.keys())
        for child in el:
            walk(child, path)

    walk(root, "")
    return dict(paths)


def shorten(path: str) -> str:
    return path.replace("Ableton/LiveSet/", "LS/")


def diff(a_name: str, a: dict[str, set[str]], b_name: str, b: dict[str, set[str]]) -> None:
    only_a = sorted(set(a) - set(b))
    only_b = sorted(set(b) - set(a))

    def collapse(paths: list[str]) -> list[str]:
        """Keep only paths whose parent is shared or is itself root of a new subtree."""
        out = []
        pathset = set(paths)
        for p in paths:
            parent = p.rsplit("/", 1)[0]
            if parent not in pathset:
                out.append(p)
        return out

    print(f"\n{'=' * 100}")
    print(f"ONLY IN {a_name} (root paths of removed/renamed subtrees): {len(only_a)} paths total")
    for p in collapse(only_a):
        print(f"  - {shorten(p)}")
    print(f"\nONLY IN {b_name} (root paths of added/renamed subtrees): {len(only_b)} paths total")
    for p in collapse(only_b):
        print(f"  + {shorten(p)}")

    print("\nATTRIBUTE DIFFS on shared paths:")
    for p in sorted(set(a) & set(b)):
        if a[p] != b[p]:
            gone = a[p] - b[p]
            new = b[p] - a[p]
            bits = []
            if gone:
                bits.append(f"-{sorted(gone)}")
            if new:
                bits.append(f"+{sorted(new)}")
            print(f"  {shorten(p)}: {' '.join(bits)}")


def main() -> int:
    a_path, b_path = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    a_root, b_root = load_root(a_path), load_root(b_path)
    print(
        f"A = {a_path.name}: Creator={a_root.get('Creator')!r} rev={a_root.get('Revision')!r} "
        f"SchemaChangeCount={a_root.get('SchemaChangeCount')!r}"
    )
    print(
        f"B = {b_path.name}: Creator={b_root.get('Creator')!r} rev={b_root.get('Revision')!r} "
        f"SchemaChangeCount={b_root.get('SchemaChangeCount')!r}"
    )
    diff(a_path.stem, census(a_root), b_path.stem, census(b_root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
