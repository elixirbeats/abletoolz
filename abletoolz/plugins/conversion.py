"""What a plugin device becomes, and writing it.

A device becomes something in one of two ways, and never a third (ADR 0001).
Within its format it is edited in place: an :class:`Edit` names the fields of
its info element that change -- the file it points at, its name, its unique id,
its program counts -- and nothing else is touched. Across formats it is rebuilt:
a :class:`Rebuild` is everything the device is and holds afterwards -- the
format, the identity, the saved state, and Live's list of its parameters when
it carries one. Whatever decides what a device becomes -- a plan's routes, a
recovery route -- answers a refusal or one of the two (:data:`Answer`), and
:func:`write` writes it.

A device whose plugin abletoolz has mapped needs no rule: its plugin map knows
its identity in every format and how its patch crosses, and :func:`as_format`
works out the rebuild that uses it. Turning it into another mapped plugin is
:func:`as_plugin`: the source plugin's map lifts its patch, the converter
converts it, and the target plugin's map writes it. Both work the result out
without touching the device.

A device whose only controls in use are the list of parameters Live made for
it by itself crosses too: it gets the list Live would make for the target VST3.
Anything that points at a parameter -- automation, modulation, a MIDI or macro
mapping -- still refuses the device, because the two formats number their
parameters differently and nothing moves a lane.
"""

from __future__ import annotations

import copy
import dataclasses
import pathlib

from abletoolz.elements import get_element
from abletoolz.plugins.container.device import PluginDevice, SavedState
from abletoolz.plugins.container.parameter_list import write_live_list
from abletoolz.plugins.container.rewritable import carries_live_list, refuse_stub
from abletoolz.plugins.identity.ids import AuId, Identity, PluginKind, Vst3Id, stored_id
from abletoolz.plugins.identity.vst3_parameters import ParameterSource, Vst3Parameter, live_list
from abletoolz.plugins.state.catalog import Converter
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.plugin_map import FormatState, PluginMap
from abletoolz.schema import Tag


@dataclasses.dataclass(frozen=True, slots=True)
class Edit:
    """A device's own info element, changed field by field in its own format.

    ``path`` and ``name`` are the plugin file the device points at and the
    name it shows. ``unique_id`` and ``programs``, when given, go in both the
    host's record and the saved state's own record of the plugin.
    """

    path: pathlib.Path
    name: str
    unique_id: int | None = None
    programs: int | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class Rebuild:
    """A device rebuilt as another format: what it is and holds afterwards.

    ``live_list`` is Live's own list of the device's parameters as Live would
    make it for the target, for a device that carries one; None leaves the list
    the device has.
    """

    kind: PluginKind
    identity: Identity
    saved_state: SavedState
    live_list: tuple[Vst3Parameter, ...] | None = None


# Why a device cannot become a target, or what it becomes.
type Answer = str | Edit | Rebuild


def write(device: PluginDevice, answer: Edit | Rebuild) -> None:
    """Write what ``device`` becomes: an edit of its own element, or its rebuild as another format."""
    if isinstance(answer, Edit):
        if answer.programs is not None:
            device.set_layout(programs=answer.programs)
            device.set_saved_state_record(programs=answer.programs)
        if answer.unique_id is not None:
            device.set_unique_id(answer.unique_id)
            device.set_saved_state_record(unique_id=answer.unique_id)
        device.set_reference(answer.path)
        device.set_name(answer.name)
        return
    refuse_stub(device)
    current = rebuilt = None
    if answer.live_list is not None:
        # Filled on a copy first, so a slot it cannot fill leaves the device as it was.
        owner = device.owner
        assert owner is not None  # carries_live_list found its list there
        current = get_element(owner, Tag.PARAMETER_LIST)
        rebuilt = copy.deepcopy(current)
        write_live_list(rebuilt, answer.live_list)
    device.rewrite(answer.kind, answer.identity, answer.saved_state)
    if current is not None and rebuilt is not None:
        current[:] = list(rebuilt)


