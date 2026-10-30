"""The plan file: what it records, and reading and writing it."""

from __future__ import annotations

import collections
import datetime
import enum
import hashlib
import pathlib

import pydantic

from abletoolz.plugins.identity.ids import PluginKind, UidFields
from abletoolz.plugins.identity.plugin_db import FORMAT_LABELS

PLAN_VERSION = 2
KEEP = "keep"


class Route(enum.StrEnum):
    """How a device reaches a target."""

    FORMAT = "format"
    FILE = "file"
    RECOVERY = "recovery"
    CONVERTER = "converter"
    EXPERIMENT = "experiment"


# -- the file ------------------------------------------------------------------


class Target(pydantic.BaseModel):
    """One thing a plugin's devices can become.

    Which of the optional fields is set depends on the route: ``map_id`` names
    the plugin map written (format, converter), ``route_id`` the recovery route,
    ``path`` the installed file (file, a VST2 experiment), ``unique_id`` and
    ``uid_fields`` an experiment's identity. ``refused`` counts this plan's
    devices that cannot take this target, by reason, and ``warnings`` those that
    take it at a cost, by what it costs, as worked out when the plan was made;
    applying checks again.
    """

    model_config = pydantic.ConfigDict(extra="forbid")

    id: str
    route: Route
    plugin: str
    format: PluginKind
    evidence: str
    proved: bool
    installed: bool
    map_id: str | None = None
    route_id: str | None = None
    path: pathlib.Path | None = None
    unique_id: int | None = None
    uid_fields: UidFields | None = None
    refused: dict[str, int] = pydantic.Field(default_factory=dict)
    warnings: dict[str, int] = pydantic.Field(default_factory=dict)


class DeviceRef(pydantic.BaseModel):
    """One device of a plugin: which set, which device in it, on which track."""

    model_config = pydantic.ConfigDict(extra="forbid")

    set: int
    device: int
    track: str


class Override(pydantic.BaseModel):
    """One device that takes a different pick from the rest of its plugin's devices."""

    model_config = pydantic.ConfigDict(extra="forbid")

    set: int
    device: int
    pick: str


class PluginChoice(pydantic.BaseModel):
    """Every device of one plugin, what it can become, and what it becomes."""

    model_config = pydantic.ConfigDict(extra="forbid")

    plugin: str
    format: PluginKind
    name: str
    stored: str | None = None
    map_id: str | None = None
    broken: bool
    devices: list[DeviceRef]
    targets: list[Target] = pydantic.Field(default_factory=list)
    pick: str = KEEP
    overrides: list[Override] = pydantic.Field(default_factory=list)

    def target(self, pick: str) -> Target | None:
        """The target ``pick`` names exactly, None for keep; an unknown name is refused with the names there are."""
        if pick == KEEP:
            return None
        for target in self.targets:
            if target.id == pick:
                return target
        known = ", ".join(target.id for target in self.targets) or "nothing here"
        raise ValueError(
            f"{self.plugin} {FORMAT_LABELS[self.format]}: no target named {pick!r} (it can become {known})"
        )

    def pick_for(self, set_index: int, device: int) -> str:
        for override in self.overrides:
            if (override.set, override.device) == (set_index, device):
                return override.pick
        return self.pick


def set_sha256(path: pathlib.Path) -> str:
    """The hash a plan records of a set's bytes, which says whether the set changed since."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PlannedSet(pydantic.BaseModel):
    """A set the plan covers, and the bytes it was planned from."""

    model_config = pydantic.ConfigDict(extra="forbid")

    path: pathlib.Path
    sha256: str


class Plan(pydantic.BaseModel):
    """What every plugin device in some sets becomes."""

    model_config = pydantic.ConfigDict(extra="forbid")

    abletoolz_plan: int = PLAN_VERSION
    made: datetime.datetime
    sets: list[PlannedSet]
    plugins: list[PluginChoice]

    @pydantic.model_validator(mode="after")
    def _refs_name_listed_devices(self) -> Plan:
        """A hand edit that points at a set or device the plan does not list is refused, not ignored."""
        for choice in self.plugins:
            devices = {(ref.set, ref.device) for ref in choice.devices}
            for named in [ref.set for ref in choice.devices] + [override.set for override in choice.overrides]:
                if not 0 <= named < len(self.sets):
                    raise ValueError(f"{choice.plugin}: set {named} is not one of the plan's {len(self.sets)} sets")
            for override in choice.overrides:
                if (override.set, override.device) not in devices:
                    raise ValueError(
                        f"{choice.plugin}: the override for set {override.set} device {override.device} "
                        "names a device this entry does not list"
                    )
        return self


# -- the file people edit -----------------------------------------------------------
#
# Three fields per plugin: plugin, format, convert_to. Everything a plugin can
# become is a comment above its entry, so the file explains itself and reading it
# back ignores it. Apply reads the sets again, so the file holds no device numbers
# or hashes that an edit could knock out of step with the sets.


class PlanEntry(pydantic.BaseModel):
    """One plugin in the file: null leaves it as it is, a target's name converts it."""

    model_config = pydantic.ConfigDict(extra="forbid")

    plugin: str
    format: str
    convert_to: str | None = None


