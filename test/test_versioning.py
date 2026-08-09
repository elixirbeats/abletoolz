"""Unit tests for version parsing and the @versioned dispatch descriptor."""

from __future__ import annotations

import pytest

from abletoolz.versioning import Version, parse_creator, versioned


@pytest.mark.parametrize(
    ("creator", "expected"),
    [
        ("Ableton Live 12.2.6", (12, 2, 6)),
        ("Ableton Live 11.0", (11, 0, 0)),
        ("Ableton Live 11.2.10b3", (11, 2, 10)),
        ("Ableton Live 9.7.5", (9, 7, 5)),
        ("Ableton Live 8.2.2", (8, 2, 2)),
    ],
)
def test_parse_creator(creator: str, expected: Version) -> None:
    assert parse_creator(creator) == expected


def test_parse_creator_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="Creator"):
        parse_creator("FL Studio 21")


class _Doc:
    def __init__(self, version: Version) -> None:
        self._version = version

    @property
    def version(self) -> Version:
        return self._version

    @versioned
    def sample_path(self) -> str:
        return "hex"

    @sample_path.since((11, 0, 0))
    def sample_path(self) -> str:
        return "string"

    @sample_path.since((12, 0, 0))
    def sample_path(self) -> str:
        return "string-12"


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ((9, 5, 0), "hex"),
        ((10, 1, 3), "hex"),
        ((11, 0, 0), "string"),  # floor is inclusive
        ((11, 3, 42), "string"),
        ((12, 0, 0), "string-12"),
        ((12, 4, 5), "string-12"),
    ],
)
def test_versioned_dispatches_newest_matching_floor(version: Version, expected: str) -> None:
    assert _Doc(version).sample_path() == expected


def test_versioned_class_access_returns_descriptor() -> None:
    assert isinstance(_Doc.sample_path, versioned)


def test_versioned_registration_order_does_not_matter() -> None:
    class Doc:
        def __init__(self, version: Version) -> None:
            self.version = version

        @versioned
        def tag(self) -> str:
            return "old"

        @tag.since((12, 0, 0))
        def tag(self) -> str:
            return "newest"

        @tag.since((10, 0, 0))
        def tag(self) -> str:
            return "middle"

    assert Doc((9, 0, 0)).tag() == "old"
    assert Doc((10, 5, 0)).tag() == "middle"
    assert Doc((12, 1, 0)).tag() == "newest"
