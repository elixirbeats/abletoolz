"""iZotope's VST2 wrapper, and the refusal that keeps it honest.

An iZotope VST2 chunk is the VST3 processor state with a length in front of it
and a preset name behind it. The nesting is not this vendor's invention, so
reading it is :func:`~abletoolz.plugin_parsers.state.families.izotope_unwrap`;
what is iZotope's own, and what lives here, is the policy a config entry names
and the error it raises for a chunk that turns out not to be that shape.
"""

from __future__ import annotations

from abletoolz.plugin_parsers.state import StateTransform, StateTransformError, register_built_in_state
from abletoolz.plugin_parsers.state.families import izotope_unwrap


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


register_built_in_state(StateTransform.IZOTOPE, _izotope_state)
