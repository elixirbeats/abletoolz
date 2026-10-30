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

Another format or plugin numbers its controls differently, so a rebuilt device
keeps no link to them: every automation and clip envelope pointing at its
controls is removed, and its list of controls -- with the MIDI and macro
mappings it holds -- becomes the list Live would make for an installed target
VST3, or an empty one. Each of these costs is a warning on the answer
(:func:`~abletoolz.plugins.container.rewritable.control_losses`), and nothing
is removed until everything that could fail has been worked out.
"""

from __future__ import annotations

import copy
import dataclasses
import pathlib
from xml.etree import ElementTree as ET

from abletoolz.elements import drop_envelopes, remove_children
from abletoolz.plugins.container.device import PluginDevice, PluginDevices, SavedState
from abletoolz.plugins.container.parameter_list import UNASSIGNED, UNASSIGNED_VISUAL_INDEX, write_live_list
from abletoolz.plugins.container.rewritable import control_losses, refuse_stub
from abletoolz.plugins.identity.ids import AuId, Identity, PluginKind, Vst3Id, stored_id
from abletoolz.plugins.identity.vst3_parameters import ParameterSource, Vst3Parameter, live_list
from abletoolz.plugins.state.catalog import Converter
from abletoolz.plugins.state.plugin_map import FormatState, PluginMap
from abletoolz.schema import Attr, Tag


@dataclasses.dataclass(frozen=True, slots=True)
class Edit:
    """A device's own info element, changed field by field in its own format.

    ``path`` and ``name`` are the plugin file the device points at and the
    name it shows. ``unique_id`` and ``programs``, when given, go in both the
    host's record and the saved state's own record of the plugin. ``forget_controls``
    says the file is another plugin, so links to the device's controls go as
    for a :class:`Rebuild`; ``warnings`` says what that costs.
    """

    path: pathlib.Path
    name: str
    unique_id: int | None = None
    programs: int | None = None
    forget_controls: bool = False
    warnings: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True, slots=True)
class Rebuild:
    """A device rebuilt as another format: what it is and holds afterwards.

    ``live_list`` is the device's list of controls afterwards: the one Live
    would make for the target VST3, or empty. ``warnings`` says what the
    rebuild costs, each in a sentence.
    """

    kind: PluginKind
    identity: Identity
    saved_state: SavedState
    live_list: tuple[Vst3Parameter, ...] = ()
    warnings: tuple[str, ...] = ()


# Why a device cannot become a target, or what it becomes.
type Answer = str | Edit | Rebuild


def write(device: PluginDevice, answer: Edit | Rebuild) -> None:
    """Write what ``device`` becomes: an edit of its own element, or its rebuild as another format."""
    if isinstance(answer, Edit):
        # Worked out before the first edit, while the device's controls still read as they were saved.
        forgetting = _forgetting(device, ()) if answer.forget_controls else None
        if answer.programs is not None:
            device.set_layout(programs=answer.programs)
            device.set_saved_state_record(programs=answer.programs)
        if answer.unique_id is not None:
            device.set_unique_id(answer.unique_id)
            device.set_saved_state_record(unique_id=answer.unique_id)
        device.set_reference(answer.path)
        device.set_name(answer.name)
        if forgetting is not None:
            forgetting.apply()
        return
    refuse_stub(device)
    forgetting = _forgetting(device, answer.live_list)
    device.rewrite(answer.kind, answer.identity, answer.saved_state)
    if forgetting is not None:
        forgetting.apply()


@dataclasses.dataclass(frozen=True, slots=True)
class _Forgetting:
    """Everything that drops a device's links to its controls, worked out before the device changes."""

    document: PluginDevices
    targets: frozenset[str]
    current: ET.Element | None
    rebuilt: ET.Element | None

    def apply(self) -> None:
        """Remove every envelope pointing at the old controls and swap in the rebuilt list; nothing here can fail."""
        if not self.targets.isdisjoint(self.document.referenced_ids):  # most devices are automated by nothing
            drop_envelopes(self.document.root, self.targets)
        if self.current is not None and self.rebuilt is not None:
            self.current[:] = list(self.rebuilt)
        self.document.changed()


def _forgetting(device: PluginDevice, listed: tuple[Vst3Parameter, ...]) -> _Forgetting | None:
    """What forgetting the device's controls takes; None for a device read outside a document, which has none known.

    The list is filled on a copy, so a slot it cannot fill raises with the
    device as it was. Without ``listed`` every slot is left naming nothing.
    """
    document = device.document
    if document is None:
        return None
    owner = device.owner
    current = None if owner is None else owner.find(Tag.PARAMETER_LIST)
    rebuilt = None if current is None else copy.deepcopy(current)
    if rebuilt is not None:
        if listed:
            write_live_list(rebuilt, listed)
        else:
            _unassign(rebuilt)
        for holder in list(rebuilt.iter()):
            if mappings := holder.findall(Tag.KEY_MIDI):
                remove_children(holder, mappings)
    return _Forgetting(document, device.controls.targets, current, rebuilt)


def _unassign(parameters: ET.Element) -> None:
    """Leave every slot naming no parameter, whatever layout its Live wrote."""
    for slot in parameters:
        for tag, value in ((Tag.PARAMETER_ID, UNASSIGNED), (Tag.VISUAL_INDEX, UNASSIGNED_VISUAL_INDEX)):
            element = slot.find(tag)
            if element is not None:
                element.set(Attr.VALUE, str(value))


def _saved_patch(device: PluginDevice, plugin_map: PluginMap) -> FormatState | None:
    """What ``device``, a device of ``plugin_map``'s plugin, saved; None when it saved no patch."""
    identity, saved = device.identity, device.saved_state
    if stored_id(identity) != plugin_map.plugin_format(device.kind).id:
        raise ValueError(f"{identity.name} is not {plugin_map.name}: its stored id is another plugin's")
    return FormatState(saved.state, saved.controller) if saved.state else None


def _live_list(
    device: PluginDevice, plugin_map: PluginMap, to_format: PluginKind, parameters: ParameterSource
) -> tuple[tuple[Vst3Parameter, ...], tuple[str, ...]]:
    """The device's list of controls as ``plugin_map``'s plugin in ``to_format``, and what it costs.

    It is the list Live would make for the target VST3, read through
    ``parameters`` from the installed plugin; for another format, or a VST3
    that cannot be read here, it is empty, which a device that listed
    controls hears about.
    """
    target = plugin_map.plugin_format(to_format).id
    found = parameters(target.uid_fields) if isinstance(target, Vst3Id) else None
    if found is not None:
        return live_list(found), ()
    if device.document is None or not device.controls.configured:
        return (), ()
    why = "it can only be built for a VST3" if to_format is not PluginKind.VST3 else "the VST3 could not be read here"
    return (), (f"its list of controls in Live was left empty: {why}",)


def _rebuild(
    device: PluginDevice,
    plugin_map: PluginMap,
    to_format: PluginKind,
    written: FormatState | None,
    parameters: ParameterSource,
    notices: tuple[str, ...] = (),
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
    listed, list_warnings = _live_list(device, plugin_map, to_format, parameters)
    warnings = (*notices, *control_losses(device), *list_warnings) if device.document is not None else notices
    return Rebuild(to_format, identity, saved, listed, warnings)


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
    written: FormatState | None = None
    notices: tuple[str, ...] = ()
    if saved is not None:
        converted = converter.convert(source.lift(saved, device.kind), device.saved_state.program_number)
        written, notices = target.lower(converted.patch, to_format), converted.notices
    return _rebuild(device, target, to_format, written, parameters, tuple(notices))
