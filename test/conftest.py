"""Fixtures shared across the suite.

The plugin folder scanners are chosen by OS, so a test that writes plugin files
into ``tmp_path`` and expects them found has to say which OS's files it wrote.
Without that it passes on the platform it was written on and finds nothing
anywhere else -- which is what the Linux CI runner found.
"""

from __future__ import annotations

import functools

import pytest

from abletoolz.plugin_parsers import plugin_db
from abletoolz.plugin_parsers.read_plugin_files import scan_plugin_dirs


@pytest.fixture
def scans_windows_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read folders as a Windows machine would, whatever machine is running.

    For tests whose plugin files are ``.dll`` modules with PE headers.
    """
    monkeypatch.setattr(plugin_db, "scan_plugin_dirs", functools.partial(scan_plugin_dirs, platform="win32"))
