"""Ozone 8 Elements and Ozone 9: the JSON document inside iZotope's VST2 wrapper.

Both products save the same kind of patch. The VST2 chunk is the iZotope
wrapper (:func:`~abletoolz.plugins.state.framings.izotope_unwrap`); the state
inside it is four little-endian uint32 -- a product word, a format word, the
length of everything after that word, and the raw length -- and then zlib of a
JSON document. Every
setting in the document is a ``{"Type": ..., "Value": ...}`` node, and
dictionaries nest the same way. Ozone 9 keeps each module under its own name in
``DSP Elements``; Ozone 8 Elements keeps its modules' settings in the
``ElementChain`` element, each name prefixed with its module's name. Both list
the signal chain in the ``ElementChain`` element's ``Extra Bytes``: per module a
zero byte, a uint32 length and the module's name.

Ozone 9 writes the document indented by three spaces, ``" : "`` between a key and
its value, floats as ``%.17g`` and a newline at the end, compressed at zlib's
default level; :meth:`OzoneDocument.encode` writes the same bytes, so a document
Ozone 9 saved encodes back to exactly what it saved.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime
import hashlib
import json
import struct
import zlib
from functools import cache
from pathlib import Path
from typing import Any

from abletoolz.plugins.identity.ids import Vst2Id, Vst3Id
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.framings import VERBATIM, izotope_unwrap
from abletoolz.plugins.state.izotope import NESTING_MEASURED, WRAPPED, IzotopeVst3
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import Layout, PluginFormat, PluginMap

OZONE_9_PRODUCT = 0x688ADE
OZONE_8_ELEMENTS_PRODUCT = 0x6895B1
_HEADER = struct.Struct("<4I")
_U32 = struct.Struct("<I")

# Ozone 9's default patch, saved by the installed VST2 with nothing loaded into it.
_DEFAULT_SHA256 = "d2111dd2d3f7438a240083132830c24e1c1b53eae256455eee1968b3ead26650"

type Node = dict[str, Any]


@dataclasses.dataclass(frozen=True, slots=True)
class OzoneDocument:
    """One Ozone patch: the product and format words, the JSON document and the preset name."""

    product: int
    format: int
    tree: Node
    name: bytes

    @classmethod
    def parse(cls, chunk: bytes) -> OzoneDocument:
        state = izotope_unwrap(chunk)
        if state is None or len(state) < _HEADER.size:
            raise StateTransformError(f"A {len(chunk)} byte chunk is not an iZotope VST2 wrapper")
        product, form, rest, raw = _HEADER.unpack_from(state)
        if 12 + rest != len(state):
            raise StateTransformError("The Ozone state's compressed length does not account for it")
        try:
            text = zlib.decompress(state[_HEADER.size :])
        except zlib.error as error:
            raise StateTransformError(f"The Ozone state does not decompress: {error}") from error
        if len(text) != raw:
            raise StateTransformError("The Ozone state's raw length does not match its document")
        try:
            tree = json.loads(text)
        except ValueError as error:
            raise StateTransformError(f"The Ozone state is not a JSON document: {error}") from error
        if not isinstance(tree, dict):
            raise StateTransformError("The Ozone state's document is not a dictionary")
        name = chunk[4 + len(state) + 4 :]
        return cls(product, form, tree, name)

    def encode(self) -> bytes:
        text = (_dumps(self.tree) + "\n").encode("utf-8")
        body = zlib.compress(text)
        state = _HEADER.pack(self.product, self.format, 4 + len(body), len(text)) + body
        return _U32.pack(len(state)) + state + _U32.pack(len(self.name)) + self.name

    def elements(self) -> Node:
        """``DSP Elements``: each module's settings by the module's name."""
        return _dictionary(_dictionary(self.tree, "DSP State"), "DSP Elements")

    def element(self, name: str) -> Node:
        return _dictionary(self.elements(), name)

    def chain(self) -> tuple[str, ...]:
        """The modules in the signal chain, in order."""
        data = base64.b64decode(_leaf(self.element("ElementChain"), "Extra Bytes", "Base64"))
        names: list[str] = []
        offset = 0
        while offset < len(data):
            if data[offset] != 0 or offset + 5 > len(data):
                raise StateTransformError("The Ozone signal chain is not a list of module names")
            (length,) = _U32.unpack_from(data, offset + 1)
            names.append(data[offset + 5 : offset + 5 + length].decode("utf-8"))
            offset += 5 + length
        return tuple(names)


def chain_bytes(names: tuple[str, ...]) -> str:
    """The ``ElementChain`` element's ``Extra Bytes`` for a signal chain of ``names``."""
    data = b"".join(b"\0" + _U32.pack(len(raw)) + raw for raw in (name.encode("utf-8") for name in names))
    return base64.b64encode(data).decode("ascii")


