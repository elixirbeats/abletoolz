"""Absynth 3 patches into Absynth 5, which migrates them itself."""

from __future__ import annotations

import datetime

from abletoolz.plugins.state.catalog import Converted, converter
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.native_instruments import ABSYNTH_3_MAGIC, is_absynth_3_patch


@converter(
    "native-instruments.absynth-3",
    "native-instruments.absynth-5",
    MeasuredState(StateRung.VENDOR_COMPAT, (StateEvidence.HOSTED,), datetime.date(2026, 10, 31)),
)
def import_absynth_3(chunk: bytes, program: int | None = None) -> Converted:
    """An Absynth 3 VST2 chunk as an Absynth 5 patch, for Absynth 5 to migrate.

    Absynth 5 reads an Absynth 3 chunk handed to it as its own bank chunk and
    saves the sound back in its own NI item container, under the Absynth 3 patch
    name, with the sound's macro names on its automation parameters. A sample the
    patch names under the Absynth 3 install folder is found again in Absynth 5's
    library ("23 - Preset Samples/Absynth 3") when that library is installed.

    Measured headless on the Absynth 5.3.4 VST2: a migrated factory sound renders
    sample for sample like Absynth 5's own conversion of it in its "Absynth
    Legacy" library, and the migration saved and loaded again renders the same.
    A sound edited in Absynth 3 has no factory conversion to be checked against.
    """
    if not is_absynth_3_patch(chunk):
        raise StateTransformError(
            f"an Absynth 3 chunk opens with {ABSYNTH_3_MAGIC!r} and a patch block, and this one does not"
        )
    return Converted(
        chunk,
        (
            "Absynth 5 migrates the Absynth 3 sound to its own format when it loads it.",
            "Absynth 5 finds the samples the sound uses in its own library, so that library has to be installed.",
        ),
    )