class PlanFile(pydantic.BaseModel):
    """The plan file as written: the sets it covers and what each plugin in them becomes."""

    model_config = pydantic.ConfigDict(extra="forbid")

    abletoolz_plan: int
    sets: list[pathlib.Path]
    plugins: list[PlanEntry]

    @pydantic.field_validator("abletoolz_plan")
    @classmethod
    def _this_version(cls, version: int) -> int:
        if version != PLAN_VERSION:
            raise ValueError(f"a plan file of version {version}, not {PLAN_VERSION}; make a fresh plan")
        return version


def entries(plan: Plan) -> list[tuple[str, PluginChoice]]:
    """Each plugin with the name its file entry uses.

    Plugins sharing a name and format are told apart by the name their devices store, and the stored id when
    that is shared too, never by their order: the order can change between planning and applying.
    """
    groups = collections.Counter((choice.plugin, choice.format) for choice in plan.plugins)
    stored_names = collections.Counter((choice.plugin, choice.format, choice.name) for choice in plan.plugins)
    named: list[tuple[str, PluginChoice]] = []
    for choice in plan.plugins:
        name = choice.plugin
        if groups[(choice.plugin, choice.format)] > 1:
            detail = choice.name
            if stored_names[(choice.plugin, choice.format, choice.name)] > 1:
                detail += f", {choice.stored or 'no id'}"
            name = f"{name} ({detail})"
        named.append((name, choice))
    return named


def take_picks(plan: Plan, file: PlanFile) -> None:
    """Set each plugin's pick from the file; a plugin the sets no longer hold, or an unknown name, is refused."""
    by_entry = {(name, FORMAT_LABELS[choice.format].casefold()): choice for name, choice in entries(plan)}
    for entry in file.plugins:
        choice = by_entry.get((entry.plugin, entry.format.casefold()))
        if choice is None:
            raise ValueError(f"{entry.plugin} {entry.format} is not in these sets any more; make a fresh plan")
        choice.pick = KEEP if entry.convert_to is None else entry.convert_to
        choice.target(choice.pick)


def _target_notes(target: Target, devices: int) -> list[str]:
    """What the plan file says under a target's name: where it is, and what taking it costs."""
    notes = [] if target.path is None else [str(target.path)]
    if not target.installed:
        notes.append("not installed here")
    if target.route is Route.EXPERIMENT:
        notes.append(f"a guess, {target.evidence}")
    notes.extend(f"{count} of {devices} device(s) can't: {reason}" for reason, count in target.refused.items())
    notes.extend(f"{count} of {devices} device(s): {cost}" for cost, count in target.warnings.items())
    return notes


def dump_plan(plan: Plan) -> str:
    """The plan as the file people edit: each plugin, what it can become in a comment above it, and its convert_to."""
    import yaml

    def scalar(value: object) -> str:
        return yaml.safe_dump(value, allow_unicode=True, width=10_000).removesuffix("...\n").strip()

    lines = [
        "# abletoolz plugin plan. Set convert_to to one of the names listed above a plugin,",
        "# then run: abletoolz plugins apply <this file>. null leaves a plugin as it is.",
        f"abletoolz_plan: {PLAN_VERSION}",
        "sets:",
        *(f"- {scalar(str(planned.path))}" for planned in plan.sets),
        "plugins:" if plan.plugins else "plugins: []",
    ]
    for name, choice in entries(plan):
        devices = len(choice.devices)
        state = ", doesn't load here" if choice.broken else ""
        lines += ["", f"# {name} {FORMAT_LABELS[choice.format]}: {devices} device(s){state}"]
        for target in choice.targets:
            lines += [f"#   {target.id}", *(f"#       {note}" for note in _target_notes(target, devices))]
        if not choice.targets:
            lines.append("#   nothing it can become here")
        convert_to = None if choice.pick == KEEP else choice.pick
        lines += [
            f"- plugin: {scalar(name)}",
            f"  format: {FORMAT_LABELS[choice.format]}",
            f"  convert_to: {scalar(convert_to)}",
        ]
    return "\n".join(lines) + "\n"


def load_plan(text: str) -> PlanFile:
    """A plan file read back; a file that is not one is refused as a :class:`ValueError`."""
    import yaml

    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ValueError(f"not a plan file: {error}") from error
    return PlanFile.model_validate(document)
