"""What an Audio Unit saves, and how it crosses to the same plugin's VST2 and VST3.

Live stores an AU device's patch in ``AuPreset/Buffer`` as the unit's
``ClassInfo`` property -- Apple's preset dictionary, written as an XML property
list. Measured 2026-10-22: the four AU buffers of a Live 11.3 set and nineteen
``ClassInfo`` dumps taken straight from installed units through AudioToolbox
re-encode byte for byte through :mod:`plistlib`, so a buffer can be read,
edited and written back without a trace.

The dictionary holds the unit's identity -- ``type``, ``subtype``,
``manufacturer`` as uint32 FourCCs, a ``version`` and a preset ``name`` -- and
then whatever the unit saves. Two keys carry the patch:

* ``data`` is Apple's SDK parameter snapshot. A unit built on the SDK alone
  keeps nothing else. A JUCE unit writes one too, but it lags: after an edit or
  a restore it still holds the values from before, in every JUCE unit
  measured, so it is never where a JUCE patch lives.
* ``jucePluginState`` is a JUCE unit's own state, which is where it does live.

A unit answers a restored dictionary with ``kAudioUnitErr_InvalidPropertyValue``
when ``version`` is missing or the identity is not its own; given the five
identity keys and ``jucePluginState`` it restores the patch, ``data`` or not.

A JUCE unit's ``jucePluginState`` is the same bytes as its VST2 chunk, which is
what :class:`JuceClassInfo` relies on; the plugins it is measured on name it in
their maps, in their vendors' modules.
"""

from __future__ import annotations

import dataclasses
import plistlib
from collections.abc import Mapping
from xml.parsers.expat import ExpatError

from abletoolz.plugins.identity.ids import AuIdentifier
from abletoolz.plugins.identity.ids import fourcc as fourcc
from abletoolz.plugins.state.errors import StateTransformError

TYPE = "type"
SUBTYPE = "subtype"
MANUFACTURER = "manufacturer"
VERSION = "version"
NAME = "name"
JUCE_STATE = "jucePluginState"
_IDENTITY = (TYPE, SUBTYPE, MANUFACTURER)


@dataclasses.dataclass(frozen=True, slots=True)
class ClassInfo:
    """An Audio Unit's saved preset, every key kept whether understood or not."""

    entries: Mapping[str, object]

    @classmethod
    def parse(cls, buffer: bytes) -> ClassInfo:
        """Read the property list an ``AuPreset`` buffer holds, XML or binary."""
        try:
            entries = plistlib.loads(buffer)
        # A truncated XML list fails in expat, a bad encoding header in the codec lookup.
        except (plistlib.InvalidFileException, ValueError, ExpatError, LookupError) as error:
            raise StateTransformError(f"an AU buffer that is not a property list: {error}") from error
        if not isinstance(entries, dict):
            raise StateTransformError(f"an AU property list holds a {type(entries).__name__}, not a dictionary")
        for key in (*_IDENTITY, VERSION):
            if not isinstance(entries.get(key), int):
                raise StateTransformError(f"an AU preset without an integer {key!r}, which no unit restores")
        return cls(entries)

    @classmethod
    def build(cls, identifier: AuIdentifier, *, plugin_state: bytes, name: str = "") -> ClassInfo:
        """The smallest preset a JUCE unit restores: its identity and its state."""
        component_type, subtype, manufacturer = identifier
        return cls(
            {
                TYPE: component_type,
                SUBTYPE: subtype,
                MANUFACTURER: manufacturer,
                VERSION: 0,
                NAME: name,
                JUCE_STATE: plugin_state,
            }
        )

    @property
    def identifier(self) -> AuIdentifier:
        """The (type, subtype, manufacturer) triple an ``AuPluginInfo`` stores too."""
        component_type, subtype, manufacturer = (self.entries[key] for key in _IDENTITY)
        assert isinstance(component_type, int) and isinstance(subtype, int) and isinstance(manufacturer, int)
        return component_type, subtype, manufacturer

    @property
    def plugin_state(self) -> bytes | None:
        """A JUCE unit's own state, or None for a unit that writes none.

        JUCE leaves the key out when the plugin's state is empty, as OpenBend's
        is: everything it keeps is in its parameters.
        """
        state = self.entries.get(JUCE_STATE)
        return state if isinstance(state, bytes) else None

    def encode(self) -> bytes:
        """The XML property list, as Live and CoreFoundation write it."""
        return plistlib.dumps(dict(self.entries), fmt=plistlib.FMT_XML)


# -- the framing a JUCE unit's AU format names --------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class JuceClassInfo:
    """A JUCE Audio Unit: the plugin's own state under ``jucePluginState``.

    Writing builds the smallest preset the unit restores, with ``identifier``
    as its identity; reading refuses a preset for another unit.
    """

    identifier: AuIdentifier

    def lift(self, state: bytes) -> bytes:
        info = ClassInfo.parse(state)
        if info.identifier != self.identifier:
            raise StateTransformError(f"an AU preset for {info.identifier}, not {self.identifier}")
        patch = info.plugin_state
        if patch is None:
            raise StateTransformError("this AU preset holds no jucePluginState to carry across")
        return patch

    def lower(self, patch: bytes) -> bytes:
        return ClassInfo.build(self.identifier, plugin_state=patch).encode()
