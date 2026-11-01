"""Arturia's second-generation V instruments into their installed third-generation successors.

Piano V3 and Wurli V3 read their predecessors' patches themselves: the archive
of named numbers goes in as it is and the V3 migrates it on load. Hosted with
every distinct patch of each in the corpus, Piano V3 keeps the V2 value of all
but one or two of its 167 to 171 shared names (it re-sets the grand piano's mic
setup, the reverb level and the velocity curve's point count as its import
does), Wurli V3 keeps all 133, and the VST2 and VST3 of each render the
migrated patch alike. Neither V2 could be hosted here, so a render against the
original was not made.

Piano V2's VST2 chunk holds two archives, an older copy with fewer names and
no preset record, then the patch; Piano V3 plays the second whichever it is
given, and its VST3 plays only what its processor half holds. So the patch is
the second archive, written twice the way the V3s save theirs.
"""

from __future__ import annotations

import datetime
import functools

from abletoolz.formats.arturia_archive import TwoArchives
from abletoolz.plugins.converters import limitation
from abletoolz.plugins.state import arturia_v2
from abletoolz.plugins.state.arturia_v import holds_two_archives, with_values
from abletoolz.plugins.state.catalog import Converted, converter
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung

MEASURED = MeasuredState(StateRung.VENDOR_COMPAT, (StateEvidence.HOSTED,), datetime.date(2026, 10, 31))


def _patch_twice(patch: bytes, product: str) -> bytes:
    """The patch's second archive, twice behind its lengths."""
    if not holds_two_archives(patch):
        raise StateTransformError(f"a {product} patch is two Boost text archives, and this one is not")
    second = TwoArchives.split(patch)[1]
    return TwoArchives.join(second, second)


@converter("arturia.piano-v2", "arturia.piano-v3", MEASURED)
def piano_v2_to_v3(patch: bytes, program: int | None = None) -> Converted:
    """A Piano V2 chunk as a Piano V3 patch, for Piano V3 to migrate. ``program`` is not used."""
    return Converted(
        _patch_twice(patch, "Piano V2"),
        (
            "Piano V3 migrates the Piano V2 patch itself when it loads it, and may re-set the grand piano's mic "
            "setup, the reverb level and the velocity curve's point count as it does. Not heard in Live: audition "
            "this device.",
        ),
    )


@converter("arturia.wurli-v2", "arturia.wurli-v3", MEASURED)
def wurli_v2_to_v3(patch: bytes, program: int | None = None) -> Converted:
    """A Wurli V2 patch as a Wurli V3 patch, for Wurli V3 to migrate. ``program`` is not used."""
    return Converted(
        _patch_twice(patch, "Wurli V2"),
        ("Wurli V3 migrates the Wurli V2 patch itself when it loads it. Not heard in Live: audition this device.",),
    )


# -- ARP 2600 V2, Modular V2 and Prophet V2: the engine's own record -------------------------------
#
# These three save their engine's patch record as it is, and their V3s keep that record in their processors and
# store each of its words under a name (``state/arturia_v2.py``). A converted patch is the installed V3's factory
# Default, saved by the plugin, with every named word of the old record written over it in the V3's stored 0 to 1.
# What the V3 adds and the old record does not hold keeps the Default's setting.

FLOAT32_INTEGERS = 1 << 24


@functools.cache
def baseline(target: str) -> bytes:
    """The installed V3's factory Default, saved by the plugin, in the VST2 form."""
    return (arturia_v2.DATA / f"{target.removeprefix('arturia.')}-baseline.state").read_bytes()


def plan(chunk: bytes, instrument: arturia_v2.Instrument, notices: list[str] | None = None) -> dict[str, float]:
    """The V3's stored value for every named word of an old chunk.

    A word past what the V3 stores, or between two items of a choice, is refused without ``notices``, or written
    at the end of its range or as the nearest item and said.
    """
    out = {}
    controls = arturia_v2.controls(instrument)
    for name, word in arturia_v2.words(chunk, instrument).items():
        control = controls[name]
        # A word this large is no setting: an old record can hold a field its plugin never set (0xCDCDCDCE).
        if abs(word) >= FLOAT32_INTEGERS:
            limitation(notices, f"{name} holds {word:g}, which is no setting; left at its Default")
            continue
        value, exact = control.stored(word)
        if not exact:
            between = control.steps is not None and 0 <= round(word) + control.steps[1] < control.steps[0]
            why = (
                f"not a whole item; written as item {round(word)}"
                if between
                else f"past what {instrument.target_name} stores; written at its end"
            )
            limitation(notices, f"{name} holds {word:g}, {why}")
        out[name] = value
    return out


def _convert(instrument: arturia_v2.Instrument, chunk: bytes, program: int | None = None) -> Converted:
    """An old chunk as a V3 patch. ``program`` is not used: the chunk holds one patch."""
    notices = [
        f"Copied word by word into {instrument.target_name}'s factory Default under the names it stores, as "
        f"{instrument.target_name} keeps {instrument.name}'s patch record; read back by the installed plugin. Not "
        "heard in Live: audition this device."
    ]
    values = plan(chunk, instrument, notices)
    return Converted(with_values(baseline(instrument.target), values, instrument.target_name), tuple(notices))


RECORD = MeasuredState(StateRung.RE_ENCODE, (StateEvidence.HOSTED,), datetime.date(2026, 10, 31))
CONVERSIONS = {
    instrument.map_id: converter(instrument.map_id, instrument.target, RECORD)(functools.partial(_convert, instrument))
    for instrument in arturia_v2.INSTRUMENTS
}
