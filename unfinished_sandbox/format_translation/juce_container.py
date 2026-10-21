"""The envelope pedalboard's ``raw_state`` puts a VST3 plugin's state in.

pedalboard is a JUCE host, so ``raw_state`` is not the plugin's own state: it is
``AudioProcessor::copyXmlToBinary`` around the XML ``VST3PluginState`` document
JUCE's VST3 wrapper builds. Layout, measured on this machine:

    offset 0   4 bytes   magic 0x21324356 little-endian, so the ASCII "VC2!"
    offset 4   4 bytes   little-endian uint32, the UTF-8 length of the XML
    offset 8   n bytes   the XML
    offset 8+n 1 byte    a NUL terminator, not counted in the length

The XML is one element per VST3 interface::

    <VST3PluginState><IComponent>...</IComponent>
                     <IEditController>...</IEditController></VST3PluginState>

and each body is ``MemoryBlock::toBase64Encoding`` -- JUCE's own encoding, not
RFC 4648. It is the decimal byte count, a literal '.', then six-bit groups read
least-significant-bit-first out of the byte stream through the alphabet
``.A-Za-z0-9+``. The leading '.' in that alphabet is why a run of zero bytes
prints as a run of dots.

The point of all this is one equality: the bytes inside ``IComponent`` are the
bytes Ableton writes into a VST3 device's ``ProcessorState``. That is what lets
a set's saved patch be handed to a plugin outside Live -- and it is measured,
not assumed: ``prove_ground_truth`` in the rig injects a set-harvested
``ProcessorState`` and demands the plugin hand the same bytes back.
"""

from __future__ import annotations

import dataclasses
import re
import struct

# JUCE's AudioProcessor::magicXmlNumber.
MAGIC = 0x21324356
_HEADER = struct.Struct("<II")

_ALPHABET = ".ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+"
_VALUE = {character: index for index, character in enumerate(_ALPHABET)}

_ELEMENT = re.compile(r"<(?P<tag>IComponent|IEditController)>(?P<body>[^<]*)</(?P=tag)>")
_DOCUMENT = (
    '<?xml version="1.0" encoding="UTF-8"?> <VST3PluginState>'
    "<IComponent>{component}</IComponent>"
    "<IEditController>{controller}</IEditController>"
    "</VST3PluginState>"
)


class ContainerError(ValueError):
    """A ``raw_state`` blob that is not the JUCE VST3 envelope."""


@dataclasses.dataclass(frozen=True, slots=True)
class PluginState:
    """The two interface states a JUCE VST3 wrapper saves.

    ``component`` is the processor state -- the same bytes an ``.als``
    ``ProcessorState`` holds. ``controller`` is the editor's own state, which
    Ableton does not store at all and which the rig therefore carries over from
    the target's defaults rather than inventing.
    """

    component: bytes
    controller: bytes


def juce_base64_decode(text: str) -> bytes:
    """Undo ``MemoryBlock::toBase64Encoding``."""
    separator = text.find(".")
    if separator < 0:
        raise ContainerError("no '.' length separator in the JUCE base64 body")
    size = int(text[:separator])
    payload = text[separator + 1 :]
    out = bytearray(size)
    for position, character in enumerate(payload):
        value = _VALUE.get(character)
        if value is None:
            raise ContainerError(f"character {character!r} is not in JUCE's base64 alphabet")
        bit = position * 6
        index, offset = bit >> 3, bit & 7
        if index >= size:
            break
        out[index] |= (value << offset) & 0xFF
        if offset > 2 and index + 1 < size:
            out[index + 1] |= value >> (8 - offset)
    return bytes(out)


def juce_base64_encode(data: bytes) -> str:
    """``MemoryBlock::toBase64Encoding``, bit for bit."""
    size = len(data)
    characters = ((size << 3) + 5) // 6
    parts = [f"{size}."]
    for position in range(characters):
        bit = position * 6
        index, offset = bit >> 3, bit & 7
        if offset > 2:
            following = data[index + 1] if index + 1 < size else 0
            value = (data[index] >> offset) | (following << (8 - offset))
        else:
            value = data[index] >> offset
        parts.append(_ALPHABET[value & 0x3F])
    return "".join(parts)


def unwrap(raw_state: bytes) -> PluginState:
    """Read a pedalboard ``raw_state`` into its two interface states."""
    if len(raw_state) < _HEADER.size:
        raise ContainerError(f"only {len(raw_state)} bytes, too short for the header")
    magic, length = _HEADER.unpack_from(raw_state)
    if magic != MAGIC:
        raise ContainerError(f"magic is {magic:#010x}, not {MAGIC:#010x}")
    document = raw_state[_HEADER.size : _HEADER.size + length].decode("utf-8")
    bodies = {match["tag"]: match["body"] for match in _ELEMENT.finditer(document)}
    if "IComponent" not in bodies:
        raise ContainerError("no IComponent element in the VST3PluginState document")
    controller = bodies.get("IEditController", "")
    return PluginState(
        component=juce_base64_decode(bodies["IComponent"]),
        controller=juce_base64_decode(controller) if controller else b"",
    )


def wrap(state: PluginState) -> bytes:
    """Build a ``raw_state`` blob a JUCE host will accept."""
    document = _DOCUMENT.format(
        component=juce_base64_encode(state.component),
        controller=juce_base64_encode(state.controller),
    ).encode("utf-8")
    return _HEADER.pack(MAGIC, len(document)) + document + b"\x00"


def with_component(raw_state: bytes, component: bytes) -> bytes:
    """``raw_state`` with a different processor state and its own controller state.

    Swapping only the component is deliberate. An ``.als`` carries a
    ``ProcessorState`` and nothing else, so this is exactly the substitution
    Live performs when it reopens a set -- and keeping the target's own
    controller state means a rejection is the plugin refusing the patch, not the
    host handing it a controller blob from a plugin that never wrote one.
    """
    return wrap(dataclasses.replace(unwrap(raw_state), component=component))
