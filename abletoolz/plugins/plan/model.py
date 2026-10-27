"""The plan file: what it records, and reading and writing it."""

from __future__ import annotations

import datetime
import enum
import hashlib
import pathlib

import pydantic

from abletoolz.plugins.identity.ids import PluginKind, UidFields

PLAN_VERSION = 1
KEEP = "keep"
DEFAULT_FORMATS = (PluginKind.VST3, PluginKind.AU, PluginKind.VST)


class Pick(enum.StrEnum):
    """What a new plan picks before anyone edits it."""

    BROKEN = "broken"
    KEEP = "keep"
    BEST = "best"


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
    devices that cannot take this target, by reason, as worked out when the
    plan was made; applying checks again.
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
        """The target ``pick`` names, None for keep; an unknown name is refused."""
        if pick == KEEP:
            return None
        for target in self.targets:
            if target.id == pick:
                return target
        known = ", ".join([KEEP, *(target.id for target in self.targets)])
        raise ValueError(f"{self.plugin}: no target named {pick!r} (choose from {known})")

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
    pick: Pick
    formats: list[PluginKind]
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


# -- reading and writing the file -----------------------------------------------------


def dump_plan(plan: Plan) -> str:
    """The plan as the YAML file you edit."""
    import yaml

    header = (
        "# abletoolz plugin plan. Edit `pick` for a plugin, or add an override for one device,\n"
        "# then run `abletoolz plugins apply` on this file. `keep` leaves devices as they are.\n"
    )
    return header + yaml.safe_dump(plan.model_dump(mode="json"), sort_keys=False, allow_unicode=True, width=120)


def load_plan(text: str) -> Plan:
    """A plan file read back; a file that is not one is refused as a :class:`ValueError`."""
    import yaml

    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ValueError(f"not a plan file: {error}") from error
    return Plan.model_validate(document)
