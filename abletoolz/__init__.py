"""Version control."""

from __future__ import annotations

from collections.abc import Callable

__version__ = "2.2.0"


def __getattr__(name: str) -> Callable[..., int]:
    # The console script is "abletoolz:main". Resolved on first use so that
    # importing any part of abletoolz does not import the whole CLI with it.
    if name == "main":
        from abletoolz.cli import main

        return main
    raise AttributeError(f"module 'abletoolz' has no attribute {name!r}")