def leaves(element: Node) -> Node:
    """An element's settings: each ``{"Type", "Value"}`` node by its name."""
    return {key: node for key, node in element.items() if isinstance(node, dict) and node.get("Type") != "Dictionary"}


def _dictionary(parent: Node, key: str) -> Node:
    node = parent.get(key)
    if not isinstance(node, dict) or node.get("Type") != "Dictionary" or not isinstance(node.get("Value"), dict):
        raise StateTransformError(f"The Ozone document has no {key!r} dictionary")
    value: Node = node["Value"]
    return value


def _leaf(parent: Node, key: str, kind: str) -> Any:
    node = parent.get(key)
    if not isinstance(node, dict) or node.get("Type") != kind:
        raise StateTransformError(f"The Ozone document has no {kind} {key!r}")
    return node["Value"]


def _dumps(node: object, depth: int = 0) -> str:
    """JSON the way Ozone 9 writes it."""
    pad = "   " * (depth + 1)
    if isinstance(node, dict):
        if not node:
            return "{}"
        items = (
            f"{pad}{json.dumps(key, ensure_ascii=False)} : {_dumps(value, depth + 1)}" for key, value in node.items()
        )
        return "{\n" + ",\n".join(items) + "\n" + "   " * depth + "}"
    if isinstance(node, list):
        if not node:
            return "[]"
        return "[\n" + ",\n".join(f"{pad}{_dumps(value, depth + 1)}" for value in node) + "\n" + "   " * depth + "]"
    if isinstance(node, bool):
        return "true" if node else "false"
    if isinstance(node, float):
        return f"{node:.17g}"
    return json.dumps(node, ensure_ascii=False)


@cache
def _default_chunk() -> bytes:
    raw = (Path(__file__).with_name("data") / "ozone-9-default.state").read_bytes()
    if hashlib.sha256(raw).hexdigest() != _DEFAULT_SHA256:
        raise StateTransformError("Ozone 9 default patch hash mismatch")
    return raw


def ozone_9_default() -> OzoneDocument:
    """Ozone 9's own default patch, a fresh copy each call."""
    return OzoneDocument.parse(_default_chunk())


def _is_product(product: int) -> Layout:
    def matches(patch: bytes) -> bool:
        state = izotope_unwrap(patch)
        return state is not None and len(state) >= _HEADER.size and _U32.unpack_from(state)[0] == product

    return Layout(f"iZotope wrapper, product word {product:#x}", matches)


# The day the installed Ozone 9 read back converted Ozone 8 Elements patches.
OZONE_MEASURED = MeasuredState(StateRung.VERBATIM, (StateEvidence.HOSTED,), datetime.date(2026, 10, 31))


@register
@cache
def ozone_9() -> PluginMap:
    return PluginMap(
        id="izotope.ozone-9",
        name="Ozone 9",
        version="9",
        scope="VST2 wrapper around a zlib JSON document; VST3 controller half not measured",
        vendor="iZotope",
        vst2=PluginFormat(Vst2Id(1517178681), "Ozone 9", VERBATIM, OZONE_MEASURED, plan_name="ozone_9_vst2"),
        vst3=PluginFormat(
            Vst3Id((1448301658, 1850685775, 2054123109, 540606464)),
            "Ozone 9",
            IzotopeVst3(),
            NESTING_MEASURED,
            plan_name="ozone_9_vst3",
        ),
        layouts=(WRAPPED,),
    )


@register
@cache
def ozone_8_elements() -> PluginMap:
    return PluginMap(
        id="izotope.ozone-8-elements",
        name="Ozone 8 Elements",
        version="8",
        scope="VST2 wrapper around a zlib JSON document, modules flattened into the element chain",
        vendor="iZotope",
        vst2=PluginFormat(
            Vst2Id(1517176172),
            "Ozone 8 Elements",
            VERBATIM,
            MeasuredState(StateRung.VERBATIM, (StateEvidence.STRUCTURAL,)),
            plan_name="ozone_8_elements_vst2",
        ),
        layouts=(_is_product(OZONE_8_ELEMENTS_PRODUCT),),
    )
