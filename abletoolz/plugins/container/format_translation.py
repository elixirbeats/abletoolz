"""Translate a plugin device from one plugin format to another, in place.

A set stores a device as a ``PluginDesc`` wrapping one format-specific info
element -- ``VstPluginInfo``, ``Vst3PluginInfo``, ``AuPluginInfo``. Measured on
Live 12.4.5: the ``PluginDevice`` wrapper around ``PluginDesc`` is byte for byte
the same whichever format is inside, so a translation is entirely local to the
info element.

Reading an info element, and rebuilding one as another format, is
:mod:`abletoolz.plugins.container.device`'s job. What this module adds is
the recipe -- what a device becomes: the target format and identity, the
state policy its patch crosses under, the controller policy a VST3 target
needs -- and the one step that applies it: read the device, carry its patch
across, write the target. A new target format is a writer in the device
module, not a pair here; VST3 is the one written today, because that is the
output that has been loaded in Live and listened to.

What the target format cannot infer from the source is supplied by a
:class:`Recipe`: VST3 identifies a plugin by a class id that appears
nowhere in a VST2 device, so it has to come from somewhere else. The
:data:`KNOWN_TRANSLATIONS` seed table is one place; every other one is
:mod:`abletoolz.plugins.identity.uid_sources`.
"""

from __future__ import annotations

import dataclasses
from typing import Protocol

import pydantic

from abletoolz.plugins.container.device import PluginDevice, PluginKind, UidFields, has_writer
from abletoolz.plugins.state import (
    NO_CONTROLLER_STATE,
    ControllerState,
    CustomState,
    PreparedBytes,
    StatePolicy,
    StateTransform,
    parse_controller_state,
    parse_state,
    state_bytes,
)


class IncompleteDevice(ValueError):
    """A device the set describes too thinly to rewrite. See :attr:`PluginDevice.complete`."""


@dataclasses.dataclass(frozen=True)
class Recipe:
    """What one plugin becomes in another format.

    ``uid_fields`` is the target format's identity: for VST3 the four signed
    int32 slices of the Audio Module Class id. ``name`` is the display name the
    target format knows the plugin by, which is often not the VST2 file name
    ("FabFilter Pro-Q 3" becomes "Pro-Q 3").

    ``controller_state`` is the second half of a VST3 preset, and saying nothing
    about it means the empty element Live writes for a plugin that keeps none.
    That is right for the plugins measured to write none and wrong for the rest,
    which is why it is declared per target rather than assumed.

    ``from_format`` says which devices the recipe is for. A stored name belongs
    to one format -- a VST2's PlugName, a VST3's display name -- and the same
    name can mean a different plugin in another, so a recipe applies only to
    devices of its own source format. VST2 is the default, which is what every
    entry written before the field existed meant.
    """

    to_format: PluginKind
    name: str
    uid_fields: UidFields
    state_transform: StatePolicy = StateTransform.VERBATIM
    controller_state: ControllerState = NO_CONTROLLER_STATE
    from_format: PluginKind = PluginKind.VST


@dataclasses.dataclass(frozen=True)
class NamedRecipe:
    """A target known by display name only, whose class id is still to be found.

    What a config entry means when it gives a ``name`` and no ``uid``: the user
    knows which VST3 they want, and the class id is a machine fact
    :mod:`abletoolz.plugins.identity.uid_sources` can look up. Kept apart from
    :class:`Recipe` so nothing can reach ``apply_recipe`` without
    a class id in hand.
    """

    to_format: PluginKind
    name: str
    state_transform: StatePolicy = StateTransform.VERBATIM
    controller_state: ControllerState = NO_CONTROLLER_STATE
    from_format: PluginKind = PluginKind.VST


# What a mapping table may hold: an identity, or a name still to be resolved.
type ConfiguredRecipe = Recipe | NamedRecipe


class UidResolver(Protocol):
    """Anything that can turn a VST3 display name into its four Uid fields.

    A protocol rather than an import so that class id sourcing can depend on
    this module and not the other way round.
    """

    def resolve(self, name: str) -> UidFields | None: ...


