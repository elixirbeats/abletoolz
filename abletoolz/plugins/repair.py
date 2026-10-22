"""Replace the plugin devices a set can no longer load with ones it can.

Repair is the whole user-facing verb. It converts only devices Live cannot load
today, and only where the user has written down what the replacement is. It
never guesses: a device with no mapping entry, a mapping naming a format pair
nothing can translate, or a mapping whose class id nothing can supply is
reported and left exactly as it was.

Direction is not an argument. Every mapping entry carries its own ``to`` format
(see :class:`~abletoolz.plugins.container.format_translation.TargetConfig`), so the
table says which way each plugin goes and the command says nothing at all --
which is the only way one table can hold a VST2 that should become a VST3 and a
VST3 that should become something else.

Whether a device is loadable is a machine question, not a set question, and it
is the one thing repair does *not* take from the plugin database. That database
records identity -- names, class ids, modules -- which is stable enough to keep
in a file. Loadability flips the moment the user toggles a plug-in folder in
Live's preferences, and a stale "yes" makes repair skip a device that is really
broken, so it is read live, every run, and it gets two answers:

* Live's own plugin database, when there is one. Measured on Live 12.4.5b:
  ``plugin_domains.enabled`` is the only column that distinguishes anything --
  ``plugins.scanstate`` and ``plugins.enabled`` are 1 for every row. With the
  VST2 custom folder switched off in preferences, the ``custom`` domain holding
  every VST2 module reads disabled and Live shows those devices as missing
  though their .dll files are all still there; switched back on and rescanned,
  the same database says the opposite. A module that failed the scan, or that
  sits in a folder Live never scans, has no row either way. The full
  measurement, both times, is in :mod:`abletoolz.plugins.identity.uid_sources`.
* Failing that -- no Live installed, another OS, a database whose schema moved
  on -- the same disk evidence ``Plugins.scan`` reports: a VST2 is loadable if
  its stored path is still there or its file turns up in a plugin folder, a VST3
  is loadable if its display name resolves to something installed.

A device that is broken and unmapped gets a suggested config line, built with
the same name rules :mod:`abletoolz.plugins.identity.mapping` tiers with, so the
one-device suggestion here and the whole-machine survey behind
``--suggest-plugin-mappings`` can never drift apart. Like every suggestion, it
is a line to read and paste, never something repair acts on.

A repaired device says what is known about its patch as well as what it became.
Converting the container and getting the identity right still leaves the third
axis open, and only two things close it: somebody listened, or the vendor
declared compatibility (see :mod:`abletoolz.plugins.state`). So a run
counts what it fixed in two piles -- measured and experimental -- and the second
number is the one that tells the user how much auditioning is left.
"""

from __future__ import annotations

import dataclasses
import enum
import logging
import pathlib
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

from abletoolz.machine import default_live_database_dir
from abletoolz.plugins.container.device import PluginDevice, PluginDevices, PluginKind, set_supports
from abletoolz.plugins.container.format_translation import (
    KNOWN_TRANSLATIONS,
    ConfiguredTarget,
    has_translator,
    resolve_target,
    translate_device,
)
from abletoolz.plugins.identity.mapping import suggest_target_name, suggestion_line
from abletoolz.plugins.identity.uid_sources import DatabasePlugin, UidLookup, read_live_databases
from abletoolz.plugins.state import MeasuredState, measured_state

if TYPE_CHECKING:
    from abletoolz.live_set.document import AbletonSet

logger = logging.getLogger(__name__)

# Given one plugin info element, whether Live could load that device today.
type LoadableOracle = Callable[[ET.Element], bool]


class RepairStatus(enum.StrEnum):
    """What repair did about one device, and why."""

    FIXED = "fixed"
    BROKEN_UNMAPPED = "broken_unmapped"
    BROKEN_NO_UID = "broken_no_uid"
    UNSUPPORTED_PAIR = "unsupported_pair"
    SET_TOO_OLD_FOR_TARGET = "set_too_old_for_target"
    INCOMPLETE_DEVICE = "incomplete_device"
    PARAMETER_TRANSLATION_REQUIRED = "parameter_translation_required"
    MAPPED_NOT_BROKEN = "mapped_not_broken"
    OK = "ok"


@dataclasses.dataclass(frozen=True)
class DeviceRepair:
    """One device repair looked at.

    ``target_format`` and ``target_name`` are the replacement where one is
    known, whether or not it was applied -- the format comes from the mapping
    entry, which is the only thing that decides direction. ``suggestion`` is a
    config line worth pasting, offered only for an unmapped device and never
    acted on.

    ``state`` is set on a device that was actually converted, and says what is
    known about whether its patch survived the trip: a rung somebody measured, or
    nothing, which makes that device an experiment the user should audition.
    """

    track: str
    source_format: PluginKind
    source_name: str
    status: RepairStatus
    target_format: PluginKind | None = None
    target_name: str | None = None
    suggestion: str | None = None
    state: MeasuredState | None = None