def _saved_patch(device: PluginDevice, plugin_map: PluginMap) -> FormatState | None:
    """What ``device``, a device of ``plugin_map``'s plugin, saved; None when it saved no patch."""
    identity, saved = device.identity, device.saved_state
    if stored_id(identity) != plugin_map.plugin_format(device.kind).id:
        raise ValueError(f"{identity.name} is not {plugin_map.name}: its stored id is another plugin's")
    return FormatState(saved.state, saved.controller) if saved.state else None


def _live_list(
    device: PluginDevice, plugin_map: PluginMap, to_format: PluginKind, parameters: ParameterSource
) -> tuple[Vst3Parameter, ...] | None:
    """The slot list ``device`` needs as ``plugin_map``'s plugin in ``to_format``, or None when it keeps its own.

    A device whose only controls in use are Live's own list gets the list Live
    would make for the target VST3, read through ``parameters``. Raises when
    that list cannot be read -- the VST3 not installed, not in the plugin
    database, or not answering -- so the device is refused and left as it was.
    """
    if device.document is None or not carries_live_list(device):
        return None
    target = plugin_map.plugin_format(to_format).id
    if to_format is not PluginKind.VST3 or not isinstance(target, Vst3Id):
        raise StateTransformError("Live's list of this device's parameters can only be rebuilt for a VST3")
    found = parameters(target.uid_fields)
    if found is None:
        raise StateTransformError(
            f"Live's list of this device's parameters needs the installed {plugin_map.name} VST3, "
            "which could not be read"
        )
    return live_list(found)


def _rebuild(
    device: PluginDevice,
    plugin_map: PluginMap,
    to_format: PluginKind,
    written: FormatState | None,
    parameters: ParameterSource,
) -> Rebuild:
    """``device`` as ``plugin_map``'s plugin in ``to_format``, holding ``written``, or no patch for None."""
    target = plugin_map.plugin_format(to_format)
    identity = dataclasses.replace(
        device.identity,
        kind=to_format,
        name=target.name,
        uid_fields=target.id.uid_fields if isinstance(target.id, Vst3Id) else None,
        au=target.id.identifier if isinstance(target.id, AuId) else None,
    )
    if target.registration is not None:
        identity = dataclasses.replace(
            identity, manufacturer=target.registration.manufacturer, flags=target.registration.flags
        )
    if written is None:
        written = FormatState(b"")
    saved = dataclasses.replace(device.saved_state, state=written.state, controller=written.controller)
    return Rebuild(to_format, identity, saved, _live_list(device, plugin_map, to_format, parameters))


def as_format(
    device: PluginDevice, plugin_map: PluginMap, to_format: PluginKind, *, parameters: ParameterSource
) -> Rebuild:
    """``device`` as the same plugin in ``to_format``, its patch carried by the plugin's map.

    A device saved without a patch stays without one. Raises when the map
    cannot carry this patch, or when Live's list for the target cannot be read
    through ``parameters``.
    """
    saved = _saved_patch(device, plugin_map)
    written = None if saved is None else plugin_map.cross(saved, device.kind, to_format)
    return _rebuild(device, plugin_map, to_format, written, parameters)


def as_plugin(
    device: PluginDevice,
    converter: Converter,
    source: PluginMap,
    target: PluginMap,
    to_format: PluginKind,
    *,
    parameters: ParameterSource,
) -> Rebuild:
    """``device``, a device of ``source``'s plugin, as ``target``'s plugin in ``to_format``.

    The converter gets the bank program the device has selected. A device
    saved without a patch stays without one. Raises when any step cannot carry
    this patch; ``parameters`` is as for :func:`as_format`.
    """
    if (converter.source, converter.target) != (source.id, target.id):
        raise ValueError(f"That converter is not from {source.name} into {target.name}")
    saved = _saved_patch(device, source)
    written = None
    if saved is not None:
        converted = converter.convert(source.lift(saved, device.kind), device.saved_state.program_number)
        written = target.lower(converted.patch, to_format)
    return _rebuild(device, target, to_format, written, parameters)
