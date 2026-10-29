"""Whether a plugin device can be rewritten as a target, and what rewriting it costs.

Only what would leave a set Live cannot open refuses a device (:func:`refusal`):

* a stub device -- a set that never says what the device is -- cannot be
  rebuilt as another format, since nothing in it says what to build;
* a document older than the target format's floor cannot hold it at all.

Everything else is a cost, said up front and paid when the device is written
(:func:`control_losses`): automation and clip envelopes, MIDI and macro
mappings and an arranged list of controls all point at the plugin's own
control numbers, which another format or plugin numbers differently, so they
are removed rather than left pointing at the wrong control. The writers keep
:func:`refuse_stub` as a one-line backstop.
"""

from __future__ import annotations

from abletoolz.plugins.container.device import PluginDevice
from abletoolz.plugins.container.parameter_list import is_automatic
from abletoolz.plugins.identity.ids import PluginKind
from abletoolz.plugins.identity.plugin_db import FORMAT_LABELS
from abletoolz.schema import Tag

# Why every link to a control goes: the plugin it now is numbers its controls differently.
_RENUMBERED = "the converted plugin numbers its controls differently"


class IncompleteDevice(ValueError):
    """A device the set describes too thinly to rewrite. See :attr:`PluginDevice.complete`."""


def is_stub(device: PluginDevice) -> bool:
    """Whether the set never says what ``device`` is."""
    return device.identity.is_instrument is None or not device.complete


def refuse_stub(device: PluginDevice) -> None:
    """The writers' backstop: raise for a device whose set never says whether it is an instrument."""
    if device.identity.is_instrument is None:
        raise IncompleteDevice(f"{device.identity.name} is a stub device: the set never says what it is")


def refusal(device: PluginDevice, to: PluginKind) -> str | None:
    """Why ``device``, read through PluginDevices, cannot become a ``to`` device at all, or None when it can."""
    if to is device.kind:
        return None
    if is_stub(device):
        return "the set never says what this device is"
    assert device.document is not None  # read through PluginDevices
    if not device.document.supports(to):
        return f"the set is too old to hold a {FORMAT_LABELS[to]} device; open and save it in Live first"
    return None


def control_losses(device: PluginDevice) -> tuple[str, ...]:
    """What becoming another format or plugin removes from ``device``, each said as a warning; empty for nothing."""
    controls = device.controls
    losses: list[str] = []
    if not controls.owner_known:
        losses.append("its controls could not be read, so links to them may stay behind pointing at nothing")
    assert device.document is not None  # read through PluginDevices
    if any(device.document.pointees[target] for target in controls.targets):
        losses.append(f"its automation and clip envelopes removed: {_RENUMBERED}")
    if controls.mapped:
        losses.append(f"its MIDI and macro mappings removed: {_RENUMBERED}")
    owner = device.owner
    parameters = None if owner is None else owner.find(Tag.PARAMETER_LIST)
    if parameters is not None and (controls.malformed or (controls.configured and not is_automatic(parameters))):
        losses.append("the list of its controls arranged in Live was replaced")
    if device.saved_state.has_parameter_settings:
        losses.append("its saved per-control settings were dropped")
    return tuple(losses)


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
