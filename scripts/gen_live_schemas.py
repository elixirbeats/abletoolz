"""Pack the AbletonSchema resources Live ships into abletoolz/schema/data/live_schemas.json.gz.

Run from the abletoolz checkout::

    uv run python scripts/gen_live_schemas.py --schemas DIR

``DIR`` holds the resources as Live ships them, one ``<MinorVersion>.txt`` each
(Live 12 Suite 12.4.7 carries 203, from Live 8 to Live 12; about 62 MB). They
stay out of git: the packed file keeps what abletoolz reads from them, which
classes and members each schema declares and with what type, as one bitmask
per (class, member, type) over the schema list.

The output is deterministic, so a rerun on the same resources writes the same
bytes.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT = REPO_ROOT / "abletoolz" / "schema" / "data" / "live_schemas.json.gz"
SOURCE = "Ableton Live 12 Suite 12.4.7 (2026-10-16_cf213561fb)"
FORMAT = 1

_MINOR = re.compile(r"^(\d+)\.(\d+)_(\d+)$")


def _order(minor_version: str) -> tuple[int, int, int]:
    match = _MINOR.match(minor_version)
    if match is None:
        raise ValueError(f"not a MinorVersion: {minor_version}")
    major, minor, translator = (int(part) for part in match.groups())
    return major, minor, translator


def pack(schemas: Path, source: str = SOURCE) -> bytes:
    """The packed file's bytes for every ``*.txt`` resource in ``schemas``."""
    files = sorted(schemas.glob("*.txt"), key=lambda file: _order(file.stem))
    if not files:
        raise SystemExit(f"no schema resources in {schemas}")
    listed: list[dict[str, object]] = []
    classes: dict[str, int] = {}
    members: dict[str, dict[str, dict[str, int]]] = {}
    for bit, file in enumerate(files):
        text = file.read_bytes()
        root = ET.fromstring(text)
        count = root.get("ChangeCount")
        listed.append(
            {
                "minor_version": file.stem,
                "change_count": None if count is None else int(count),
                "sha256": hashlib.sha256(text).hexdigest(),
            }
        )
        for declared in root:
            classes[declared.tag] = classes.get(declared.tag, 0) | 1 << bit
            per_class = members.setdefault(declared.tag, {})
            for member in declared:
                types = per_class.setdefault(member.tag, {})
                kind = member.get("Class", "")
                types[kind] = types.get(kind, 0) | 1 << bit
    payload = {
        "format": FORMAT,
        "source": source,
        "schemas": listed,
        "classes": {name: f"{mask:x}" for name, mask in sorted(classes.items())},
        "members": {
            name: {
                member: {kind: f"{mask:x}" for kind, mask in sorted(types.items())}
                for member, types in sorted(per_class.items())
            }
            for name, per_class in sorted(members.items())
        },
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", compresslevel=9, mtime=0, filename="") as stream:
        stream.write(raw)
    return buffer.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--schemas", type=Path, required=True, help="folder of <MinorVersion>.txt resources")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    packed = pack(args.schemas)
    args.out.write_bytes(packed)
    print(f"{len(packed)} bytes -> {args.out}")


if __name__ == "__main__":
    main()
