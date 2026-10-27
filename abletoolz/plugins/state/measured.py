"""What is known about each plugin's state, and how it came to be known.

The evidence half of the state axis, and the half a run has to print. The seam
next door decides what to do with a patch's bytes; this decides how much anyone
should trust the answer.

:class:`MeasuredState` is one record: which rung a crossing sits on and how
that was learned. Each plugin map carries one per plugin format, for crossing
between that plugin's own formats. :data:`MEASURED_STATE` holds the other kind:
migrations from one plugin to a newer one, keyed by the source plugin's name.

Evidence is what the predictability rule turns on. A rung learned by
ear or from a vendor declaration is predictable; a rung inferred from the shape
of the bytes is a good guess awaiting a listen, and a plugin nobody has measured
is an experiment. Every plan target is labelled with which of the three it
is, so a plan says out loud what still needs the user's ears.

Nothing here reads a byte, which is why it depends on nothing else in the
package: a measurement is a record of what a human or a rig found, not a parser.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum


class StateRung(enum.StrEnum):
    """Where a plugin's state sits on the state ladder, cheapest first."""

    VERBATIM = "verbatim"
    REFRAME = "reframe"
    VENDOR_COMPAT = "vendor-compat"
    RE_ENCODE = "re-encode"
    UNKNOWN = "unknown"


class StateEvidence(enum.StrEnum):
    """How a rung was learned, and therefore how much it is worth.

    ``EAR`` is a human who loaded the converted device in Live and listened.
    ``DECLARED`` is the vendor saying so in ``moduleinfo.json``. Those two are
    the only two the predictability rule accepts.

    ``HOSTED`` is the plugin itself answering, outside Live: the host rig loads
    the target VST3, pushes a real patch of the source version into it and reads
    back its state, its parameters and two renders. That is far more than the
    bytes looking right -- a rejection there is the plugin refusing the patch.
    It is still not a listen, because a plugin can take a patch and sound
    wrong, so it does not make a conversion predictable.

    ``STRUCTURAL`` is the bytes looking right, which is where a listen starts
    rather than ends, and ``UNKNOWN`` is a plugin nobody has measured at all.
    """

    EAR = "ear"
    DECLARED = "declared"
    HOSTED = "hosted"
    STRUCTURAL = "structural"
    UNKNOWN = "unknown"


# Evidence that settles the question. Anything else leaves a conversion an
# experiment, however convincing the bytes look.
_PREDICTIVE = frozenset({StateEvidence.EAR, StateEvidence.DECLARED})


@dataclasses.dataclass(frozen=True)
class MeasuredState:
    """What is known about one plugin's state, and how it came to be known."""

    rung: StateRung
    evidence: tuple[StateEvidence, ...]
    date: datetime.date | None = None

    @property
    def predictable(self) -> bool:
        """Whether a conversion of this plugin is measured rather than an experiment."""
        return bool(_PREDICTIVE.intersection(self.evidence))

    @property
    def annotation(self) -> str:
        """The label a plan target carries for this evidence."""
        if self.rung is StateRung.UNKNOWN:
            return "state: unknown -- experiment, audition before trusting"
        evidence = "+".join(str(item) for item in self.evidence)
        when = "" if self.date is None else f" {self.date.isoformat()}"
        return f"state: {self.rung} ({evidence}{when})"


# A plugin no measurement covers. Not an absence -- a statement that a conversion
# of it is an experiment, which is the thing worth printing.
UNMEASURED = MeasuredState(StateRung.UNKNOWN, (StateEvidence.UNKNOWN,))

_HOSTED_ON = datetime.date(2026, 8, 13)
_RE_ENCODED_ON = datetime.date(2026, 8, 15)


def _hosted(rung: StateRung) -> MeasuredState:
    """A rung the target plugin itself answered for, in the host rig."""
    return MeasuredState(rung, (StateEvidence.HOSTED,), _HOSTED_ON)


def _re_encoded(rung: StateRung) -> MeasuredState:
    """A rung whose re-encode the target plugin read back, on the day it did."""
    return MeasuredState(rung, (StateEvidence.HOSTED,), _RE_ENCODED_ON)


# Each key is the source plugin, a different plugin from the newer one it would
# become. The host rig asked the newer plugin to read a real patch of the older
# on 2026-08-13 and it refused, so crossing needs a re-encode written per pair.
MEASURED_STATE: dict[str, MeasuredState] = {
    "FabFilter Volcano 2": _hosted(StateRung.RE_ENCODE),
    "FabFilter Timeless 2": _hosted(StateRung.RE_ENCODE),
    "FabFilter Saturn": _hosted(StateRung.RE_ENCODE),
    "FabFilter Pro-C": _re_encoded(StateRung.RE_ENCODE),
    "FabFilter Pro-C.64": _re_encoded(StateRung.RE_ENCODE),
    "iZotope Ozone 4": _hosted(StateRung.RE_ENCODE),
    "Ozone 8 Elements": _hosted(StateRung.RE_ENCODE),
}


def measured_state(*names: str | None) -> MeasuredState:
    """What is known about the plugin any of ``names`` refers to.

    A measurement is about a plugin, not about a spelling, so both ends of a
    mapping are asked: "Serum_x64" is not in the table and the "Serum" it becomes
    is. Nothing here guesses -- a name no row matches answers
    :data:`UNMEASURED`, which prints as an experiment.
    """
    for name in names:
        if name is None:
            continue
        found = MEASURED_STATE.get(name)
        if found is not None:
            return found
    return UNMEASURED