def resolve_recipe(target: ConfiguredRecipe, uid_lookup: UidResolver | None) -> Recipe | None:
    """Give a configured target its class id, or answer None when none is known."""
    if isinstance(target, Recipe):
        return target
    fields = None if uid_lookup is None else uid_lookup.resolve(target.name)
    if fields is None:
        return None
    return Recipe(
        target.to_format, target.name, fields, target.state_transform, target.controller_state, target.from_format
    )


def _vst3(
    name: str,
    uid_fields: UidFields,
    state_transform: StatePolicy = StateTransform.VERBATIM,
) -> Recipe:
    """Seed-table shorthand for a VST3 target."""
    return Recipe(PluginKind.VST3, name, uid_fields, state_transform)


# Keyed by the VST2 PlugName a set stores. Measured 2026-08-10 against installed
# plugins and sets that already carry the VST3; every entry but the last was
# translated, loaded in Live 12.4.5 and confirmed by ear to keep its patch.
# "FabFilter Pro-Q 3" is measured only: its class id is cross-checked by
# test_format_translation against a VST3 instance in the 10.1.3 fixture, but no
# translated device has been listened to.
KNOWN_TRANSLATIONS: dict[str, Recipe] = {
    "Serum_x64": _vst3("Serum", (1448301656, 1718835315, 1701999981, 0)),
    "Ghz Tupe 3": _vst3("Ghz Tupe 3", (1448301652, 1345542247, 1752834164, 1970300192)),
    "Serato Sample": _vst3("Serato Sample", (1448301651, 1836084339, 1701994868, 1864397665)),
    "Prophet V3": _vst3("Prophet V3", (1098019957, 1096173907, 1347571507, 1349676899)),
    "FabFilter Timeless 3": _vst3("Timeless 3", (-756127758, -984465363, -1876879981, 452832875)),
    "FabFilter Pro-Q 3": _vst3("Pro-Q 3", (1925503857, 2051884442, -1182903948, 1568977821)),
    "kHs Distortion": _vst3("kHs Distortion", (-209286662, 4410480, 16003238, 13171824), StateTransform.KILOHEARTS),
    "kHs Filter": _vst3("kHs Filter", (2034193867, 5261734, 12553643, 11857824), StateTransform.KILOHEARTS),
}


# -- config -----------------------------------------------------------------


# Measured across the Kilohearts range: every kHs VST3 wraps the payload its
# VST2 stores raw, so a kHs entry that says nothing about state means this.
# A VST2 device from that vendor is named "kHs <effect>" in every set seen.
KILOHEARTS_PREFIX = "kHs "


