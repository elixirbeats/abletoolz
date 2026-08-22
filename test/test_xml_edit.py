"""Direct coverage for what a copied subtree owns and has to give up.

Every other exercise of these functions goes through the higher-level
clone/graft paths (test_clips_write.py, test_devices_write.py), whose
fixtures only ever produce ``AutomationTarget``/``Pointee`` owners. Nothing
there would catch a whole tag family -- modulation targets, MIDI controller
targets -- being missing from the predicate, which is exactly the bug this
file exists to pin down.

The binding fixture below is written out by hand for the same reason: the
only fixtures carrying ``KeyMidi`` are whole sets, where the indentation a
removal has to leave behind is buried under a drum rack.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

from abletoolz.live_set.xml_edit import renumber_pointee_ids, strip_remote_bindings

# One mapped device switch, one mapped parameter alongside real values, and one
# parameter whose binding is all it holds -- Live's own tab indentation throughout.
DEVICE_WITH_BINDINGS = """<PluginDevice Id="3">
\t<On>
\t\t<Manual Value="true" />
\t\t<KeyMidi>
\t\t\t<PersistentKeyString Value="" />
\t\t\t<IsNote Value="false" />
\t\t\t<Channel Value="0" />
\t\t\t<NoteOrController Value="7" />
\t\t\t<LowerRangeNote Value="-1" />
\t\t\t<UpperRangeNote Value="-1" />
\t\t\t<ControllerMapMode Value="0" />
\t\t</KeyMidi>
\t</On>
\t<ParameterList>
\t\t<PluginFloatParameter Id="0">
\t\t\t<ParameterValue>
\t\t\t\t<KeyMidi>
\t\t\t\t\t<PersistentKeyString Value="" />
\t\t\t\t</KeyMidi>
\t\t\t\t<Manual Value="0.5" />
\t\t\t</ParameterValue>
\t\t\t<ParameterId Value="12" />
\t\t</PluginFloatParameter>
\t\t<PluginFloatParameter Id="1">
\t\t\t<ParameterValue>
\t\t\t\t<KeyMidi>
\t\t\t\t\t<PersistentKeyString Value="" />
\t\t\t\t</KeyMidi>
\t\t\t</ParameterValue>
\t\t</PluginFloatParameter>
\t</ParameterList>
</PluginDevice>"""


def make_root(next_id: int) -> ET.Element:
    """A minimal root carrying just the LiveSet/NextPointeeId allocator."""
    root = ET.Element("Ableton")
    live_set = ET.SubElement(root, "LiveSet")
    ET.SubElement(live_set, "NextPointeeId", {"Value": str(next_id)})
    return root


def test_renumber_pointee_ids_covers_modulation_and_controller_target_families() -> None:
    """VolumeModulationTarget and ControllerTargets.N own ids too, not just AutomationTarget/Pointee."""
    root = make_root(500)
    subtree = ET.Element("AudioClip")
    warp = ET.SubElement(subtree, "WarpMarkers")
    volume_target = ET.SubElement(warp, "VolumeModulationTarget", {"Id": "12"})
    controllers = ET.SubElement(subtree, "MidiControllersListWrapper")
    controller_target = ET.SubElement(controllers, "ControllerTargets.3", {"Id": "13"})
    envelope = ET.SubElement(subtree, "AutomationEnvelope")
    automation_target = ET.SubElement(envelope, "AutomationTarget", {"Id": "14"})
    reference = ET.SubElement(envelope, "PointeeId", {"Value": "13"})  # names the controller target above

    renumbered = renumber_pointee_ids(subtree, root)

    new_ids = [volume_target.get("Id"), controller_target.get("Id"), automation_target.get("Id")]
    assert new_ids == ["500", "501", "502"]
    assert len(set(new_ids)) == 3  # every owner got its own fresh id

    counter = root.find("LiveSet/NextPointeeId")
    assert counter is not None
    assert int(counter.get("Value", "")) == 503  # advanced past every id just handed out

    assert renumbered["13"] == controller_target.get("Id")
    assert reference.get("Value") == controller_target.get("Id")  # remapped, not left on the stale id


def test_strip_remote_bindings_takes_every_keymidi_and_leaves_the_parameters() -> None:
    """A copy carries the parameter, not the key/MIDI mapping the donor had on it."""
    subtree = ET.fromstring(DEVICE_WITH_BINDINGS)
    assert len(list(subtree.iter("KeyMidi"))) == 3

    strip_remote_bindings(subtree)

    assert list(subtree.iter("KeyMidi")) == []
    values = [(node.tag, node.get("Value")) for node in subtree.iter() if node.tag in {"Manual", "ParameterId"}]
    assert values == [("Manual", "true"), ("Manual", "0.5"), ("ParameterId", "12")]
    assert [parameter.get("Id") for parameter in subtree.iter("PluginFloatParameter")] == ["0", "1"]


def test_strip_remote_bindings_leaves_indentation_live_would_have_written() -> None:
    """Removing the last child moves its tail to the one before, so the parent still closes on its own indent."""
    subtree = ET.fromstring(DEVICE_WITH_BINDINGS)

    strip_remote_bindings(subtree)

    written = ET.tostring(subtree, encoding="unicode")
    assert '\t\t<Manual Value="true" />\n\t</On>' in written
    assert '\t\t\t\t<Manual Value="0.5" />\n\t\t\t</ParameterValue>' in written
    assert "<ParameterValue />" in written  # nothing but a binding left it empty
