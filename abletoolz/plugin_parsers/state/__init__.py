"""What happens to a plugin's saved patch when its container changes.

The third axis of MODEL.md, and the only hard one. Container and identity are
finite knowledge -- a format pair either has a rewrite or it does not, a class id
either comes from an authoritative source or it does not. State is per plugin and
per vendor, and getting it wrong does not fail: the device loads and sounds
wrong.

Three things live here, and they answer different questions.

What to do with the bytes
-------------------------
:class:`StateTransform` and :class:`CustomState` are the *policy* a mapping entry
chooses, and there are only three shapes of answer:

* ``verbatim`` -- copy the blob untouched. The default, and what a config entry
  means when it says nothing about state.
* ``kilohearts`` -- the one measured reframe: same payload, new envelope.
* ``custom:<name>`` -- a per-plugin re-encode registered by name.
  :mod:`abletoolz.plugin_parsers.state.fabfilter` registers the first,
  ``custom:fabfilter-q1-to-q3``. Naming one nothing has registered is a loud
  error rather than a silent fall back to verbatim, because a plugin that needs
  re-encoding and gets the old bytes is exactly the failure this module exists
  to prevent.

Vendor-compat is deliberately not a transform. When a target plugin migrates the
old format's state itself, the bytes still go across untouched -- verbatim is the
policy and the vendor's declaration is *evidence*, which is the other half.

What goes in the ControllerState beside it
------------------------------------------
A VST3 preset holds two blobs, not one, and the second is a per-plugin fact
rather than a formality. :class:`ControllerState` and its three shapes --
:class:`NoControllerState`, :class:`ConstantControllerState`, and the
FFBS-generation form in :mod:`abletoolz.plugin_parsers.state.fabfilter` -- are what a
:class:`~abletoolz.plugin_parsers.format_translation.TranslationTarget` declares
about it. See MODEL.md, "The ControllerState beside it".

What is known about those bytes
-------------------------------
:data:`MEASURED_STATE` is the evidence table: for each plugin measured so far,
which rung it sits on and how that was learned. It is the same nine rows as the
table in ``MODEL.md``, and ``test_state`` cross-checks the two so the document
and the code cannot drift.

Evidence is what the predictability rule in MODEL.md turns on. A rung learned by
ear or from a vendor declaration is predictable; a rung inferred from the shape
of the bytes is a good guess awaiting a listen, and a plugin nobody has measured
is an experiment. Every suggestion and every repaired device is labelled with
which of the three it is, so a run says out loud what still needs the user's
ears.

Where the rest of the axis lives
--------------------------------
This module is the seam; the containers behind it are one module per *family*
rather than one per vendor, because a container is almost never a vendor's
invention:

* :mod:`~abletoolz.plugin_parsers.state.families` -- what a buffer is, read off
  its own bytes, and the reframes those families share.
* :mod:`~abletoolz.plugin_parsers.state.fxbk` -- the standard VST2 bank a host
  writes for a plugin that exposes no chunk.
* :mod:`~abletoolz.plugin_parsers.state.derived` -- a transfer table the
  derivation rig measured, and the state it writes.
* :mod:`~abletoolz.plugin_parsers.state.fabfilter` -- what really is FabFilter's
  own: its chunk, its editor state, and the re-encodes registered here.
* :mod:`~abletoolz.plugin_parsers.state.serato` -- a per-plugin parser for
  Serato Sample's JSON, reached through the parser registry rather than here.

Everything the rest of abletoolz needs is re-exported below, so nothing outside
this package has to know which of them a name came from.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
from collections.abc import Callable
from typing import Protocol

from abletoolz.plugin_parsers.state.families import (
    Family,
    detect,
    izotope_unwrap,
    izotope_wrap,
    juce_private_data_strip,
    kilohearts_unwrap,
    kilohearts_wrap,
    length_prefix_add,
    length_prefix_strip,
    vstw_chunk,
)

# One state blob in, the target format's state blob out.
type StateBytes = Callable[[bytes], bytes]

# What a config entry writes to reach a registered per-plugin transform.
CUSTOM_PREFIX = "custom:"


class StateTransformError(ValueError):
    """A ``state:`` naming a per-plugin transform nothing has registered."""


class StateTransform(enum.StrEnum):
    """The state policies built into abletoolz.

    ``VERBATIM`` copies the hex blob untouched. Sound-validated that way in Live
    12.4.5 for Serum (zlib chunk), Ghz Tupe 3, Prophet V3 and Serato Sample
    (JSON).

    ``KILOHEARTS`` was the first measured reframe: every kHs plugin (VST2 names
    start ``"kHs "``) wraps the same payload in an 8 byte header in VST3.

    ``FABFILTER`` and ``IZOTOPE`` are the two the host rig found on 2026-08-13,
    and both correct an entry that used to say verbatim. A VST2 chunk holds more
    than the VST3 processor state does, and the extra is different per vendor:
    FabFilter appends its editor's half, iZotope wraps the whole thing in a
    length and a preset name.
    """

    VERBATIM = "verbatim"
    KILOHEARTS = "kilohearts"
    FABFILTER = "fabfilter"
    IZOTOPE = "izotope"


@dataclasses.dataclass(frozen=True)
class CustomState:
    """A per-plugin re-encode, named in a config entry as ``custom:<name>``.

    The fourth rung of MODEL.md: nobody migrates the patch for you, so someone
    has to parse the source format and write the target's. That parser is
    registered with :func:`register_custom_state` and reached by name.
    """

    name: str

    def __str__(self) -> str:
        return f"{CUSTOM_PREFIX}{self.name}"


# What a mapping entry may say about state.
type StatePolicy = StateTransform | CustomState


# -- the registry -----------------------------------------------------------

_CUSTOM_TRANSFORMS: dict[str, StateBytes] = {}


def register_custom_state(name: str, transform: StateBytes) -> None:
    """Make ``custom:<name>`` available to config entries.

    Registering a name twice with two different callables is refused: whichever
    won would decide what a patch sounds like, and nothing about a silent winner
    is inspectable afterwards.
    """
    existing = _CUSTOM_TRANSFORMS.get(name)
    if existing is not None and existing is not transform:
        raise StateTransformError(f"A different custom state transform is already registered as {name!r}")
    _CUSTOM_TRANSFORMS[name] = transform


def custom_state(name: str) -> StateBytes:
    """The registered transform ``custom:<name>`` means."""
    transform = _CUSTOM_TRANSFORMS.get(name)
    if transform is None:
        known = ", ".join(sorted(_CUSTOM_TRANSFORMS)) or "nothing"
        raise StateTransformError(
            f"No custom state transform named {name!r} is registered ({CUSTOM_PREFIX}{name}); "
            f"registered: {known}. Register one before a mapping entry asks for it."
        )
    return transform


def registered_custom_states() -> frozenset[str]:
    """Every name a ``custom:`` entry may use on this run."""
    return frozenset(_CUSTOM_TRANSFORMS)


def parse_state(raw: str) -> StatePolicy:
    """Read a config entry's ``state:``, which is a rung name or ``custom:<name>``.

    Both failure modes raise: an unknown built-in name, and a ``custom:`` naming
    something unregistered. Neither can be quietly treated as verbatim.
    """
    if raw.startswith(CUSTOM_PREFIX):
        name = raw[len(CUSTOM_PREFIX) :]
        custom_state(name)
        return CustomState(name)
    return StateTransform(raw)


# -- applying it ------------------------------------------------------------


# The 4CC each FFBS-generation FabFilter product's editor state begins with, read
# off the plugins themselves on 2026-08-13. Inside a VST2 chunk the same magic is
# where the processor's half ends.
_FABFILTER_EDITOR_MAGIC = (b"FV3l", b"FQ3p", b"FQ4p", b"F3Ts", b"FS2a")

# What a FabFilter VST3 puts after its processor state and a VST2 chunk does not.
# Shared with :mod:`abletoolz.plugin_parsers.state.fabfilter`, which has to write it
# rather than move it: a re-encoded patch has no chunk to cut it off.
FABFILTER_PROCESSOR_TRAILER = b"FFpr\x01\x00\x00\x00\x00\x00\x00\x00"

# The same idea on the controller side: "FFed", a zero word and a float32 1.0.
# Measured across 200 sets -- every Pro-C 2, Pro-L 2, Pro-MB and Pro-R device
# (357 of them) writes exactly these twelve bytes as its whole ControllerState,
# with no variation at all, and every Pro-Q 3, Saturn 2 and Timeless 3 device
# ends its longer one with them. The two fields are carried, not understood.
FABFILTER_CONTROLLER_TRAILER = b"FFed\x00\x00\x00\x00\x00\x00\x80\x3f"


def _fabfilter_state(payload: bytes) -> bytes:
    """Cut an FFBS-generation FabFilter VST2 chunk down to its VST3 processor state.

    A VST2 chunk is the processor's state, then the editor's, and Ableton's
    ``ProcessorState`` is the first half plus a twelve byte trailer. Measured
    2026-08-13: a Volcano 3 VST2 buffer cut here and given the trailer is byte
    for byte a corpus Volcano 3 ``ProcessorState`` of the same patch, and the
    plugin hands those bytes straight back when asked.

    Copying the chunk whole is the trap this replaces. The processor half still
    reaches the DSP, so the device sounds right and passes a listen -- while the
    edit controller never sees the patch and every parameter reads as default.
    """
    for magic in _FABFILTER_EDITOR_MAGIC:
        found = payload.find(magic)
        if found > 0:
            return payload[:found] + FABFILTER_PROCESSOR_TRAILER
    known = ", ".join(magic.decode("ascii") for magic in _FABFILTER_EDITOR_MAGIC)
    raise StateTransformError(
        f"No FabFilter editor section in a {len(payload)} byte chunk (looked for {known}). "
        "The older FabF-generation products -- Pro-C 2, Pro-L 2, Pro-R, Pro-MB, Pro-DS, Pro-G, "
        "Micro and their 1.x predecessors -- write a different chunk that nothing here can convert yet."
    )


def _izotope_state(payload: bytes) -> bytes:
    """Unwrap an iZotope VST2 chunk down to the VST3 processor state inside it.

    The nesting itself is
    :func:`~abletoolz.plugin_parsers.state.families.izotope_unwrap`; what this
    adds is the refusal. A chunk that is not that shape cannot be passed through
    as verbatim, because the bytes that would go across include the length and
    the preset name and the target would read a patch nobody chose.
    """
    state = izotope_unwrap(payload)
    if state is None:
        raise StateTransformError(
            f"A {len(payload)} byte chunk is not an iZotope VST2 wrapper: its leading length and its "
            "trailing preset name do not account for it."
        )
    return state


# The Kilohearts reframe is the primitive itself: the wrap cannot fail, so there
# is nothing to say about a payload that it would refuse.
_BUILT_IN_TRANSFORMS: dict[StateTransform, StateBytes] = {
    StateTransform.KILOHEARTS: kilohearts_wrap,
    StateTransform.FABFILTER: _fabfilter_state,
    StateTransform.IZOTOPE: _izotope_state,
}


def state_bytes(policy: StatePolicy) -> StateBytes | None:
    """The byte rewrite ``policy`` asks for, or None when the bytes go across as they are."""
    if isinstance(policy, CustomState):
        return custom_state(policy.name)
    return _BUILT_IN_TRANSFORMS.get(policy)


# -- the ControllerState beside it ------------------------------------------
# Measured over 150 sets: a VST3 preset holds a ProcessorState and exactly one
# ControllerState, 917 pairs out of 917 in a second sample of 100. Whether the
# second one holds anything is a fact about the plugin and not about Live --
# soothe2, Oszillos Mega Scope, Rift, Phase Plant, SPAN Plus and Smack Attack
# write none, while Diva writes ~6.7 KB, Arturia Mini V3 ~15.9 KB, Serum 2
# ~2.4 KB and Chorus JUN-6 ~2.1 KB. Across the corpus it is populated 704 times
# and empty 377.
#
# So an empty one is right for some plugins and wrong for others, and which it
# is cannot be guessed from the processor state. Headless readback showed
# iZotope Trash 2 and u-he Diva reverting to their defaults when handed a
# processor state with no controller state beside it.


class ControllerState(Protocol):
    """What a translated device's ``ControllerState`` holds.

    A protocol rather than a closed union so that a vendor's own form can live
    with that vendor's parser -- the FFBS-generation FabFilter one is in
    :mod:`abletoolz.plugin_parsers.state.fabfilter`, which already knows the layout.
    """

    def build(self, source: bytes) -> bytes | None:
        """The bytes to write, given what the source format's device saved.

        None means the element is written and left empty, which is what Live
        does for a plugin that keeps no controller state of its own.
        """


@dataclasses.dataclass(frozen=True, slots=True)
class NoControllerState:
    """For the plugins that write none. The element is still there, and empty."""

    def build(self, source: bytes) -> bytes | None:
        return None


@dataclasses.dataclass(frozen=True, slots=True)
class ConstantControllerState:
    """One fixed blob whatever the patch is.

    What a plugin writes when its controller half carries no patch-dependent
    state at all -- the FabF-generation FabFilters and their twelve constant
    bytes are the measured case.
    """

    payload: bytes

    def build(self, source: bytes) -> bytes | None:
        return self.payload


# The default: say nothing, write the element empty. Correct for every plugin
# measured to write none, and the behaviour every target had before the shapes
# above existed.
NO_CONTROLLER_STATE = NoControllerState()

# What the FabF-generation products -- Pro-C 2, Pro-L 2, Pro-MB, Pro-R and their
# siblings -- write. Their VST2 exposes no chunk, so there is no editor half to
# carry and the trailer is the whole of it.
FABFILTER_CONSTANT_CONTROLLER = ConstantControllerState(FABFILTER_CONTROLLER_TRAILER)


# -- naming one from a config entry -----------------------------------------
# A shape is per plugin, so the same registry idea the state transforms use:
# a config entry names one and an unregistered name is a loud error rather than
# a quiet empty element. Kept apart from the state registry because the two
# answer different questions and a target declares them independently.

_CONTROLLER_STATES: dict[str, ControllerState] = {}


def register_controller_state(name: str, controller: ControllerState) -> None:
    """Make ``controller: <name>`` available to config entries."""
    existing = _CONTROLLER_STATES.get(name)
    if existing is not None and existing != controller:
        raise StateTransformError(f"A different controller state is already registered as {name!r}")
    _CONTROLLER_STATES[name] = controller


def parse_controller_state(name: str) -> ControllerState:
    """The registered controller state ``name`` means."""
    controller = _CONTROLLER_STATES.get(name)
    if controller is None:
        known = ", ".join(sorted(_CONTROLLER_STATES)) or "nothing"
        raise StateTransformError(
            f"No controller state named {name!r} is registered; registered: {known}."
        )
    return controller


def registered_controller_states() -> frozenset[str]:
    """Every name a ``controller:`` entry may use on this run."""
    return frozenset(_CONTROLLER_STATES)


# The one every FabF-generation target wants, and the first thing any config
# entry has ever named. The FFBS-generation shapes register themselves in
# :mod:`abletoolz.plugin_parsers.state.fabfilter`, where their layout lives.
FABFILTER_FABF_CONTROLLER = "fabfilter-fabf"

register_controller_state(FABFILTER_FABF_CONTROLLER, FABFILTER_CONSTANT_CONTROLLER)


# -- what is known about a plugin's state -----------------------------------


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


def _inferred(rung: StateRung) -> MeasuredState:
    """A rung the bytes imply and nobody has heard yet."""
    return MeasuredState(rung, (StateEvidence.STRUCTURAL,))


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
    "kHs Stereo": MeasuredState(
        StateRung.REFRAME, (StateEvidence.EAR,), datetime.date(2026, 8, 15)
    ),
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


# -- the containers, re-exported --------------------------------------------
# Last on purpose, and the order is a constraint rather than a preference: every
# module below reads the seam above -- the error type, the registry a re-encode
# registers itself in, the two FabFilter trailers -- so none of them can be
# imported until this file has finished defining it.

from abletoolz.plugin_parsers.state.derived import (  # noqa: E402
    DERIVED_TABLES,
    DerivedParameter,
    DerivedTable,
    FabfState,
    LinearTransfer,
    ParameterTransfer,
    StepTransfer,
    TableTransfer,
    read_derived_table,
)
from abletoolz.plugin_parsers.state.fabfilter import (  # noqa: E402
    PRO_C_1_DROPPED,
    PRO_C_1_ENUM_WIDENING,
    PRO_C_1_FILTER_SWITCHES,
    PRO_C_1_PARAMETERS,
    PRO_C_1_TO_PRO_C_2,
    PRO_C_1_TO_PRO_C_2_STATE,
    PRO_C_2,
    PRO_C_2_TABLE,
    PRO_Q1_TO_PRO_Q3,
    EditorState,
    FfbsControllerState,
    FfbsState,
    pro_c_1_to_pro_c_2,
    pro_c_1_to_pro_c_2_values,
    pro_q1_to_pro_q3,
    pro_q1_to_pro_q3_parameters,
)
from abletoolz.plugin_parsers.state.fxbk import LEGACY_NAME_BYTES, LegacyBank  # noqa: E402

__all__ = [
    # What a mapping entry may say about the bytes
    "CUSTOM_PREFIX",
    "CustomState",
    "StateBytes",
    "StatePolicy",
    "StateTransform",
    "StateTransformError",
    "custom_state",
    "parse_state",
    "register_custom_state",
    "registered_custom_states",
    "state_bytes",
    # What goes in the ControllerState beside it
    "FABFILTER_CONSTANT_CONTROLLER",
    "FABFILTER_CONTROLLER_TRAILER",
    "FABFILTER_FABF_CONTROLLER",
    "FABFILTER_PROCESSOR_TRAILER",
    "NO_CONTROLLER_STATE",
    "ConstantControllerState",
    "ControllerState",
    "NoControllerState",
    "parse_controller_state",
    "register_controller_state",
    "registered_controller_states",
    # What is known about a plugin's state
    "MEASURED_STATE",
    "UNMEASURED",
    "MeasuredState",
    "StateEvidence",
    "StateRung",
    "measured_state",
    # What a buffer is, and the reframes families share
    "Family",
    "detect",
    "izotope_unwrap",
    "izotope_wrap",
    "juce_private_data_strip",
    "kilohearts_unwrap",
    "kilohearts_wrap",
    "length_prefix_add",
    "length_prefix_strip",
    "vstw_chunk",
    # The containers behind it
    "DERIVED_TABLES",
    "LEGACY_NAME_BYTES",
    "PRO_C_1_DROPPED",
    "PRO_C_1_ENUM_WIDENING",
    "PRO_C_1_FILTER_SWITCHES",
    "PRO_C_1_PARAMETERS",
    "PRO_C_1_TO_PRO_C_2",
    "PRO_C_1_TO_PRO_C_2_STATE",
    "PRO_C_2",
    "PRO_C_2_TABLE",
    "PRO_Q1_TO_PRO_Q3",
    "DerivedParameter",
    "DerivedTable",
    "EditorState",
    "FabfState",
    "FfbsControllerState",
    "FfbsState",
    "LegacyBank",
    "LinearTransfer",
    "ParameterTransfer",
    "StepTransfer",
    "TableTransfer",
    "pro_c_1_to_pro_c_2",
    "pro_c_1_to_pro_c_2_values",
    "pro_q1_to_pro_q3",
    "pro_q1_to_pro_q3_parameters",
    "read_derived_table",
]
