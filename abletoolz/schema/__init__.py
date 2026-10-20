"""The names in Live's XML, and how they change between Live versions.

``Tag`` and ``Attr`` (:mod:`abletoolz.schema.tags`) name the document's
structure. The settings of every object -- each native device
(:mod:`abletoolz.schema.devices`), tracks, mixers, clips and the transport
(:mod:`abletoolz.schema.objects`) -- are generated enums measured from real
sets, and :mod:`abletoolz.schema.settings` resolves a member against the
version of the set it is used in. Every rename Live made to a setting lives in
those generated tables; the full census of known renames is
doc/VERSION_DIFFS.md.
"""

from __future__ import annotations

from abletoolz.schema.tags import Attr, Tag, path

__all__ = ["Attr", "Tag", "path"]