class TargetConfig(pydantic.BaseModel):
    """One ``plugin_translation.targets`` entry from config.yaml.

    ``uid`` is optional: leaving it out asks for the class id to be looked up by
    ``name`` at translation time, which is the normal case on a machine that has
    the VST3 installed. ``state`` names a rung of the state ladder --
    ``verbatim``, ``kilohearts``, or ``custom:<name>`` for a registered
    per-plugin re-encode. Left out it means verbatim, except for Kilohearts
    plugins -- see :data:`KILOHEARTS_PREFIX`.

    ``controller`` names one of the registered ``ControllerState`` shapes, and
    left out it means the empty element Live writes for a plugin that keeps
    none. That is right for the plugins measured to write none and wrong for
    the rest, which is why it is written per entry rather than guessed from the
    target's name.
    """

    model_config = pydantic.ConfigDict(extra="forbid")

    # ``from`` is a keyword, so the field is spelled ``from_`` here and ``from`` in the file.
    from_: PluginKind = pydantic.Field(default=PluginKind.VST, alias="from")
    to: PluginKind = PluginKind.VST3
    name: str
    uid: tuple[int, int, int, int] | None = None
    state: StateTransform | CustomState | None = None
    controller: str | None = None

    @pydantic.field_validator("state", mode="before")
    @classmethod
    def _read_state(cls, raw: object) -> object:
        """Turn the written rung name into a policy, loudly if it names nothing."""
        return parse_state(raw) if isinstance(raw, str) else raw

    @pydantic.field_validator("controller")
    @classmethod
    def _check_controller(cls, raw: str | None) -> str | None:
        """Refuse a name nothing registered, here rather than at translation time.

        The name stays a name: a :class:`ControllerState` is a protocol and
        pydantic has no schema for one. It is looked up again in
        :meth:`target`, which is cheap and keeps the failure where the user can
        see which line caused it.
        """
        if raw is not None:
            parse_controller_state(raw)
        return raw

    def state_for(self, source_name: str) -> StatePolicy:
        """The state transform this entry asks for, or the vendor default."""
        if self.state is not None:
            return self.state
        if source_name.startswith(KILOHEARTS_PREFIX):
            return StateTransform.KILOHEARTS
        return StateTransform.VERBATIM

    def target(self, source_name: str) -> ConfiguredRecipe:
        """The mapping entry, resolved if it carries a class id and named if not."""
        transform = self.state_for(source_name)
        controller = NO_CONTROLLER_STATE if self.controller is None else parse_controller_state(self.controller)
        if self.uid is None:
            return NamedRecipe(self.to, self.name, transform, controller, self.from_)
        return Recipe(self.to, self.name, self.uid, transform, controller, self.from_)


def parse_config_targets(raw: object) -> dict[str, ConfiguredRecipe]:
    """Build targets from the ``plugin_translation.targets`` mapping in config.yaml."""
    targets = pydantic.TypeAdapter(dict[str, TargetConfig]).validate_python(raw)
    return {source_name: entry.target(source_name) for source_name, entry in targets.items()}


# -- applying a recipe ------------------------------------------------------


def carried_state(source: bytes, policy: StatePolicy) -> bytes:
    """The preset bytes a device carries across under ``policy``.

    A device saved without a patch stays without one -- there is nothing to
    reframe or re-encode -- unless the policy brings bytes of its own.
    """
    rewrite = state_bytes(policy)
    if rewrite is None or (not source and not isinstance(policy, PreparedBytes)):
        return source
    return rewrite(source)


def apply_recipe(device: PluginDevice, target: Recipe) -> None:
    """Rewrite ``device`` as ``target`` says: read it, carry its patch across, write the target format.

    The controller half of a VST3 preset is built from what the *source*
    device saved rather than from what the processor half becomes, because for
    the products that populate it the two are halves of the same saved chunk
    (measured over 150 sets: exactly one ControllerState per ProcessorState,
    917 pairs out of 917). A device saved with no patch has nothing to build
    one out of, and gets the empty element that says so -- unless its bytes
    are prepared, which carry their own controller half with them.
    """
    if device.kind is not target.from_format:
        raise ValueError(f"a recipe for {target.from_format.value} devices cannot rewrite a {device.kind.value} device")
    identity, preset = device.identity, device.preset
    if identity.is_instrument is None:
        raise IncompleteDevice(f"{identity.name} is a stub device: the set never says what it is")
    source = preset.state
    prepared = isinstance(target.state_transform, PreparedBytes)
    controller = target.controller_state.build(source) if source or prepared else None
    device.rewrite(
        target.to_format,
        dataclasses.replace(identity, kind=target.to_format, name=target.name, uid_fields=target.uid_fields),
        dataclasses.replace(preset, state=carried_state(source, target.state_transform), controller=controller),
    )


def can_rewrite(source: PluginKind, to: PluginKind) -> bool:
    """Whether a device of ``source`` can be rewritten as ``to`` at all.

    Direction comes from the mapping entry, so an entry may name a format
    nothing can write yet. That is a fact to report, not an error: the entry
    is the user's statement of intent, and the missing half is a writer nobody
    has measured yet.
    """
    return source is not to and has_writer(to)
