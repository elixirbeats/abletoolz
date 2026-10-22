"""Redacted routing tokens must not reach newly authored Live documents."""

from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest

from abletoolz.formats import FormatError, live_document


@pytest.mark.parametrize(
    ("kind", "target"),
    [
        ("AudioInputRouting", "p46251/p41230/p20475"),
        ("AudioOutputRouting", "p76088/p41230/p36996"),
        ("MidiInputRouting", "p70821/p41230.All/p73962"),
        ("MidiOutputRouting", "AudioOut/None"),
    ],
)
def test_authoring_preflight_rejects_invalid_routing_roots(kind: str, target: str) -> None:
    root = ET.Element("Ableton")
    route = ET.SubElement(root, kind)
    ET.SubElement(route, "Target", Value=target)
    with pytest.raises(FormatError, match=f"{kind}.*invalid routing target"):
        live_document.validate_routing(root)


def test_authoring_preflight_preserves_native_routes() -> None:
    root = ET.Element("Ableton")
    for kind, target in [
        ("AudioInputRouting", "AudioIn/External/S0"),
        ("AudioOutputRouting", "AudioOut/Main"),
        ("MidiInputRouting", "MidiIn/External.All/-1"),
        ("MidiOutputRouting", "MidiOut/None"),
    ]:
        route = ET.SubElement(root, kind)
        ET.SubElement(route, "Target", Value=target)
    original = ET.tostring(root)
    live_document.validate_routing(root)
    assert ET.tostring(root) == original