@dataclasses.dataclass(frozen=True)
class RepairReport:
    """What one :func:`repair_set` pass did, device by device."""

    actions: tuple[DeviceRepair, ...]

    def by_status(self, status: RepairStatus) -> tuple[DeviceRepair, ...]:
        return tuple(action for action in self.actions if action.status is status)

    @property
    def fixed_count(self) -> int:
        return len(self.by_status(RepairStatus.FIXED))

    @property
    def fixed_measured_count(self) -> int:
        """Repaired devices whose patch is known to survive, by ear or declaration."""
        return sum(
            1 for action in self.by_status(RepairStatus.FIXED) if action.state is not None and action.state.predictable
        )

    @property
    def fixed_experimental_count(self) -> int:
        """Repaired devices nobody has heard yet -- the ones that still need ears."""
        return self.fixed_count - self.fixed_measured_count

    @property
    def broken_unmapped_count(self) -> int:
        return len(self.by_status(RepairStatus.BROKEN_UNMAPPED))

    @property
    def broken_no_uid_count(self) -> int:
        return len(self.by_status(RepairStatus.BROKEN_NO_UID))

    @property
    def unsupported_pair_count(self) -> int:
        return len(self.by_status(RepairStatus.UNSUPPORTED_PAIR))

    @property
    def set_too_old_count(self) -> int:
        return len(self.by_status(RepairStatus.SET_TOO_OLD_FOR_TARGET))

    @property
    def incomplete_device_count(self) -> int:
        return len(self.by_status(RepairStatus.INCOMPLETE_DEVICE))

    @property
    def parameter_translation_required_count(self) -> int:
        return len(self.by_status(RepairStatus.PARAMETER_TRANSLATION_REQUIRED))

    @property
    def mapped_not_broken_count(self) -> int:
        return len(self.by_status(RepairStatus.MAPPED_NOT_BROKEN))

    @property
    def ok_count(self) -> int:
        return len(self.by_status(RepairStatus.OK))

    @property
    def suggestions(self) -> tuple[str, ...]:
        """Every suggested config line, once each, in the order they came up."""
        found: list[str] = []
        for action in self.actions:
            if action.suggestion is not None and action.suggestion not in found:
                found.append(action.suggestion)
        return tuple(found)


# -- is this device loadable ------------------------------------------------


def database_oracle(plugins: Sequence[DatabasePlugin]) -> LoadableOracle:
    """Answer from Live's plugin database: right folder switched on, file present."""
    loadable = frozenset((plugin.kind, plugin.name) for plugin in plugins if plugin.loadable)

    def check(info: ET.Element) -> bool:
        device = PluginDevice(info)
        return (device.kind, device.identity.name) in loadable

    return check


def _with_audio_units(check: LoadableOracle, live_set: AbletonSet) -> LoadableOracle:
    """Live's database lists VST2 and VST3; an Audio Unit is asked of the component registry."""
    plugins = live_set.plugins

    def judge(info: ET.Element) -> bool:
        device = PluginDevice(info)
        if device.kind is PluginKind.AU:
            return plugins.au_loadable(device.identity.au)
        return check(info)

    return judge


def disk_oracle(live_set: AbletonSet, *, vst_dirs: Sequence[pathlib.Path] | None = None) -> LoadableOracle:
    """Answer from disk, the way ``Plugins.scan`` already does.

    ``vst_dirs`` are extra folders to search, as ``scan`` takes them.
    """
    plugins = live_set.plugins
    plugins.found_vst_dirs.extend(vst_dirs or [])

    def check(info: ET.Element) -> bool:
        device = PluginDevice(info)
        identity, reference = device.identity, device.reference
        if device.kind is PluginKind.AU:
            return plugins.au_loadable(identity.au)
        if device.kind is PluginKind.VST3:
            if reference.path is not None and reference.path.exists():
                return True
            return identity.name is not None and plugins.search_vst3(identity.name) is not None
        if reference.path is not None and reference.path.exists():
            return True
        return reference.file_name is not None and plugins.search(reference.file_name) is not None

    return check


def default_oracle(
    live_set: AbletonSet,
    *,
    database_dir: pathlib.Path | None = None,
    vst_dirs: Sequence[pathlib.Path] | None = None,
) -> LoadableOracle:
    """Live's database where it can answer, disk evidence where it cannot.

    Both locations are injectable so a test never reads this machine's state.
    """
    directory = database_dir if database_dir is not None else default_live_database_dir()
    if directory is not None:
        known = read_live_databases(directory)
        if known:
            logger.debug("Judging plugins against %s entries in %s", len(known), directory)
            return _with_audio_units(database_oracle(known), live_set)
    logger.debug("No Live plugin database to consult; judging plugins by what is on disk")
    return disk_oracle(live_set, vst_dirs=vst_dirs)


# -- what to paste ----------------------------------------------------------


