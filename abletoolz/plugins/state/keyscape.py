"""Spectrasonics Keyscape: its state, framed for both formats, and the controls of its first part edited.

Keyscape saves one text document, ``<SynthMaster ...>``, holding the multi and
its parts; each part is a ``<SynthSubEngine>`` holding the patch it plays. The
VST2 chunk is that text with a closing NUL. The VST3 processor state is the
same text behind a 24-byte header -- four u32 (999999999, 0, 1, 0) and the
text's length as a u64 -- and four zero bytes after it; the controller state
is empty (both measured on the devices Live wrote and on the installed
plugin's own saves).

A patch's controls (the knobs on its MAIN, TONE and EFFECTS pages) are
``Custom<n>`` elements; each control drives attributes elsewhere in the part,
the attribute naming it in ``<attr>_Cstm_<slot>_="<n>"`` beside the points of
its curve and, for a choice, one switch value per item
(``<attr>_Cstm_<slot>_SV<i>``). Every number is a big-endian float32 in hex,
``0`` for zero. Measured on the installed 2.2 build: the sound follows the
attribute values, and the knob a part shows, ``Custom_<n>`` on its
``SYNTHENG``, is read back from the first attribute the control drives, so a
knob written without its attributes is undone on load. :func:`edit` therefore
writes attributes, by the control that drives them, and the knob with them.

``data/keyscape-rhodes-classic.state.gz`` is the installed plugin's own state
holding its factory patch "Rhodes - Classic" in the first part, as it saves it
(VST2 form); the plugin reads it back byte for byte.
"""

from __future__ import annotations

import dataclasses
import datetime
import functools
import gzip
import pathlib
import re
import struct
from collections.abc import Callable

from abletoolz.plugins.container.controller import NO_CONTROLLER_STATE
from abletoolz.plugins.identity.ids import Vst2Id, Vst3Id
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.framings import VERBATIM
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import Layout, PluginFormat, PluginMap

KEYSCAPE_VST2 = Vst2Id(1265857646)
KEYSCAPE_VST3 = Vst3Id((-2065113505, -1839910607, -1761942509, 1016289816))
PRODUCT = "Keyscape"
DATA = pathlib.Path(__file__).with_name("data")

_VST3_HEADER = struct.Struct("<4IQ")
_VST3_WORDS = (999999999, 0, 1, 0)
_VST3_TRAILER = b"\x00" * 4
_PART_OPEN, _PART_CLOSE = "<SynthSubEngine >", "</SynthSubEngine>"
_ELEMENT = re.compile(r"<(\w+)\s([^<>]*)>")
_ATTRIBUTE = re.compile(r'(\w+)="([^"]*)"')

type Rewrite = Callable[[str, tuple[tuple[float, float], ...], tuple[float, ...]], float | None]
"""Given an attribute a control drives, its curve points and switch values: the value to write, or None to keep it."""


@functools.cache
def rhodes_classic() -> bytes:
    """The installed plugin's own state with its factory "Rhodes - Classic" in the first part, VST2 form."""
    return gzip.decompress((DATA / "keyscape-rhodes-classic.state.gz").read_bytes())


def _hex(value: float) -> str:
    return "0" if value == 0 else struct.pack(">f", value).hex().lstrip("0")


def _float(text: str) -> float:
    try:
        return float(struct.unpack(">f", bytes.fromhex(text.zfill(8)))[0])
    except ValueError:
        raise StateTransformError(f"a {PRODUCT} number reads {text!r}") from None


def _text(patch: bytes) -> str:
    if not (patch.startswith(b"<SynthMaster ") and patch.endswith(b"\x00")):
        raise StateTransformError(f"not a {PRODUCT} state")
    return patch[:-1].decode("utf-8")


def _first_part(text: str) -> tuple[int, int]:
    start = text.find(_PART_OPEN)
    end = text.find(_PART_CLOSE, start)
    if start < 0 or end < 0:
        raise StateTransformError(f"this {PRODUCT} state holds no part")
    return start, end


def knobs(patch: bytes) -> dict[int, float]:
    """The first part's knobs as it shows them, by control number."""
    text = _text(patch)
    start, end = _first_part(text)
    return {int(n): _float(v) for n, v in re.findall(r'\sCustom_(\d+)="([0-9a-f]+)"', text[start:end])}


def driven(patch: bytes, control: int) -> list[tuple[str, float]]:
    """Every attribute ``control`` drives in the first part, with its value, in document order."""
    text = _text(patch)
    start, end = _first_part(text)
    found: list[tuple[str, float]] = []
    for element in _ELEMENT.finditer(text[start:end]):
        attributes = dict(_ATTRIBUTE.findall(element[2]))
        found.extend((attribute, _float(attributes[attribute])) for attribute, _, _ in _targets(attributes, control))
    return found


