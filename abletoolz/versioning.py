"""Version parsing and version-aware method dispatch.

Ableton behaviors are introduced at some version and rarely end cleanly, so
everything here works on minimum-version floors: the newest implementation
whose floor the set's version meets wins. String-level differences (tag
names) live in ``schema.py``; structural/logic differences use ``@versioned``
method overrides. Same newest-floor-wins rule in both.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Concatenate, Protocol, cast

type Version = tuple[int, int, int]

# Pre-8.2 sets are a different container format (magic bytes 0xab1e) and are
# rejected at parse; this is the floor the whole package assumes.
MIN_SUPPORTED: Version = (8, 2, 0)

_CREATOR_RE = re.compile(r"Ableton Live (\d{1,2})\.(\d{1,3})(?:[.b](\d{1,3}))?")


def parse_creator(creator: str) -> Version:
    """Parse the root element's ``Creator`` attribute into a version tuple.

    Two-part versions get patch 0 ("Ableton Live 11.0" -> (11, 0, 0));
    beta markers are skipped over ("... 11.2.10b3" -> (11, 2, 10)).
    """
    match = _CREATOR_RE.search(creator)
    if not match:
        raise ValueError(f"Cannot parse Live version from Creator: {creator!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)


class HasVersion(Protocol):
    """Anything owning versioned methods exposes the set's version."""

    @property
    def version(self) -> Version: ...


class versioned[**P, R]:  # noqa: N801 - decorator, reads like a keyword at use sites
    """Descriptor dispatching to the newest implementation the version meets.

    The undecorated body is the floor implementation; overrides register with
    ``@name.since((11, 0, 0))``. ``since`` returns the descriptor, so
    redefining the same method name keeps binding to it::

        @versioned
        def set_relative(self, path: str) -> None: ...      # pre-11 form

        @set_relative.since((11, 0, 0))
        def set_relative(self, path: str) -> None: ...      # string form
    """

    def __init__(self, base: Callable[Concatenate[Any, P], R]) -> None:
        self._impls: list[tuple[Version, Callable[Concatenate[Any, P], R]]] = [((0, 0, 0), base)]
        self.__doc__ = base.__doc__

    def since(
        self, floor: Version
    ) -> Callable[[Callable[Concatenate[Any, P], R]], versioned[P, R]]:
        def register(fn: Callable[Concatenate[Any, P], R]) -> versioned[P, R]:
            self._impls.append((floor, fn))
            self._impls.sort(key=lambda entry: entry[0], reverse=True)
            return self

        return register

    def __get__(self, obj: HasVersion | None, objtype: type | None = None) -> Callable[P, R]:
        if obj is None:
            return self  # type: ignore[return-value]  # class access: the descriptor itself
        for floor, fn in self._impls:
            if obj.version >= floor:
                # function.__get__ isn't part of the Callable protocol, so its bound-method
                # result types as Any; the descriptor contract guarantees it matches R.
                return cast(Callable[P, R], fn.__get__(obj, objtype))
        return cast(Callable[P, R], self._impls[-1][1].__get__(obj, objtype))
