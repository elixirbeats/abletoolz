"""Reaktor 6's ensemble path, in both forms its state's head stores it.

Reading it, relinking it and rewriting it back are in ``test_file_paths``; this is what is Reaktor's own.
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable

import pytest
from plugin_patches import ENSEMBLE, REAKTOR, REAKTOR_REST, REAKTOR_UTF16_COPY, file_paths, reaktor_plain, reaktor_url

from abletoolz.plugins.state.plugin_map import FileRef


@pytest.mark.parametrize("form", [reaktor_url, reaktor_plain], ids=["file URL", "plain path"])
def test_a_reaktor_rewrite_changes_only_the_head_s_path(form: Callable[[str], bytes]) -> None:
    """The fixed head before the path and the snapshot after it, its UTF-16 copy included, stay as they were."""
    state = form(ENSEMBLE)
    written = file_paths(REAKTOR).rewrite(state, FileRef(ENSEMBLE), pathlib.Path("E:\\My Ensembles\\Monark.ens"))
    assert written[:24] == state[:24]
    assert written.endswith(REAKTOR_REST + REAKTOR_UTF16_COPY)


def test_the_url_form_is_percent_encoded() -> None:
    written = file_paths(REAKTOR).rewrite(reaktor_url(ENSEMBLE), FileRef(ENSEMBLE), pathlib.Path("E:/A & B/x.ens"))
    assert b"file:///E:/A%20%26%20B/x.ens\x00" in written
