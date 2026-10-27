"""What goes in a rewritten VST3 device's ``ControllerState``, the preset half beside the patch.

A plugin map says what goes there. That is about what the container writes,
so it lives with it; the vendor forms of a controller half live with their
vendor.
"""

from __future__ import annotations

import dataclasses
from typing import Protocol

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
    """What a rewritten device's ``ControllerState`` holds.

    A protocol rather than a closed union so that a vendor's own form can live
    with that vendor's parser -- the FFBS-generation FabFilter one is in
    :mod:`abletoolz.plugins.state.fabfilter`, which already knows the layout.
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
# measured to write none.
NO_CONTROLLER_STATE = NoControllerState()
