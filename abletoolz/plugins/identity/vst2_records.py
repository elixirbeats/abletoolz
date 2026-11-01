"""What Live records about a VST2 it hosts, read from the installed plugin rather than stored.

A VST2 device carries the host's record of its plugin -- parameter and program
counts, flags, version, the VST version and category it reports -- and its
preset carries the counts and version again. A device written for another
plugin takes them from that plugin: :mod:`~abletoolz.plugins.identity.vst2_host`
asks it in a child process, and the answer -- or why it failed, so a plugin that
hangs or crashes is launched once -- is kept in one file per module in the folder
named after ``Locations.vst2_records``, keyed by its path, size and time.

Live's ``Flags`` are not the plugin's own. Compared over 38 installed plugins
and the devices Live wrote for them, it keeps four of the plugin's bits in
others of its own -- replacing (0x10) as 0x100, program chunks (0x20) as 0x400,
synth (0x100) as 0x1, and 0x8 as 0x200 -- and adds 0x4 for most devices; the
rest of its bits differ between devices of one plugin and are written clear.
Its ``Inputs`` and ``Outputs`` are 0 in every device it wrote.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import pathlib
import subprocess
import sys
from collections.abc import Callable, Iterable
from typing import Any

from abletoolz.plugins.identity.ids import Vst2Record

logger = logging.getLogger(__name__)

_HOST_TIMEOUT_SECONDS = 60
_PACKAGE_ROOT = str(pathlib.Path(__file__).resolve().parents[3])

# The plugin's AEffect flag, and the bit Live keeps it in.
_LIVE_FLAG_BITS = ((0x10, 0x100), (0x20, 0x400), (0x100, 0x1), (0x8, 0x200))
_LIVE_DEVICE_FLAG = 0x4
_PROGRAM_CHUNKS = 0x20


type Vst2Source = Callable[[int], Vst2Record | None]
"""The record of the installed VST2 with a UniqueId, or None when none is installed or it would not say."""


def no_vst2_records(unique_id: int) -> None:
    """A :data:`Vst2Source` for a machine nothing is known about."""
    return None


def live_flags(plugin_flags: int) -> int:
    """The flags Live writes for a plugin reporting ``plugin_flags``."""
    flags = _LIVE_DEVICE_FLAG
    for plugin_bit, live_bit in _LIVE_FLAG_BITS:
        if plugin_flags & plugin_bit:
            flags |= live_bit
    return flags


def _key(module: pathlib.Path) -> str:
    status = module.stat()
    return f"{module.resolve()}|{status.st_size}|{status.st_mtime_ns}"


def _entry(cache_path: pathlib.Path, key: str) -> pathlib.Path:
    """One file per key, so processes asking about different modules never write the same file."""
    return cache_path.with_suffix("") / f"{hashlib.sha1(key.encode()).hexdigest()}.json"


def _ask(module: pathlib.Path, cache_path: pathlib.Path) -> dict[str, Any]:
    """The plugin's answer from a child process, or ``{"error": ...}`` when it failed, hung or said nothing."""
    search_path = os.pathsep.join(filter(None, (_PACKAGE_ROOT, os.environ.get("PYTHONPATH"))))
    command = [sys.executable, "-m", "abletoolz.plugins.identity.vst2_host", str(module)]
    try:
        done = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_HOST_TIMEOUT_SECONDS,
            check=False,
            # Run beside the cache: some plugins write logs into whatever folder they start in.
            cwd=cache_path.parent,
            env={**os.environ, "PYTHONPATH": search_path},
        )
    except subprocess.TimeoutExpired:
        return {"error": f"{module.name} did not answer within {_HOST_TIMEOUT_SECONDS} s"}
    if done.returncode != 0:
        last = (done.stderr.strip().splitlines() or ["no message"])[-1]
        return {"error": f"{module.name} could not be read: {last}"}
    try:
        answer = json.loads(done.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        answer = None
    return answer if isinstance(answer, dict) else {"error": f"{module.name} printed no record"}


def read(module: pathlib.Path, cache_path: pathlib.Path) -> Vst2Record:
    """The record of the VST2 in ``module``, asked once and then kept beside ``cache_path``.

    Raises :class:`OSError` when the module cannot be read: not on Windows, the
    file gone, or the plugin failing, hanging or crashing in its child process.
    A failure is kept too, so the module is asked again only once its file changes.
    """
    entry = _entry(cache_path, _key(module))
    try:
        answer = json.loads(entry.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        entry.parent.mkdir(parents=True, exist_ok=True)
        answer = _ask(module, cache_path)
        temporary = entry.with_name(f"{entry.name}.{os.getpid()}")
        temporary.write_text(json.dumps(answer), encoding="utf-8")
        try:
            os.replace(temporary, entry)
        except PermissionError:  # Windows: another process asking the same module has the entry open
            temporary.unlink()
    if "error" in answer:
        raise OSError(answer["error"])
    return Vst2Record(
        path=module,
        unique_id=answer["UniqueId"],
        parameters=answer["NumberOfParameters"],
        programs=answer["NumberOfPrograms"],
        flags=live_flags(answer["Flags"]),
        version=answer["Version"],
        vst_version=answer["VstVersion"],
        category=answer["Category"],
        chunks=bool(answer["Flags"] & _PROGRAM_CHUNKS),
    )


def record_source(installed: Iterable[tuple[int, pathlib.Path]], cache_path: pathlib.Path) -> Vst2Source:
    """The record of each installed VST2 by UniqueId, from the ``(unique_id, file)`` pairs found here."""
    by_id: dict[int, list[pathlib.Path]] = {}
    for unique_id, module in installed:
        by_id.setdefault(unique_id, []).append(module)

    @functools.cache
    def for_id(unique_id: int) -> Vst2Record | None:
        for module in by_id.get(unique_id, ()):
            try:
                record = read(module, cache_path)
            except (OSError, LookupError, ValueError, KeyError) as error:
                logger.info("Could not read %s: %s", module, error)
                continue
            if record.unique_id == unique_id:
                return record
        return None

    return for_id
