"""Surge 1.x patches into Surge XT, which migrates them itself."""

from __future__ import annotations

import datetime
import re

from abletoolz.plugins.state.catalog import Converted, converter
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.surge_synth_team import SURGE_1_MAGIC

_REVISION = re.compile(rb'revision="(\d+)"')
_REVISION_WINDOW = 512
_SURGE_1_REVISIONS = frozenset({10})


@converter(
    "surge-synth-team.surge-1",
    "surge-synth-team.surge-xt",
    MeasuredState(StateRung.VENDOR_COMPAT, (StateEvidence.HOSTED,), datetime.date(2026, 10, 22)),
)
def import_surge_1(chunk: bytes, program: int | None = None) -> Converted:
    """A Surge 1.x VST2 chunk as a Surge XT patch, for Surge XT to migrate.

    Surge XT imports Surge 1.x patches itself, so the chunk goes in untouched: a
    hand-built revision bump crashed the plugin. Measured 2026-10-22 on all 14
    distinct legacy patches in the user's sets (19 devices in 14 sets, all
    revision 10): the AU migrated each to revision 24, and after one more load
    matched the migration verified natively on Windows on every sound attribute
    and every wavetable byte but the voice limit, which resets from 8 to 16.
    """
    if not chunk.startswith(SURGE_1_MAGIC):
        raise StateTransformError(f"a Surge 1.x chunk opens with {SURGE_1_MAGIC!r}, and this one does not")
    found = _REVISION.search(chunk[:_REVISION_WINDOW])
    revision = None if found is None else int(found[1])
    if revision not in _SURGE_1_REVISIONS:
        raise StateTransformError(f"Surge XT was not measured importing Surge 1.x revision {revision}")
    return Converted(
        chunk,
        (
            "Surge XT migrates the patch to its current format when it loads it.",
            "The patch's voice limit becomes Surge XT's default of 16; the legacy patch saved 8.",
        ),
    )
