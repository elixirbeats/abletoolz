"""CMI V's voice samples: the fixed 256 byte path field in each voice's sample object, in both sections.

Which samples it names, relinking one, the field's width and rewriting it back are in ``test_file_paths``;
this is what is CMI V's own.
"""

from __future__ import annotations

import pathlib

from plugin_patches import CMI_V, FACTORY, LOOP, cmi_v, file_paths

from abletoolz.plugins.state.plugin_map import FileRef


def test_a_cmi_v_rewrite_fills_every_copy_and_leaves_the_factory_voices() -> None:
    """Two voices play the sample, and each section holds both: four copies to fill."""
    moved = pathlib.Path("E:\\Moved\\bass loop.wav")
    written = file_paths(CMI_V).rewrite(cmi_v(LOOP, FACTORY, LOOP), FileRef(LOOP), moved)
    assert written.count(str(moved).encode()) == 4
    assert LOOP.encode() not in written
    assert written.count(FACTORY.encode()) == 2
