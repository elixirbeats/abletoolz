"""Asking an installed VST2 for its record once, with the child process faked: no plugin is ever loaded."""

from __future__ import annotations

import json
import pathlib
import subprocess
from typing import Any

import pytest

from abletoolz.plugins.identity.vst2_records import live_flags, record_source

UNIQUE_ID = 1179668850
ANSWER = {
    "UniqueId": UNIQUE_ID,
    "NumberOfParameters": 12,
    "NumberOfPrograms": 1,
    "Flags": 0x31,
    "Version": 1300,
    "VstVersion": 2400,
    "Category": 1,
}


class Child:
    """A stand-in for ``subprocess.run`` that counts launches and answers or fails as told."""

    def __init__(self, *, stdout: str = json.dumps(ANSWER), returncode: int = 0, hang: bool = False) -> None:
        self.stdout, self.returncode, self.hang = stdout, returncode, hang
        self.launches = 0

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.launches += 1
        if self.hang:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, self.returncode, self.stdout, "Traceback\nOSError: no entry point")


@pytest.fixture
def plugin(tmp_path: pathlib.Path) -> pathlib.Path:
    module = tmp_path / "Plugin x64.dll"
    module.write_bytes(b"MZ")
    return module


def ask(child: Child, plugin: pathlib.Path, monkeypatch: pytest.MonkeyPatch, unique_id: int = UNIQUE_ID) -> Any:
    monkeypatch.setattr(subprocess, "run", child)
    return record_source([(unique_id, plugin)], plugin.parent / "config" / "vst2_records.json")(unique_id)


def test_a_module_is_asked_once_and_then_read_from_the_cache(
    plugin: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = Child()
    record = ask(first, plugin, monkeypatch)
    assert record is not None and (record.unique_id, record.parameters, record.flags, record.chunks) == (
        UNIQUE_ID,
        12,
        live_flags(0x31),
        True,
    )
    again = Child()
    assert ask(again, plugin, monkeypatch) == record
    assert (first.launches, again.launches) == (1, 0)


@pytest.mark.parametrize(
    "child",
    [Child(hang=True), Child(returncode=1), Child(stdout=""), Child(stdout="[]")],
    ids=["timeout", "nonzero-exit", "no-output", "not-a-record"],
)
def test_a_module_that_fails_is_none_and_not_launched_again(
    child: Child, plugin: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert ask(child, plugin, monkeypatch) is None
    again = Child()
    assert ask(again, plugin, monkeypatch) is None
    assert (child.launches, again.launches) == (1, 0)


def test_a_changed_module_is_asked_again_after_a_failure(plugin: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert ask(Child(hang=True), plugin, monkeypatch) is None
    plugin.write_bytes(b"MZ, rebuilt")
    again = Child()
    assert ask(again, plugin, monkeypatch) is not None
    assert again.launches == 1


def test_a_module_reporting_another_unique_id_is_none(plugin: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert ask(Child(), plugin, monkeypatch, unique_id=UNIQUE_ID + 1) is None


def test_each_module_is_kept_in_its_own_file_beside_the_cache_path(
    plugin: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = plugin.with_name("Other x64.dll")
    other.write_bytes(b"MZ")
    monkeypatch.setattr(subprocess, "run", Child())
    source = record_source(
        [(UNIQUE_ID, plugin), (UNIQUE_ID + 1, other)], plugin.parent / "config" / "vst2_records.json"
    )
    source(UNIQUE_ID), source(UNIQUE_ID + 1)
    assert len(list((plugin.parent / "config" / "vst2_records").glob("*.json"))) == 2


@pytest.mark.parametrize(
    ("plugin_flags", "live"),
    [
        (0x0, 0x4),
        (0x10, 0x104),  # replacing
        (0x20, 0x404),  # program chunks
        (0x100, 0x5),  # synth
        (0x8, 0x204),
        (0x131, 0x505),
        (0x1 | 0x2 | 0x200, 0x4),  # bits Live writes clear
    ],
)
def test_live_keeps_four_of_the_plugins_flags_in_bits_of_its_own(plugin_flags: int, live: int) -> None:
    assert live_flags(plugin_flags) == live
