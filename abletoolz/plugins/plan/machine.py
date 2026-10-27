"""What this machine has installed, as a plan sees it."""

from __future__ import annotations

import dataclasses
import pathlib
from collections.abc import Callable, Mapping
from xml.etree import ElementTree as ET

from abletoolz.machine import Locations
from abletoolz.plugins.config import AbletoolzConfig
from abletoolz.plugins.identity.ids import AuIdentifier, Identity, PluginKind, StoredId, UidFields, Vst2Id, Vst3Id
from abletoolz.plugins.identity.mapping import best_match
from abletoolz.plugins.identity.plugin_db import PluginDatabase, PluginEntry, load_plugin_db
from abletoolz.plugins.identity.vst3_parameters import ParameterSource, no_parameters, parameter_source
from abletoolz.plugins.loadable import oracle
from abletoolz.plugins.plan.evidence import known_state
from abletoolz.plugins.recovery_routes import RecoveryTarget


@dataclasses.dataclass(frozen=True)
class InstalledVst2:
    """One VST2 file installed here, and the plugin it is."""

    name: str
    unique_id: int
    path: pathlib.Path


@dataclasses.dataclass(frozen=True)
class Machine:
    """What is installed here, as the planner asks it.

    ``loadable`` answers, for one info element, whether Live can load its
    device today. ``vst2`` and ``vst3`` (by name) are the plugins installed,
    ``au`` asks the component registry, and ``recovery`` holds the native
    recovery routes with the binaries found for them. ``entries`` are the
    plugin database's records, whose vendors keep a name guess from crossing
    to another company's plugin; without them every vendor is unknown.
    ``parameters`` reads an installed VST3's parameters, for a device carrying
    Live's own list of them; without it no such device can change format.
    """

    vst2: tuple[InstalledVst2, ...]
    vst3: Mapping[str, UidFields]
    au: Callable[[AuIdentifier], bool]
    loadable: Callable[[ET.Element], bool]
    recovery: tuple[RecoveryTarget, ...] = ()
    entries: tuple[PluginEntry, ...] = ()
    parameters: ParameterSource = no_parameters

    @classmethod
    def here(cls, routes: tuple[RecoveryTarget, ...], *, config: AbletoolzConfig, locations: Locations) -> Machine:
        """What is installed at ``locations``: its plugin database, the AU registry and Live's own plugin database.

        Plan and apply both ask it, so a device is rewritten against the same
        machine it was planned against.
        """
        database = load_plugin_db(config, locations)
        loads = oracle(locations)
        return cls.of(
            database,
            routes,
            loadable=loads,
            au=loads.registered,
            parameters=parameter_source(database, locations.vst3_parameters),
        )

    @classmethod
    def of(
        cls,
        database: PluginDatabase,
        routes: tuple[RecoveryTarget, ...],
        *,
        loadable: Callable[[ET.Element], bool],
        au: Callable[[AuIdentifier], bool],
        parameters: ParameterSource = no_parameters,
    ) -> Machine:
        """The machine ``database`` describes, with these recovery routes and answers to loading."""
        vst2 = tuple(
            InstalledVst2(entry.name, entry.unique_id, entry.module_path)
            for entry in database.installed(PluginKind.VST)
            if entry.unique_id is not None and entry.module_path is not None
        )
        # Through the ranked lookup rather than the collapsed records, so every name
        # whose sources claim different class ids is shouted about: a wrong one
        # makes Live load another plugin without a word.
        uids = database.uid_lookup()
        vst3 = {name: fields for name in sorted(uids.names()) if (fields := uids.resolve(name)) is not None}
        return cls(
            vst2=vst2,
            vst3=vst3,
            au=au,
            loadable=loadable,
            recovery=routes,
            entries=database.installed(PluginKind.VST) + database.installed(PluginKind.VST3),
            parameters=parameters,
        )

    def vendor(self, identity: Identity) -> str | None:
        """Who made the plugin ``identity`` is: what an Audio Unit stores, else its own record in the database."""
        if identity.kind is PluginKind.AU:
            return identity.manufacturer
        return next(
            (entry.vendor for entry in self.entries if entry.kind is identity.kind and entry.name == identity.name),
            None,
        )

    def guess(self, identity: Identity, to: PluginKind) -> tuple[str, str] | None:
        """The installed plugin of format ``to`` an unmapped plugin most likely is, and why; or None.

        A plugin is never its own guess: a VST2 of the same unique id is
        another file of it, which is not a guess at all.
        """
        name = identity.name or ""
        candidates = (
            [entry.name for entry in self.vst2 if entry.unique_id != identity.unique_id]
            if to is PluginKind.VST
            else list(self.vst3)
        )
        vendors = {entry.name: entry.vendor for entry in self.entries if entry.kind is to}
        targets = {candidate: vendors.get(candidate) for candidate in candidates}
        match = best_match(name, identity.kind, to, targets, vendor=self.vendor(identity))
        if match is None:
            return None
        return match.target_name, f"{match.annotation}, {known_state(name, match.target_name, to).annotation}"

    def installed(self, stored: StoredId) -> bool:
        if isinstance(stored, Vst2Id):
            return any(entry.unique_id == stored.unique_id for entry in self.vst2)
        if isinstance(stored, Vst3Id):
            return stored.uid_fields in self.vst3.values()
        return self.au(stored.identifier)
