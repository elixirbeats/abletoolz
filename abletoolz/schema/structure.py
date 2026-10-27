"""Live's document structure per Live release, measured from real sets.

The tables come from ``data/structure.json``, which ``scripts/gen_vocab.py``
writes from a corpus of sets (``uv run python scripts/gen_vocab.py --corpus DIR``).

A set's schema is the pair of numbers on its root element, ``MinorVersion`` and
``SchemaChangeCount``; several releases share one. These tables say which
elements and attributes each release writes, so a set can be written in an
older release's schema (:mod:`abletoolz.live_set.save_as`). Tags are counted
the way the grammar folds them: a numbered tag (``ControllerTargets.12``) is
``ControllerTargets.{n}``.

* ``RELEASES``: the root attributes each Live release wrote, and ``SETS`` how
  many sets of the corpus it wrote.
* ``NEWER_CHILDREN`` / ``NEWER_ATTRIBUTES``: a child or attribute first seen
  after its parent was, with the release it first appeared in. Anything not
  listed is as old as its parent.
* ``REQUIRED_ATTRIBUTES`` / ``REQUIRED_CHILDREN``: per schema, the ones on every
  element of a tag where another schema leaves them off. ``FILL_VALUES`` holds
  each required attribute's most common value and ``ATTRIBUTE_ORDER`` the order
  the schema writes that tag's attributes in.
* ``DEVICE_KINDS``: whether each device is an audio effect, a MIDI effect or an
  instrument, from the chains it was found in.
* ``TAG_RENAMES``: tags Live renamed, by old spelling, with the release that
  writes the new one, and ``RENAMED_HELD`` what each old spelling held.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from abletoolz.schema.versioning import Version

type Schema = tuple[str, int | None]
"""``MinorVersion`` and ``SchemaChangeCount``."""

# gen_vocab writes exactly this shape; the tables below are typed where they are built.
_DATA: dict[str, Any] = json.loads((Path(__file__).with_name("data") / "structure.json").read_text(encoding="utf-8"))


def _version(raw: list[int]) -> Version:
    return raw[0], raw[1], raw[2]


def _versions(table: dict[str, dict[str, list[int]]]) -> dict[str, dict[str, Version]]:
    return {tag: {name: _version(first) for name, first in names.items()} for tag, names in table.items()}


def _per_schema(key: str) -> dict[Schema, dict[str, tuple[str, ...]]]:
    return {
        (schema["minor_version"], schema["schema_change_count"]): {tag: tuple(names) for tag, names in table.items()}
        for schema in _DATA["schemas"]
        if (table := schema[key])
    }


RELEASES: dict[Version, tuple[tuple[str, str], ...]] = {
    _version(row["release"]): tuple((name, value) for name, value in row["root"]) for row in _DATA["releases"]
}
"""The root attributes each release wrote, in its order."""

SETS: dict[Version, int] = {_version(row["release"]): row["sets"] for row in _DATA["releases"]}
"""How many sets of the corpus each release wrote."""

NEWER_CHILDREN = _versions(_DATA["newer_children"])
"""Parent tag -> child -> the release it first appeared in, where that is after the parent's first."""

NEWER_ATTRIBUTES = _versions(_DATA["newer_attributes"])
"""Parent tag -> attribute -> the release it first appeared in, where that is after the parent's first."""

REQUIRED_ATTRIBUTES = _per_schema("required_attributes")
"""Per schema: tag -> the attributes on every one of its elements, where another schema leaves them off."""

REQUIRED_CHILDREN = _per_schema("required_children")
"""Per schema: tag -> the children under every one of its elements, where another schema leaves them off."""

FILL_VALUES: dict[str, dict[str, str]] = _DATA["fill_values"]
"""Tag -> required attribute -> the value the corpus holds there most often."""

ATTRIBUTE_ORDER = _per_schema("attribute_order")
"""Per schema: tag -> the attribute order it writes, for the tags with required attributes."""

DEVICE_KINDS: dict[str, str] = _DATA["device_kinds"]
"""Device tag -> ``audio_effect``, ``midi_effect`` or ``instrument``, by majority over every one found."""

RENAMED_HELD: dict[str, tuple[dict[str, Version], dict[str, Version]]] = {
    old: (
        {name: _version(first) for name, first in held["children"].items()},
        {name: _version(first) for name, first in held["attributes"].items()},
    )
    for old, held in _DATA["renamed_held"].items()
}
"""Old tag -> the children and the attributes it held, each with the release it first appeared in."""

TAG_RENAMES: dict[str, tuple[str, Version]] = {
    old: (rename["new"], _version(rename["since"])) for old, rename in _DATA["tag_renames"].items()
}
"""Old tag -> the new tag and the first release that writes it."""
