"""What Live lists of a VST3 plugin's parameters, read from the plugin rather than stored.

Live gives a plugin device 128 parameter slots: the knobs that show under the
device, that automation lanes, macros and MIDI mappings point at. For a plugin
with few parameters it fills them by itself, and which ones it picks is not
Live's to decide -- the plugin says. Measured 2026-10-24 over every VST3 Pro-C
2, Pro-L 2 and Pro-G device in the user's sets (10,523 of them): Live's list is
the parameters the plugin marks automatable, without its bypass, in the
plugin's own order and under its own ids. Every list Live wrote is that list or
the start of it; older Live and plugin builds wrote shorter starts, and Live
11.0 left out the trailing program-change parameter that later versions keep.

So nothing per plugin is stored here. :func:`parameter_source` finds the
installed module through the plugin database it is given, has
:mod:`~abletoolz.plugins.identity.vst3_host` read its parameters in a child
process, and keeps what it read in a cache file (``Locations.vst3_parameters``),
keyed by the module file's size and time.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import logging
import os
import pathlib
import subprocess
import sys
from collections.abc import Callable

from abletoolz.plugins.identity.ids import PluginKind, UidFields
from abletoolz.plugins.identity.plugin_db import PluginDatabase

logger = logging.getLogger(__name__)

LIVE_PARAMETER_SLOTS = 128
"""How many parameter slots Live gives a plugin device."""

_HOST_TIMEOUT_SECONDS = 120

# Where the child finds abletoolz itself: the folder holding the package.
_PACKAGE_ROOT = str(pathlib.Path(__file__).resolve().parents[3])


@dataclasses.dataclass(frozen=True, slots=True)
class Vst3Parameter:
    """One parameter as the plugin describes it; the three values are in its own units."""

    id: int
    title: str
    steps: int
    flags: frozenset[str]
    minimum: float
    maximum: float
    default: float


def live_list(parameters: tuple[Vst3Parameter, ...]) -> tuple[Vst3Parameter, ...]:
    """The parameters Live lists by itself for a device of this plugin, slot by slot."""
    listed = tuple(p for p in parameters if "can_automate" in p.flags and "bypass" not in p.flags)
    return listed[:LIVE_PARAMETER_SLOTS]


def _key(module: pathlib.Path, uid_fields: UidFields) -> str:
    status = module.stat()
    return f"{module.resolve()}|{status.st_size}|{status.st_mtime_ns}|{','.join(map(str, uid_fields))}"


def _read_cache(cache_path: pathlib.Path) -> dict[str, list[dict[str, object]]]:
    try:
        return dict(json.loads(cache_path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def _write_cache(cache_path: pathlib.Path, key: str, parameters: list[dict[str, object]]) -> None:
    """Add one module's answer to the cache, keeping what other planning processes wrote meanwhile."""
    temporary = cache_path.with_name(f"{cache_path.name}.{os.getpid()}")
    temporary.write_text(json.dumps(_read_cache(cache_path) | {key: parameters}), encoding="utf-8")
    try:
        os.replace(temporary, cache_path)
    except PermissionError:  # another process has the cache open; this answer is asked for again next run
        temporary.unlink()


def read(module: pathlib.Path, uid_fields: UidFields, cache_path: pathlib.Path) -> tuple[Vst3Parameter, ...]:
    """Every parameter of the class ``uid_fields`` in ``module``, read once and then kept in ``cache_path``.

    Raises :class:`OSError` when the module cannot be read: not on Windows, the
    file gone, or the plugin failing, hanging or crashing in its child process.
    """
    key = _key(module, uid_fields)
    cache = _read_cache(cache_path)
    if key not in cache:
        command = [sys.executable, "-m", "abletoolz.plugins.identity.vst3_host", str(module), *map(str, uid_fields)]
        # Run beside the cache: some plugins write logs into whatever folder they start in.
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        search_path = os.pathsep.join(filter(None, (_PACKAGE_ROOT, os.environ.get("PYTHONPATH"))))
        try:
            done = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=_HOST_TIMEOUT_SECONDS,
                check=False,
                cwd=cache_path.parent,
                env={**os.environ, "PYTHONPATH": search_path},
            )
        except subprocess.TimeoutExpired as error:
            raise OSError(f"{module.name} did not answer within {_HOST_TIMEOUT_SECONDS} s") from error
        if done.returncode != 0:
            last = (done.stderr.strip().splitlines() or ["no message"])[-1]
            raise OSError(f"{module.name} could not be read: {last}")
        cache[key] = json.loads(done.stdout)
        _write_cache(cache_path, key, cache[key])
    return tuple(
        Vst3Parameter(
            id=int(str(entry["id"])),
            title=str(entry["title"]),
            steps=int(str(entry["steps"])),
            flags=frozenset(str(flag) for flag in entry["flags"]),  # type: ignore[attr-defined]
            minimum=float(str(entry["minimum"])),
            maximum=float(str(entry["maximum"])),
            default=float(str(entry["default"])),
        )
        for entry in cache[key]
    )


type ParameterSource = Callable[[UidFields], tuple[Vst3Parameter, ...] | None]
"""The parameters of the installed VST3 with a class id, or None when they cannot be read.

None is an answer, not an error: no installed module for the class, or a
module that would not say. A conversion that needs the list then refuses the
device, as it would without this module.
"""


def no_parameters(uid_fields: UidFields) -> None:
    """A :data:`ParameterSource` for a machine nothing is known about: no plugin's parameters can be read."""
    return None


def parameter_source(database: PluginDatabase, cache_path: pathlib.Path) -> ParameterSource:
    """The parameters of each VST3 ``database`` says is installed, read off its module once per class.

    What a module said is kept in ``cache_path``, so the next run does not ask it again.
    """

    @functools.cache
    def for_class(uid_fields: UidFields) -> tuple[Vst3Parameter, ...] | None:
        modules = [
            entry.module_path
            for entry in database.of_kind(PluginKind.VST3)
            if entry.uid_fields == uid_fields and entry.module_path is not None and entry.module_path.exists()
        ]
        for module in dict.fromkeys(modules):
            try:
                return read(module, uid_fields, cache_path)
            except (OSError, LookupError, ValueError) as error:
                logger.info("Could not read the parameters of %s: %s", module, error)
        return None

    return for_class
