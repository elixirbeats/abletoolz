"""Subsonic Labs Wolfram, read from what its devices saved.

As a VST3, its ProcessorState: u32 1 when slot B is active, the u32 sizes of
slot B's and slot A's caches, then both caches, each a ``.sub`` preset stream.
Its VST2 chunk is an i64 size of that state, an i64 controller size (0), then
the state.
"""

from __future__ import annotations

import datetime
import struct

from abletoolz.plugins.identity.ids import Vst2Id, Vst3Id
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.framings import VERBATIM
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import Layout, PluginFormat, PluginMap

# The format is only read here, by the shape of its bytes.
_STRUCTURAL = MeasuredState(StateRung.VERBATIM, (StateEvidence.STRUCTURAL,), datetime.date(2026, 10, 31))

_VST2_HEADER = struct.Struct("<qq")
_SLOTS = struct.Struct("<3I")


def is_wolfram_state(state: bytes) -> bool:
    """Whether ``state`` is Wolfram's ProcessorState: the slot flag, both cache sizes, both caches."""
    if len(state) < _SLOTS.size:
        return False
    slot_b, size_b, size_a = _SLOTS.unpack_from(state)
    return bool(slot_b <= 1 and _SLOTS.size + size_a + size_b == len(state))


class _WolframVst2:
    """Wolfram's VST2 chunk: the ProcessorState's size, a controller size of 0, then the ProcessorState."""

    def lift(self, state: bytes) -> bytes:
        if len(state) < _VST2_HEADER.size:
            raise StateTransformError("A Wolfram VST2 chunk opens with two 8-byte sizes")
        component, controller = _VST2_HEADER.unpack_from(state)
        if controller != 0 or component != len(state) - _VST2_HEADER.size:
            raise StateTransformError("This Wolfram VST2 chunk's sizes do not describe it")
        return state[_VST2_HEADER.size :]

    def lower(self, patch: bytes) -> bytes:
        raise StateTransformError("Writing Wolfram's own VST2 is not measured; it does not load in Live")


@register
def wolfram() -> PluginMap:
    # The VST3's controller half is not measured, so the original VST3 is read and never written.
    return PluginMap(
        id="subsonic-labs.wolfram",
        name="Wolfram",
        version=None,
        scope="VST3 ProcessorState and the VST2 chunk around it, by their structure",
        vendor="Subsonic Labs",
        vst2=PluginFormat(Vst2Id(0x576F6C66), "Wolfram", _WolframVst2(), _STRUCTURAL, plan_name="wolfram_vst2"),
        vst3=PluginFormat(
            Vst3Id.of("11111111-92464F53-93FBE413-1C945A19"), "Wolfram", VERBATIM, _STRUCTURAL, plan_name="wolfram_vst3"
        ),
        layouts=(Layout("ProcessorState of two slot caches", is_wolfram_state),),
    )