def _targets(
    attributes: dict[str, str], control: int
) -> list[tuple[str, tuple[tuple[float, float], ...], tuple[float, ...]]]:
    out = []
    for key, value in attributes.items():
        named = re.fullmatch(r"(\w+)_Cstm_(\d+)_", key)
        if named is None or value != str(control):
            continue
        attribute, slot = named[1], named[2]
        if attribute not in attributes:
            raise StateTransformError(f"{PRODUCT} control {control} drives {attribute}, which the element lacks")
        points: list[tuple[float, float]] = []
        switches: list[float] = []
        while f"{attribute}_Cstm_{slot}_{len(points)}X" in attributes:
            prefix = f"{attribute}_Cstm_{slot}_{len(points)}"
            points.append((_float(attributes[prefix + "X"]), _float(attributes[prefix + "Y"])))
        while f"{attribute}_Cstm_{slot}_SV{len(switches)}" in attributes:
            switches.append(_float(attributes[f"{attribute}_Cstm_{slot}_SV{len(switches)}"]))
        out.append((attribute, tuple(points), tuple(switches)))
    return out


def edit(patch: bytes, control: int, rewrite: Rewrite) -> bytes:
    """``patch`` with each attribute ``control`` drives in the first part rewritten, and its knob with them.

    The knob is written as Keyscape reads it back: the value of the first attribute the control drives.
    Refused when the control drives nothing there, which is a patch other than the one the caller measured.
    """
    text = _text(patch)
    start, end = _first_part(text)
    part = text[start:end]
    pieces: list[str] = []
    last = 0
    first: float | None = None
    for element in _ELEMENT.finditer(part):
        body = element[2]
        attributes = dict(_ATTRIBUTE.findall(body))
        for attribute, points, switches in _targets(attributes, control):
            value = rewrite(attribute, points, switches)
            if first is None:
                first = _float(attributes[attribute]) if value is None else value
            if value is not None:
                body = re.sub(rf'(?<=\s){attribute}="[0-9a-f]+"', f'{attribute}="{_hex(value)}"', body, count=1)
        pieces.append(part[last : element.start(2)] + body)
        last = element.end(2)
    pieces.append(part[last:])
    if first is None:
        raise StateTransformError(f"{PRODUCT} control {control} drives nothing in this patch")
    part, count = re.subn(
        rf'(?<=\s)Custom_{control}="[0-9a-f]+"', f'Custom_{control}="{_hex(first)}"', "".join(pieces), count=1
    )
    if count != 1:
        raise StateTransformError(f"this {PRODUCT} part has no knob {control}")
    return (text[:start] + part + text[end:]).encode("utf-8") + b"\x00"


def choose(patch: bytes, control: int, item: int) -> bytes:
    """Select item ``item`` of a choice control: each attribute it drives takes its switch value for that item."""

    def switch(attribute: str, _points: tuple[tuple[float, float], ...], switches: tuple[float, ...]) -> float:
        if not 0 <= item < len(switches):
            raise StateTransformError(f"{PRODUCT} control {control} has no item {item} for {attribute}")
        return switches[item]

    return edit(patch, control, switch)


@dataclasses.dataclass(frozen=True, slots=True)
class KeyscapeVst3:
    """The VST3 processor state: the VST2 chunk behind its 24-byte header, four zero bytes after it."""

    def lift(self, state: bytes) -> bytes:
        if len(state) < _VST3_HEADER.size + len(_VST3_TRAILER):
            raise StateTransformError(f"a {PRODUCT} VST3 state is longer than {len(state)} bytes")
        *words, length = _VST3_HEADER.unpack_from(state)
        if tuple(words) != _VST3_WORDS or len(state) != _VST3_HEADER.size + length + len(_VST3_TRAILER):
            raise StateTransformError(f"not a {PRODUCT} VST3 state")
        if not state.endswith(_VST3_TRAILER):
            raise StateTransformError(f"a {PRODUCT} VST3 state ends in four zero bytes")
        return state[_VST3_HEADER.size : _VST3_HEADER.size + length]

    def lower(self, patch: bytes) -> bytes:
        return _VST3_HEADER.pack(*_VST3_WORDS, len(patch)) + patch + _VST3_TRAILER


def _is_state(patch: bytes) -> bool:
    return patch.startswith(b"<SynthMaster ") and patch.endswith(b"\x00")


@register
def keyscape() -> PluginMap:
    measured = MeasuredState(StateRung.VERBATIM, (StateEvidence.HOSTED,), datetime.date(2026, 10, 31))
    framed = MeasuredState(StateRung.REFRAME, (StateEvidence.HOSTED,), datetime.date(2026, 10, 31))
    return PluginMap(
        id="spectrasonics.keyscape",
        name=PRODUCT,
        version="2",
        scope="2.2 VST2 and VST3; the SynthMaster text, its first part's controls edited",
        vendor="Spectrasonics",
        vst2=PluginFormat(KEYSCAPE_VST2, PRODUCT, VERBATIM, measured, plan_name="keyscape_vst2"),
        vst3=PluginFormat(
            KEYSCAPE_VST3, PRODUCT, KeyscapeVst3(), framed, NO_CONTROLLER_STATE, plan_name="keyscape_vst3"
        ),
        layouts=(Layout("SynthMaster text", _is_state),),
    )