def _suggestion_with_state(source_name: str, target_name: str) -> str:
    """A config line for an unmapped device, with what is known about its patch.

    The same shape the whole-machine survey writes, comment and all, because a
    line the user pastes should say the same thing wherever they read it.
    """
    entry = suggestion_line(source_name, PluginKind.VST3, target_name)
    return f"{entry}  # {measured_state(source_name, target_name).annotation}"


# -- whole set --------------------------------------------------------------


def needs_parameter_mapper(device: PluginDevice) -> bool:
    """Whether changing this device's plugin format would orphan controls it has in use.

    Parameter indices are the plugin's own, and two formats of one plugin do
    not number them alike, so a bound, mapped, automated or stored parameter
    needs a mapper nothing here has yet.
    """
    controls = device.controls
    return not controls.owner_known or device.preset.has_parameter_settings or controls.in_use


def repair_set(
    live_set: AbletonSet,
    *,
    targets: Mapping[str, ConfiguredTarget] | None = None,
    uid_lookup: UidLookup | None = None,
    loadable: LoadableOracle,
) -> RepairReport:
    """Convert every broken device whose mapping entry says what it becomes.

    Each entry declares its own target format, so nothing here chooses a
    direction. An entry naming a pair with no translator is reported as
    :attr:`RepairStatus.UNSUPPORTED_PAIR` -- the user said what they wanted and
    abletoolz cannot do it yet, which is worth saying out loud and is not an
    error. A device only a stub was written for is reported the same way, as
    :attr:`RepairStatus.INCOMPLETE_DEVICE`: the set never says what that plugin
    is, so there is nothing to rewrite it into.

    A set older than the target format is the third refusal, and the one that
    matters most: writing the newer element into an older document costs the
    whole file, not the one device, because Live rejects a set holding a class
    its schema never declared. That is
    :attr:`RepairStatus.SET_TOO_OLD_FOR_TARGET`, and the way past it is to open
    the set in Live and save it, which upgrades the schema.

    Devices ``loadable`` accepts are never touched, whether or not they are
    mapped: repair fixes what is broken and leaves working devices alone. The
    caller saves. Configured, mapped or referenced controls require an explicit
    parameter translation before their plugin format can change.
    """
    table: dict[str, ConfiguredTarget] = dict(KNOWN_TRANSLATIONS)
    table.update(targets or {})
    version = live_set.version_tuple
    known_names = uid_lookup.names() if uid_lookup is not None else frozenset[str]()
    actions: list[DeviceRepair] = []

    for device in PluginDevices(live_set.root):
        info, source = device.info, device.kind
        name = device.identity.name or ""
        track = live_set.plugins.track_name(info)
        configured = table.get(name)

        if loadable(info):
            status = RepairStatus.MAPPED_NOT_BROKEN if configured else RepairStatus.OK
            target_format = configured.to_format if configured else None
            target_name = configured.name if configured else None
            actions.append(DeviceRepair(track, source, name, status, target_format, target_name))
            continue

        if configured is None:
            match = suggest_target_name(name, known_names)
            suggestion = None if match is None else _suggestion_with_state(name, match)
            actions.append(DeviceRepair(track, source, name, RepairStatus.BROKEN_UNMAPPED, None, None, suggestion))
            continue

        if not has_translator(source, configured.to_format):
            actions.append(
                DeviceRepair(
                    track,
                    source,
                    name,
                    RepairStatus.UNSUPPORTED_PAIR,
                    configured.to_format,
                    configured.name,
                )
            )
            continue

        if not device.complete:
            actions.append(
                DeviceRepair(
                    track,
                    source,
                    name,
                    RepairStatus.INCOMPLETE_DEVICE,
                    configured.to_format,
                    configured.name,
                )
            )
            continue

        # A schema mismatch makes the entire document invalid.
        if not set_supports(configured.to_format, version, minor_version=live_set.root.get("MinorVersion", "")):
            actions.append(
                DeviceRepair(
                    track,
                    source,
                    name,
                    RepairStatus.SET_TOO_OLD_FOR_TARGET,
                    configured.to_format,
                    configured.name,
                )
            )
            continue

        target = resolve_target(configured, uid_lookup)
        if target is None:
            actions.append(
                DeviceRepair(track, source, name, RepairStatus.BROKEN_NO_UID, configured.to_format, configured.name)
            )
            continue

        if source is not target.to_format and needs_parameter_mapper(device):
            actions.append(
                DeviceRepair(
                    track,
                    source,
                    name,
                    RepairStatus.PARAMETER_TRANSLATION_REQUIRED,
                    target.to_format,
                    target.name,
                )
            )
            continue

        translate_device(info, target)
        state = measured_state(name, target.name)
        actions.append(
            DeviceRepair(track, source, name, RepairStatus.FIXED, target.to_format, target.name, None, state)
        )
        logger.debug("Repaired %s on %s as %s %s (%s)", name, track, target.to_format, target.name, state.annotation)

    return RepairReport(tuple(actions))
