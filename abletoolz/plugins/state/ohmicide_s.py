"""Ohm Force Ohmicide[S]: its patch, edited by parameter index in the plain units the plugin stores.

A JUCE plugin. Its patch is a JSON document and a terminating NUL: the preset's
name and folder, the four macros' labels, and ``settings``, which holds every
parameter's plain value under its index as a string ("106": 10.0 is band 1's
gain, as a linear factor) followed by the modulation and macro routings. The
VST2 chunk is the patch as it is; the VST3 holds it with JUCE's private-data
trailer inside a ``VstW`` bank carrying the VST2 id ``OhcS`` and version
0x10001, and writes no controller half.

The plugin spells a number the way JUCE's JSON writer does -- a number of
decimals that grows as the number shrinks, trailing zeros trimmed, an integral
one with ``.0``, outside 1e-5 to 1e6 an exponent with no padding
(``3.764955458507302e-9``) -- and indents by two spaces with CRLF line ends.
Written that way, every patch it saved re-serialises byte for byte, and a
patch built here comes back from it unchanged. It holds a few ranges' ends as
single-precision numbers and puts a value past one there on load, so a value
at such an end is written as that number.

``data/ohmicide-s-neutral.state`` is the plugin's own "Neutral" factory program:
four bands of no distortion, no modulation and no macro routing.
"""

from __future__ import annotations

import datetime
import functools
import json
import pathlib
import re
from collections.abc import Mapping
from typing import Any

from abletoolz.plugins.container.controller import NO_CONTROLLER_STATE
from abletoolz.plugins.identity.ids import Vst2Id, Vst3Id
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.framings import VERBATIM, JuceVstw
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import Layout, PluginFormat, PluginMap

OHMICIDE_S_VST2 = Vst2Id(0x4F686353)
OHMICIDE_S_VST3 = Vst3Id((1448301647, 1751339887, 1752000867, 1768187227))
_VERSION_CODE = 0x10001
DATA = pathlib.Path(__file__).with_name("data")
PRODUCT = "Ohmicide[S]"
_EXPONENT = re.compile(r"e\+?(-?)0*(\d)")
_TRAILING_ZEROS = re.compile(r"(\.\d*?)0+(?=e|$)")
# The decimals JUCE's JSON writer gives a number below each bound: 16 significant digits, at most 18 decimals.
_DECIMALS = ((1e-2, 18), (1e-1, 17), (1.0, 16), (1e1, 15), (1e2, 14), (1e3, 13), (1e4, 12), (1e5, 11))


@functools.cache
def baseline() -> bytes:
    """The plugin's own "Neutral" program, as its patch."""
    return (DATA / "ohmicide-s-neutral.state").read_bytes()


def _document(patch: bytes) -> dict[str, Any]:
    if not patch.endswith(b"}\x00"):
        raise StateTransformError(f"not an {PRODUCT} patch: it is not a JSON document and its NUL")
    try:
        document = json.loads(patch[:-1].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StateTransformError(f"not an {PRODUCT} patch: {error}") from None
    if not isinstance(document, dict) or not isinstance(document.get("settings"), dict):
        raise StateTransformError(f"not an {PRODUCT} patch: it has no settings")
    return document


def is_patch(patch: bytes) -> bool:
    """Whether ``patch`` is a JSON document with the plugin's settings."""
    try:
        _document(patch)
    except StateTransformError:
        return False
    return True


def settings(patch: bytes) -> dict[int, float]:
    """Every parameter's plain value in an Ohmicide[S] patch, by index."""
    stored = _document(patch)["settings"]
    return {int(key): float(value) for key, value in stored.items() if key.isdigit()}


def _number(value: float) -> str:
    """``value`` as JUCE's JSON writer spells it."""
    size = abs(value)
    if size >= 1e6 or (size <= 1e-5 and value):
        text = _EXPONENT.sub(r"e\1\2", f"{value:.15e}")
    elif value == int(value):
        return f"{value:.1f}"
    else:
        text = f"{value:.{next((places for bound, places in _DECIMALS if size < bound), 10)}f}"
    return _TRAILING_ZEROS.sub(lambda match: match[1] if len(match[1]) > 1 else ".0", text)


def _spell(value: object, depth: int = 0) -> str:
    """``value`` as the plugin writes JSON."""
    indent = "  " * depth
    if isinstance(value, dict):
        items = [f"{indent}  {json.dumps(key)}: {_spell(item, depth + 1)}" for key, item in value.items()]
        return "{\r\n" + ",\r\n".join(items) + f"\r\n{indent}}}" if items else "{}"
    if isinstance(value, list):
        items = [f"{indent}  {_spell(item, depth + 1)}" for item in value]
        return "[\r\n" + ",\r\n".join(items) + f"\r\n{indent}]" if items else "[]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return _number(value)
    if isinstance(value, int):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def spelled(patch: bytes) -> bytes:
    """``patch`` re-written in the plugin's own spelling; a patch the plugin saved comes back unchanged."""
    return _spell(_document(patch)).encode("utf-8") + b"\x00"


def with_settings(patch: bytes, changes: Mapping[int, float], *, name: str | None = None) -> bytes:
    """``patch`` with each indexed parameter set to its plain value, and renamed ``name`` when given.

    An index the patch does not hold is refused: the plugin saves every one it reads.
    """
    document = _document(patch)
    stored = document["settings"]
    for index, value in changes.items():
        if str(index) not in stored:
            raise StateTransformError(f"this {PRODUCT} patch holds no parameter {index}")
        stored[str(index)] = float(value)
    if name is not None:
        document["patch_name"] = name
    return _spell(document).encode("utf-8") + b"\x00"


@register
def ohmicide_s() -> PluginMap:
    measured = MeasuredState(StateRung.VERBATIM, (StateEvidence.HOSTED,), datetime.date(2026, 10, 31))
    framed = MeasuredState(StateRung.REFRAME, (StateEvidence.HOSTED,), datetime.date(2026, 10, 31))
    return PluginMap(
        id="ohm-force.ohmicide-s",
        name=PRODUCT,
        version="1",
        scope="1.0.1 in VST2 and VST3, a patch echoed by both and read back by its parameters",
        vendor="Ohm Force",
        vst2=PluginFormat(OHMICIDE_S_VST2, PRODUCT, VERBATIM, measured, plan_name="ohmicide_s_vst2"),
        vst3=PluginFormat(
            OHMICIDE_S_VST3,
            PRODUCT,
            JuceVstw(OHMICIDE_S_VST2.unique_id, _VERSION_CODE, own_bypass=True),
            framed,
            NO_CONTROLLER_STATE,
            plan_name="ohmicide_s_vst3",
        ),
        layouts=(Layout("a JSON document with its settings", is_patch),),
    )
