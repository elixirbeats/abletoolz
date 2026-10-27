"""An Audio Unit's saved preset, as Live stores it in an ``AuPreset`` buffer.

The fixtures are native: each unit's own ClassInfo after an edited patch was
handed back to it (see ``state_fixtures/audio_units/manifest.json``). Crossing
them to the same plugin's other formats is tested through the plugin maps, in
``test_plugin_maps.py``.
"""

from __future__ import annotations

import plistlib

import pytest
from conftest import AU_UNITS, au_fixture

from abletoolz.plugins.identity.ids import AuId
from abletoolz.plugins.state.audio_unit import (
    JUCE_STATE,
    VERSION,
    ClassInfo,
    JuceClassInfo,
)
from abletoolz.plugins.state.errors import StateTransformError

SUPERMASSIVE = AU_UNITS["valhalla-supermassive"].identifier


# -- the preset -------------------------------------------------------------


@pytest.mark.parametrize("slug", AU_UNITS)
def test_a_preset_reencodes_byte_for_byte(slug: str) -> None:
    buffer = au_fixture(slug, "au.plist")
    assert ClassInfo.parse(buffer).encode() == buffer


@pytest.mark.parametrize(("slug", "unit"), AU_UNITS.items())
def test_a_preset_names_its_unit(slug: str, unit: AuId) -> None:
    assert ClassInfo.parse(au_fixture(slug, "au.plist")).identifier == unit.identifier


def test_a_binary_property_list_reads_the_same() -> None:
    """What a JUCE host hands back is the binary form of the same dictionary."""
    buffer = au_fixture("valhalla-supermassive", "au.plist")
    binary = plistlib.dumps(plistlib.loads(buffer), fmt=plistlib.FMT_BINARY)
    assert ClassInfo.parse(binary).encode() == buffer


@pytest.mark.parametrize("key", [VERSION, "subtype"])
def test_a_preset_no_unit_restores_is_refused(key: str) -> None:
    """Measured: a unit answers kAudioUnitErr_InvalidPropertyValue without these."""
    entries = plistlib.loads(au_fixture("valhalla-supermassive", "au.plist"))
    del entries[key]
    with pytest.raises(StateTransformError, match=key):
        ClassInfo.parse(plistlib.dumps(entries))


def test_something_that_is_not_a_property_list_is_refused() -> None:
    with pytest.raises(StateTransformError, match="property list"):
        ClassInfo.parse(b"FFBS not a plist")


@pytest.mark.parametrize("damage", ["truncated", "encoding"])
def test_a_damaged_xml_preset_is_refused(damage: str) -> None:
    """A cut-off buffer or a bogus encoding header is one device's problem, not the set's."""
    buffer = au_fixture("valhalla-supermassive", "au.plist")
    if damage == "truncated":
        buffer = buffer[: len(buffer) // 2]
    else:
        buffer = buffer.replace(b'encoding="UTF-8"', b'encoding="no-such-codec"', 1)
    with pytest.raises(StateTransformError, match="property list"):
        ClassInfo.parse(buffer)


# -- the framing a JUCE unit's AU format names --------------------------------


def test_the_au_framing_reads_the_juce_state() -> None:
    buffer = au_fixture("valhalla-supermassive", "au.plist")
    assert JuceClassInfo(SUPERMASSIVE).lift(buffer) == ClassInfo.parse(buffer).plugin_state


def test_the_au_framing_writes_the_smallest_preset_the_unit_restores() -> None:
    written = ClassInfo.parse(JuceClassInfo(SUPERMASSIVE).lower(b"a patch"))
    assert written.identifier == SUPERMASSIVE
    assert written.plugin_state == b"a patch"
    assert sorted(written.entries) == sorted({"type", "subtype", "manufacturer", VERSION, "name", JUCE_STATE})


def test_a_preset_for_another_unit_is_refused() -> None:
    with pytest.raises(StateTransformError, match="not"):
        JuceClassInfo(SUPERMASSIVE).lift(au_fixture("valhalla-freqecho", "au.plist"))


def test_a_preset_without_juce_state_is_refused() -> None:
    """OpenBend writes none: everything it keeps is in its parameters."""
    entries = plistlib.loads(au_fixture("valhalla-supermassive", "au.plist"))
    del entries[JUCE_STATE]
    with pytest.raises(StateTransformError, match="no jucePluginState"):
        JuceClassInfo(SUPERMASSIVE).lift(plistlib.dumps(entries))
