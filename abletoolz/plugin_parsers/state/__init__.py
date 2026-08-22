"""What happens to a plugin's saved patch when its container changes.

The third axis of the plugin transform model -- container, identity, state --
and the only hard one. Container and identity are
finite knowledge -- a format pair either has a rewrite or it does not, a class id
either comes from an authoritative source or it does not. State is per plugin and
per vendor, and getting it wrong does not fail: the device loads and sounds
wrong.

This module is the seam and nothing else: the vocabulary a config entry writes,
the two registries a vendor module fills, and the lookups the translator makes.
No vendor's bytes are read here.

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

The named rungs beyond ``verbatim`` are the same idea one step earlier: the enum
member is the config surface, and the transform behind it is registered by
whichever vendor module knows the format. So :class:`StateTransform` can be read
here for what a config entry may say, and the answer to what those bytes become
is next to the vendor that measured it.

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
about it.

Where the rest of the axis lives
--------------------------------
The containers behind the seam are one module per *family* rather than one per
vendor, because a container is almost never a vendor's invention; the two vendor
modules hold only what really is theirs.

* :mod:`~abletoolz.plugin_parsers.state.measured` -- which rung each plugin sits
  on and how that was learned, which is what every run prints.
* :mod:`~abletoolz.plugin_parsers.state.families` -- what a buffer is, read off
  its own bytes, and the reframes those families share.
* :mod:`~abletoolz.plugin_parsers.state.fxbk` -- the standard VST2 bank a host
  writes for a plugin that exposes no chunk.
* :mod:`~abletoolz.plugin_parsers.state.derived` -- a transfer table the
  derivation rig measured, and the state it writes.
* :mod:`~abletoolz.plugin_parsers.state.fabfilter` -- what really is FabFilter's
  own: its chunk, its editor state, its trailers and the re-encodes between them.
* :mod:`~abletoolz.plugin_parsers.state.izotope` -- what really is iZotope's own:
  the refusal that stops a wrapper crossing as a patch.
"""

from __future__ import annotations

import dataclasses
import enum
from collections.abc import Callable
from typing import Protocol

from abletoolz.plugin_parsers.state.families import kilohearts_wrap
from abletoolz.plugin_parsers.state.measured import (
    MEASURED_STATE,
    UNMEASURED,
    MeasuredState,
    StateEvidence,
    StateRung,
    measured_state,
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

    The fourth state rung: nobody migrates the patch for you, so someone
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
# The Kilohearts reframe is the primitive itself: the wrap cannot fail, so there
# is nothing to say about a payload that it would refuse, and no vendor module
# would have anything to add. The other two named rungs are filled in by the
# vendor that measured them.

_BUILT_IN_TRANSFORMS: dict[StateTransform, StateBytes] = {
    StateTransform.KILOHEARTS: kilohearts_wrap,
}


def register_built_in_state(policy: StateTransform, transform: StateBytes) -> None:
    """Say what a named rung does to the bytes.

    Refused twice over for the same reason :func:`register_custom_state` is: two
    callables behind one config word would decide what a patch sounds like, and
    which of them won would not be inspectable afterwards.
    """
    existing = _BUILT_IN_TRANSFORMS.get(policy)
    if existing is not None and existing is not transform:
        raise StateTransformError(f"A different state transform is already registered as {policy!r}")
    _BUILT_IN_TRANSFORMS[policy] = transform


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
        raise StateTransformError(f"No controller state named {name!r} is registered; registered: {known}.")
    return controller


def registered_controller_states() -> frozenset[str]:
    """Every name a ``controller:`` entry may use on this run."""
    return frozenset(_CONTROLLER_STATES)


# -- the vendors, loaded last -----------------------------------------------


def _load_vendor_states() -> None:
    """Import the vendor modules, because importing one is what registers it.

    Called last on purpose, and the order is a constraint rather than a
    preference: every module named here reads the seam above -- the error type,
    the two registries, the controller shapes -- so none of them can be imported
    until this file has finished defining it.
    """
    from abletoolz.plugin_parsers.state import fabfilter, izotope  # noqa: F401


_load_vendor_states()

# What the rest of abletoolz reaches for. Everything else this module defines is
# still importable and still tested -- it is just not advertised, because a name
# nothing outside calls reads as a promise that was never asked for. The family
# toolbox in :mod:`~abletoolz.plugin_parsers.state.families` is the same: a
# measured knowledge base, imported from where it lives.
__all__ = [
    # What a mapping entry may say about the bytes
    "CustomState",
    "StatePolicy",
    "StateTransform",
    "StateTransformError",
    "parse_state",
    "register_built_in_state",
    "register_custom_state",
    "state_bytes",
    # What goes in the ControllerState beside it
    "NO_CONTROLLER_STATE",
    "ConstantControllerState",
    "ControllerState",
    "NoControllerState",
    "parse_controller_state",
    "register_controller_state",
    # What is known about a plugin's state
    "MEASURED_STATE",
    "UNMEASURED",
    "MeasuredState",
    "StateEvidence",
    "StateRung",
    "measured_state",
]
