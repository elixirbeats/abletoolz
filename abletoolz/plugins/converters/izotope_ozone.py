"""Ozone 8 Elements into Ozone 9, setting by setting under the same names.

Ozone 8 Elements is the Ozone 8 generation, and Ozone 9 keeps that
generation's names: the Ozone 8 presets Ozone 9 ships and loads address the same
elements and parameters by the same names, and the values carry no unit of
their own (frequencies in Hz, gains in dB, Q as displayed). So each module's
settings move from Ozone 8 Elements' ``ElementChain`` element, where they sit
behind the module's name, into the Ozone 9 element of that name, and the
signal chain moves with them. What Ozone 9 has no setting for is a display
range, a grid, or a setting Ozone 8 Elements never offers; the last kind is
carried only at its neutral value.
"""

from __future__ import annotations

import base64
import datetime
from typing import Any

from abletoolz.plugins.converters import limitation
from abletoolz.plugins.state.catalog import Converted, converter
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.izotope_ozone import (
    OZONE_8_ELEMENTS_PRODUCT,
    OzoneDocument,
    chain_bytes,
    leaves,
    ozone_9_default,
)
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung

# The modules Ozone 8 Elements keeps in its element chain, by the prefix on their settings.
_MODULES = ("Post Equalizer", "Stereo Imager", "Equalizer", "Maximizer")
# Elements both products keep at the same place under the same name.
_SHARED = ("Gain Input", "Gain Output", "Gain Output Trim", "ChannelOps")
_GLOBAL = ("Global Bypass", "Global AutoGain Enable")
# How Ozone 8 Elements' equalizer draws itself; Ozone 9 keeps none of it.
_DISPLAY = (
    "Curve Magnitude",
    "Delay Magnitude",
    "Phase Magnitude",
    "Spectrum Magnitude",
    "Ruler Frequency",
    "Show Gridlines",
)
# Settings with no Ozone 9 counterpart, and the value at which they do nothing.
_NEUTRAL: dict[str, Any] = {"Amount": 0, "Surgical Enable": False, "Aux: Surgical Enable": False}


def convert_ozone_8_elements(chunk: bytes, *, lossy: bool = False) -> tuple[OzoneDocument, tuple[str, ...]]:
    """An Ozone 8 Elements patch as Ozone 9's own, and what could not be carried.

    A setting Ozone 9 has no place for is refused; with ``lossy`` it is left out
    and said in the notices.
    """
    source = OzoneDocument.parse(chunk)
    if source.product != OZONE_8_ELEMENTS_PRODUCT:
        raise StateTransformError(f"Product word {source.product:#x} is not Ozone 8 Elements'")
    notices: list[str] = []
    left_out = notices if lossy else None
    target = ozone_9_default()
    elements = target.elements()

    for name, node in leaves(source.element("ElementChain")).items():
        module = next((module for module in _MODULES if name.startswith(module)), None)
        if module is None:
            continue  # the chain element's own fields: its name, version and module list
        setting = name[len(module) :]
        destination = leaves(target.element(module)).get(setting)
        if destination is not None:
            _copy(node, destination, f"{module}: {setting}")
        elif setting.startswith(_DISPLAY):
            continue
        elif setting not in _NEUTRAL or node["Value"] != _NEUTRAL[setting]:
            limitation(left_out, f"{module}'s {setting} has no Ozone 9 counterpart; it is left out")

    for module in ("Equalizer", "Post Equalizer"):
        settings = leaves(target.element(module))
        # Ozone 9 draws a band's node only when told to; its presets show the enabled ones.
        for name, node in settings.items():
            visible = settings.get(name.removesuffix(" Enable") + " Visible")
            if name.endswith(" Enable") and visible is not None:
                visible["Value"] = node["Value"]

    imager = leaves(source.element("ElementChain")).get("Stereo ImagerEnable Stereoizer")
    if imager is not None and imager["Value"]:
        notices.append("Ozone 9 has two Stereoize modes and Ozone 8 Elements one; it plays in Ozone 9's default mode.")

    for element in _SHARED:
        targets = leaves(target.element(element))
        for name, node in leaves(source.element(element)).items():
            if name in targets and name not in ("Element Name", "Param Version"):
                _copy(node, targets[name], f"{element}: {name}")
    global_targets = leaves(target.element("Global"))
    for name, node in leaves(source.element("Global")).items():
        if name in _GLOBAL:
            _copy(node, global_targets[name], name)

    chain = source.chain()
    for module in chain:
        if module not in _MODULES or module not in elements:
            raise StateTransformError(f"Ozone 8 Elements' chain names {module!r}, which Ozone 9 has no element for")
    leaves(target.element("ElementChain"))["Extra Bytes"]["Value"] = chain_bytes(chain)

    context = target.tree["Context State"]["Value"]
    context["PluginUUID"] = dict(source.tree["Context State"]["Value"]["PluginUUID"])
    # Ozone 9 labels the program it loaded at the head of its preset state, as <name>.
    preset = leaves(target.element("Preset State"))["Extra Bytes"]
    label = b"<" + source.name + b">"
    preset["Value"] = base64.b64encode(label + base64.b64decode(preset["Value"])[len(label) :]).decode("ascii")
    return OzoneDocument(target.product, target.format, target.tree, source.name), tuple(notices)


def _copy(source: dict[str, Any], target: dict[str, Any], name: str) -> None:
    if source.get("Type") != target.get("Type"):
        raise StateTransformError(
            f"{name} is a {source.get('Type')} in Ozone 8 Elements and a {target.get('Type')} in Ozone 9"
        )
    target["Value"] = source["Value"]


@converter(
    "izotope.ozone-8-elements",
    "izotope.ozone-9",
    MeasuredState(StateRung.RE_ENCODE, (StateEvidence.HOSTED,), datetime.date(2026, 10, 31)),
)
def ozone_8_elements_to_ozone_9(patch: bytes, program: int | None) -> Converted:
    """An Ozone 8 Elements patch as Ozone 9's own."""
    document, notices = convert_ozone_8_elements(patch, lossy=True)
    return Converted(document.encode(), notices)
