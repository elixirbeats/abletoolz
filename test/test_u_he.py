"""u-he Podolski crosses between its VST2 text and the VST3's byte count around it."""

from __future__ import annotations

import pathlib

import pytest

from abletoolz.plugins.identity.ids import PluginKind
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.plugin_map import FormatState

# What the installed VST3 saves for its default patch, processor and controller alike.
DEFAULT = (pathlib.Path(__file__).parent / "state_fixtures" / "u-he" / "podolski-default.vst3state").read_bytes()


def test_the_vst3_state_is_the_vst2_text_after_its_byte_count_and_the_controller_repeats_it() -> None:
    podolski = CATALOG.by_map_id("u-he.podolski")
    assert podolski is not None
    text = podolski.lift(FormatState(DEFAULT, DEFAULT), PluginKind.VST3)
    assert text.startswith(b"#pgm=initialize.h2p\n#AM=Podolski\n")

    assert podolski.lower(text, PluginKind.VST3) == FormatState(DEFAULT, DEFAULT)
    assert podolski.cross(FormatState(text, None), PluginKind.VST, PluginKind.VST3) == FormatState(DEFAULT, DEFAULT)


def test_a_state_whose_count_does_not_fit_is_refused() -> None:
    podolski = CATALOG.by_map_id("u-he.podolski")
    assert podolski is not None
    with pytest.raises(StateTransformError):
        podolski.lift(FormatState(DEFAULT[:-1], None), PluginKind.VST3)
