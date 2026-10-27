"""Whether a plugin device can be rewritten as a target, and what stops it.

Four facts decide it, and every route that rewrites a device asks them here:

* a stub device -- a set that never says what the device is -- cannot be
  rebuilt as another format, since nothing in it says what to build;
* a document older than the target format's floor cannot hold it at all;
* controls in use point at the plugin's own parameter numbers, which another
  format or another plugin numbers differently;
* except Live's own parameter list, which nobody chose and nothing points at:
  a route that can build the list Live would make for the target carries it.

A change that keeps the format (another file, a new name) rebuilds nothing, so
only the controls are asked about it. A plan and a recovery ask
:func:`refusal` before they write; the writers keep :func:`refuse_stub` as a
one-line backstop.
"""

from __future__ import annotations

from abletoolz.plugins.container.device import PluginDevice
from abletoolz.plugins.container.parameter_list import is_automatic
from abletoolz.plugins.identity.ids import PluginKind
from abletoolz.plugins.identity.plugin_db import FORMAT_LABELS
from abletoolz.schema import Tag

CONTROLS_IN_USE = "it has automation or mapped controls, which need a parameter map"
LIVE_LIST_IN_USE = "Live's own list of its parameters is in use, which this target cannot rebuild"


class IncompleteDevice(ValueError):
    """A device the set describes too thinly to rewrite. See :attr:`PluginDevice.complete`."""


def is_stub(device: PluginDevice) -> bool:
    """Whether the set never says what ``device`` is."""
    return device.identity.is_instrument is None or not device.complete


def refuse_stub(device: PluginDevice) -> None:
    """The writers' backstop: raise for a device whose set never says whether it is an instrument."""
    if device.identity.is_instrument is None:
        raise IncompleteDevice(f"{device.identity.name} is a stub device: the set never says what it is")


def refusal(device: PluginDevice, to: PluginKind, *, can_rebuild_live_list: bool) -> str | None:
    """Why ``device``, read through PluginDevices, cannot become a ``to`` device, or None when it can.

    ``can_rebuild_live_list`` says the route can build Live's own parameter
    list for its target; one that cannot refuses a device carrying that list.
    """
    if to is not device.kind:
        if is_stub(device):
            return "the set never says what this device is"
        assert device.document is not None  # read through PluginDevices
        if not device.document.supports(to):
            return f"the set is too old to hold a {FORMAT_LABELS[to]} device; open and save it in Live first"
    if not needs_parameter_mapper(device):
        return None
    if not carries_live_list(device):
        return CONTROLS_IN_USE
    if not can_rebuild_live_list:
        return LIVE_LIST_IN_USE
    return None


def needs_parameter_mapper(device: PluginDevice) -> bool:
    """Whether changing this device's plugin or format would orphan controls it has in use.

    Parameter indices are the plugin's own, and two formats of one plugin do
    not number them alike, so a bound, mapped, automated or stored parameter
    needs a mapper nothing here has yet.
    """
    controls = device.controls
    return not controls.owner_known or device.saved_state.has_parameter_settings or controls.in_use


def carries_live_list(device: PluginDevice) -> bool:
    """Whether the only controls this device has in use are the list Live made for it by itself.

    That list -- every parameter of a small plugin, in order -- is no choice
    anyone made, and nothing points at it here: no automation or modulation,
    no MIDI or macro mapping (both are ``KeyMidi``), no stored settings. Such a
    device can become a VST3 with the list Live would make for that VST3
    instead; see :mod:`abletoolz.plugins.container.parameter_list`.
    """
    controls = device.controls
    if not controls.owner_known or device.saved_state.has_parameter_settings:
        return False
    if controls.malformed or controls.mapped or controls.referenced or not controls.configured:
        return False
    owner = device.owner
    parameters = None if owner is None else owner.find(Tag.PARAMETER_LIST)
    return parameters is not None and is_automatic(parameters)
