"""What is known about each plugin's state, and how it came to be known.

The evidence half of the state axis, and the half a run has to print. The seam
next door decides what to do with a patch's bytes; this decides how much anyone
should trust the answer.

:data:`MEASURED_STATE` is the table: for each plugin measured so far, which rung
it sits on and how that was learned. It is the same rows as the table in
``MODEL.md``, and ``test_state`` cross-checks the two so the document and the
code cannot drift.

Evidence is what the predictability rule in MODEL.md turns on. A rung learned by
ear or from a vendor declaration is predictable; a rung inferred from the shape
of the bytes is a good guess awaiting a listen, and a plugin nobody has measured
is an experiment. Every suggestion and every repaired device is labelled with
which of the three it is, so a run says out loud what still needs the user's
ears.

Nothing here reads a byte, which is why it depends on nothing else in the
package: a measurement is a record of what a human or a rig found, not a parser.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum


class StateRung(enum.StrEnum):
    """Where a plugin's state sits on MODEL.md's ladder, cheapest first."""

    VERBATIM = "verbatim"
    REFRAME = "reframe"
    VENDOR_COMPAT = "vendor-compat"
    RE_ENCODE = "re-encode"
    UNKNOWN = "unknown"


class StateEvidence(enum.StrEnum):
    """How a rung was learned, and therefore how much it is worth.

    ``EAR`` is a human who loaded the converted device in Live and listened.
    ``DECLARED`` is the vendor saying so in ``moduleinfo.json``. Those two are
    what MODEL.md's predictability rule accepts.

    ``HOSTED`` is the plugin itself answering, outside Live: the host rig loads
    the target VST3, pushes a real patch of the source version into it and reads
    back its state, its parameters and two renders. That is far more than the
    bytes looking right -- a rejection there is the plugin refusing the patch,
    and it caught four version migrations this project was about to ship as
    working. It is still not a listen, because a plugin can take a patch and
    sound wrong, so it does not make a conversion predictable.

    ``STRUCTURAL`` is the bytes looking right, which is where a listen starts
    rather than ends, and ``UNKNOWN`` is a plugin nobody has measured at all.
    """

    EAR = "ear"
    DECLARED = "declared"
    HOSTED = "hosted"
    STRUCTURAL = "structural"
    UNKNOWN = "unknown"


# Evidence that settles the question, per MODEL.md's predictability rule.
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
        """The label a suggestion or a repaired device carries."""
        if self.rung is StateRung.UNKNOWN:
            return "state: unknown -- experiment, audition before trusting"
        evidence = "+".join(str(item) for item in self.evidence)
        when = "" if self.date is None else f" {self.date.isoformat()}"
        return f"state: {self.rung} ({evidence}{when})"


# A plugin no measurement covers. Not an absence -- a statement that a conversion
# of it is an experiment, which is the thing worth printing.
UNMEASURED = MeasuredState(StateRung.UNKNOWN, (StateEvidence.UNKNOWN,))

_MEASURED_ON = datetime.date(2026, 8, 10)
_HOSTED_ON = datetime.date(2026, 8, 13)
_RE_ENCODED_ON = datetime.date(2026, 8, 15)


def _heard(rung: StateRung, *also: StateEvidence) -> MeasuredState:
    """A rung a human loaded in Live and listened to, on the day they did."""
    return MeasuredState(rung, (StateEvidence.EAR, *also), _MEASURED_ON)


def _hosted(rung: StateRung, *also: StateEvidence) -> MeasuredState:
    """A rung the target plugin itself answered for, in the host rig."""
    return MeasuredState(rung, (*also, StateEvidence.HOSTED), _HOSTED_ON)


def _re_encoded(rung: StateRung, *also: StateEvidence) -> MeasuredState:
    """A rung whose re-encode the target plugin read back, on the day it did."""
    return MeasuredState(rung, (*also, StateEvidence.HOSTED), _RE_ENCODED_ON)


# Every plugin whose state is measured, keyed by a name a set stores -- the VST2
# name where the two formats spell it differently ("FabFilter Pro-Q 3"), the
# shared name where they do not. Kept in step with MODEL.md's table by
# test_state, because a table only in prose is a table that goes stale.
#
# A row carries one date, which is the day its rung was last measured; where two
# kinds of evidence disagree in age, MODEL.md's prose says which came when.
MEASURED_STATE: dict[str, MeasuredState] = {
    "Serum": _heard(StateRung.VERBATIM, StateEvidence.DECLARED),
    "Ghz Tupe 3": _heard(StateRung.VERBATIM),
    "Prophet V3": _heard(StateRung.VERBATIM),
    "Serato Sample": _heard(StateRung.VERBATIM),
    # FFBS-generation FabFilter: the VST2 chunk carries the editor's half too,
    # and the VST3 wants a trailer the chunk has not got. Copying it whole
    # reaches the DSP and nothing else, which is why the listen passed.
    "FabFilter Volcano 3": _hosted(StateRung.REFRAME),
    "FabFilter Pro-Q 3": _hosted(StateRung.REFRAME),
    "FabFilter Saturn 2": _hosted(StateRung.REFRAME),
    "FabFilter Timeless 3": _hosted(StateRung.REFRAME, StateEvidence.EAR),
    # FabF-generation FabFilter: a bare parameter array with a header. The target
    # takes any correctly headed blob and reads it positionally, so a converted
    # chunk lands on a patch nobody chose. Nothing here can convert it yet.
    "FabFilter Pro-C 2": _hosted(StateRung.RE_ENCODE),
    "FabFilter Pro-L 2": _hosted(StateRung.RE_ENCODE),
    "FabFilter Pro-R": _hosted(StateRung.RE_ENCODE),
    # Version migrations the target refused outright.
    "FabFilter Volcano 2": _hosted(StateRung.RE_ENCODE),
    # Pro-Q 1 is the one with a re-encode written for it. Both spellings are the
    # same plugin -- ".64" is a jBridged 32-bit build, which Live 12 will not
    # load at all, so converting it is the only way its patch comes back.
    "FabFilter Pro-Q": _hosted(StateRung.RE_ENCODE, StateEvidence.EAR),
    "FabFilter Pro-Q.64": _hosted(StateRung.RE_ENCODE, StateEvidence.EAR),
    "FabFilter Pro-Q 2 x64": _hosted(StateRung.RE_ENCODE),
    "FabFilter Timeless 2": _hosted(StateRung.RE_ENCODE),
    "FabFilter Saturn": _hosted(StateRung.RE_ENCODE),
    # Pro-C 1 is the second product with a re-encode written for it, and the
    # first whose source side came out of Live's own records rather than out of
    # the plugin. Both spellings again: the ".64" is the jBridged 32-bit build
    # every one of the user's newer sets carries.
    "FabFilter Pro-C": _re_encoded(StateRung.RE_ENCODE),
    "FabFilter Pro-C.64": _re_encoded(StateRung.RE_ENCODE),
    "iZotope Ozone 4": _hosted(StateRung.RE_ENCODE),
    "Ozone 8 Elements": _hosted(StateRung.RE_ENCODE),
    "Ozone 9 Exciter": _hosted(StateRung.REFRAME),
    "kHs Distortion": _heard(StateRung.REFRAME),
    "kHs Filter": _heard(StateRung.REFRAME),
    # Probed from the binary on 2026-08-13; the user heard both devices carry
    # their old settings on 2026-08-15, which is what closed the recovery set.
    "kHs Stereo": MeasuredState(StateRung.REFRAME, (StateEvidence.EAR,), datetime.date(2026, 8, 15)),
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
