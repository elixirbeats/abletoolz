"""Ozone 8 Elements patches converted into Ozone 9's own, on synthetic patches."""

from __future__ import annotations

import base64
from typing import Any

import pytest

from abletoolz.plugins.converters.izotope_ozone import convert_ozone_8_elements
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.izotope_ozone import (
    OZONE_8_ELEMENTS_PRODUCT,
    OZONE_9_PRODUCT,
    OzoneDocument,
    _default_chunk,
    chain_bytes,
    leaves,
    ozone_9_default,
)

UUID = "01234567-89AB-CDEF-0123-456789ABCDEF"


def _leaf(kind: str, value: object) -> dict[str, Any]:
    return {"Type": kind, "Value": value}


def _element(name: str, **settings: dict[str, Any]) -> dict[str, Any]:
    return _leaf("Dictionary", {"Element Name": _leaf("String", name), "Param Version": _leaf("UInt", 0)} | settings)


def elements_patch(chain: tuple[str, ...] = ("Equalizer", "Stereo Imager", "Maximizer"), **extra: Any) -> bytes:
    """An Ozone 8 Elements patch: module settings behind their module's name in the element chain."""
    settings = {
        "EqualizerBand 2 Enable": _leaf("Bool", True),
        "EqualizerBand 2 Frequency": _leaf("Float", 30),
        "EqualizerBand 2 Gain": _leaf("Float", -1.5),
        "EqualizerBand 2 Q": _leaf("Float", 1.8857144117355347),
        "EqualizerBand 2 Shape": _leaf("UInt", 6),
        "EqualizerBypass": _leaf("Bool", True),
        "EqualizerCurve Magnitude Max": _leaf("Float", 5.4),
        "EqualizerAmount": _leaf("Float", 0),
        "Stereo ImagerBand 1 Width Percent": _leaf("Float", 28.148147583007812),
        "Stereo ImagerNum Bands": _leaf("UInt", 1),
        "MaximizerThreshold": _leaf("Float", -7.9639892578125),
        "MaximizerMode": _leaf("UInt", 1),
    } | extra
    tree = {
        "Context State": _leaf("Dictionary", {"PluginUUID": _leaf("String", UUID)}),
        "DSP State": _leaf(
            "Dictionary",
            {
                "DSP Elements": _leaf(
                    "Dictionary",
                    {
                        "ElementChain": _element(
                            "ElementChain", **{"Extra Bytes": _leaf("Base64", chain_bytes(chain))}, **settings
                        ),
                        "Gain Input": _element("Gain Input", **{"Gain Input Left": _leaf("Float", -3)}),
                        "Gain Output": _element("Gain Output"),
                        "Gain Output Trim": _element("Gain Output Trim"),
                        "ChannelOps": _element("ChannelOps", Mono=_leaf("Bool", True)),
                        "Global": _element("Global", **{"Global Bypass": _leaf("Bool", False)}),
                    },
                )
            },
        ),
    }
    return OzoneDocument(OZONE_8_ELEMENTS_PRODUCT, 1, tree, b"Default").encode()


def test_ozone_9_default_encodes_back_to_the_bytes_the_plugin_saved() -> None:
    assert ozone_9_default().encode() == _default_chunk()


def test_settings_cross_into_ozone_9_modules_of_the_same_name() -> None:
    document, notices = convert_ozone_8_elements(elements_patch())
    assert notices == ()
    assert document.product == OZONE_9_PRODUCT and document.name == b"Default"
    equalizer = leaves(document.element("Equalizer"))
    assert equalizer["Band 2 Frequency"]["Value"] == 30
    assert equalizer["Band 2 Gain"]["Value"] == -1.5
    assert equalizer["Band 2 Q"]["Value"] == 1.8857144117355347
    assert equalizer["Band 2 Shape"]["Value"] == 6
    assert equalizer["Band 2 Enable"]["Value"] is True and equalizer["Band 2 Visible"]["Value"] is True
    assert equalizer["Bypass"]["Value"] is True
    assert leaves(document.element("Stereo Imager"))["Band 1 Width Percent"]["Value"] == 28.148147583007812
    assert leaves(document.element("Maximizer"))["Threshold"]["Value"] == -7.9639892578125
    assert leaves(document.element("Gain Input"))["Gain Input Left"]["Value"] == -3
    assert leaves(document.element("ChannelOps"))["Mono"]["Value"] is True
    assert document.chain() == ("Equalizer", "Stereo Imager", "Maximizer")
    assert document.tree["Context State"]["Value"]["PluginUUID"]["Value"] == UUID
    preset = base64.b64decode(leaves(document.element("Preset State"))["Extra Bytes"]["Value"])
    assert preset.startswith(b"<Default>\0")
    assert OzoneDocument.parse(document.encode()) == document


def test_the_registered_converter_writes_the_same_patch() -> None:
    planned = CATALOG.converter("izotope.ozone-8-elements", "izotope.ozone-9")
    assert planned is not None
    document, _ = convert_ozone_8_elements(elements_patch())
    assert planned.convert(elements_patch(), 0).patch == document.encode()


def test_a_setting_ozone_9_has_no_place_for_is_refused_or_reported() -> None:
    patch = elements_patch(EqualizerAmount=_leaf("Float", 4.0))
    with pytest.raises(StateTransformError, match="Amount has no Ozone 9 counterpart"):
        convert_ozone_8_elements(patch)
    document, notices = convert_ozone_8_elements(patch, lossy=True)
    assert notices == ("Equalizer's Amount has no Ozone 9 counterpart; it is left out",)
    assert "Amount" not in leaves(document.element("Equalizer"))


def test_a_setting_of_another_type_is_refused() -> None:
    with pytest.raises(StateTransformError, match="Maximizer: Threshold is a UInt"):
        convert_ozone_8_elements(elements_patch(MaximizerThreshold=_leaf("UInt", 3)), lossy=True)


def test_a_chain_module_ozone_9_lacks_is_refused() -> None:
    with pytest.raises(StateTransformError, match="'Reverb'"):
        convert_ozone_8_elements(elements_patch(chain=("Equalizer", "Reverb")), lossy=True)


def test_an_ozone_9_patch_is_not_taken_for_ozone_8_elements() -> None:
    with pytest.raises(StateTransformError, match="not Ozone 8 Elements'"):
        convert_ozone_8_elements(_default_chunk())


def test_a_chunk_that_is_not_the_wrapper_is_refused() -> None:
    with pytest.raises(StateTransformError, match="not an iZotope VST2 wrapper"):
        OzoneDocument.parse(b"\x01\x02\x03")
