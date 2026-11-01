"""AAS Lounge Lizard EP-4: its saved patch, read by control in the engine's own units.

Lounge Lizard EP-4 is a VST2 whose state is the engine's own text, a Lua table:
the tuning (a Scala file), the MIDI map, the program and bank names, and a
``parameters`` table of three strings -- ``version``, a ``schema`` naming every
control under its section (``{"Mallet"},"Stiffness",...``) and the ``values``
in the same order. The values are what the engine plays, in its units: hertz,
seconds, linear gains and depths. A switch or a choice holds a 1-based item
index (``On`` is 1 off, 2 on).

The schema and units were measured by hosting the 4.0.3 engine's x86 build: 82
host controls, 77 of them reloading exactly the values written here, and its
displays name the items of the choices read below (``Tremolo/Shape`` Tri, Sqr;
``Tremolo/Stereo`` Mono, Stereo; the effect types and reverb sizes). Only
version 6 tables written by Lounge Lizard EP-4 are read.
"""

from __future__ import annotations

import dataclasses
import json
import math
import re
import zlib
from xml.etree import ElementTree as ET

from abletoolz.plugins.identity.ids import Vst2Id
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.framings import VERBATIM
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import Layout, PluginFormat, PluginMap

LOUNGE_LIZARD_EP4_VST2 = Vst2Id(1280072244)
PRODUCT = "Lounge Lizard EP-4"
MAX_STATE_BYTES = 1024 * 1024

# The effect slots' items, by the 1-based index the engine stores; 6 was not seen in a display.
EFFECT_TYPES = {1: "Delay", 2: "Distort", 3: "Phaser", 4: "Chorus", 5: "Flanger", 7: "Wah Wah", 8: "Notch"}

_TABLE = re.compile(rb"\n\tparameters = \{\r?\n(?P<body>.*?)\r?\n\t\},\r?\n\}\s*\x00?\Z", re.DOTALL)
_STRING = re.compile(rb'^\t\t(version|schema|values) = ("(?:[^"\\\r\n]|\\.)*"),\r?$', re.MULTILINE)
_NAME = re.compile(rb'^\tprogramName = ("(?:[^"\\\r\n]|\\.)*"),\r?$', re.MULTILINE)


def _array(text: str) -> list[object]:
    """A Lua array literal of scalars and nested arrays, read as JSON (``{}`` outside strings become ``[]``)."""
    out, quoted, escaped = [], False, False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            out.append(char)
        else:
            quoted = char == '"'
            out.append({"{": "[", "}": "]"}.get(char, char))
    try:
        value = json.loads("".join(out))
    except ValueError as error:
        raise StateTransformError(f"not a {PRODUCT} parameters table: {error}") from None
    if not isinstance(value, list):
        raise StateTransformError(f"not a {PRODUCT} parameters table")
    return value


def _strings(state: bytes) -> dict[str, str]:
    if not 0 < len(state) <= MAX_STATE_BYTES:
        raise StateTransformError(f"a {PRODUCT} state is not {len(state)} bytes long")
    block = _TABLE.search(state)
    if block is None:
        raise StateTransformError(f"not a {PRODUCT} state: no parameters table at its end")
    found: dict[str, str] = {}
    for match in _STRING.finditer(block["body"]):
        found[match[1].decode("ascii")] = json.loads(match[2])
    if found.keys() != {"version", "schema", "values"}:
        raise StateTransformError(f"not a {PRODUCT} state: its parameters table holds {sorted(found)}")
    return found


def values(state: bytes) -> dict[str, float | str]:
    """Every control the patch holds, as ``"Section/Name"``, in the engine's units; an unused slot reads ``"none"``."""
    strings = _strings(state)
    version = _array(strings["version"])
    if len(version) != 3 or version[0] != 6 or version[1] != PRODUCT:
        raise StateTransformError(f"not a {PRODUCT} version 6 parameters table: {version}")
    out: dict[str, float | str] = {}
    section = ""
    names: list[str] = []
    for item in _array(strings["schema"]):
        if isinstance(item, list) and len(item) == 1 and isinstance(item[0], str):
            section = item[0]
        elif isinstance(item, str) and section:
            names.append(f"{section}/{item}")
        else:
            raise StateTransformError(f"a {PRODUCT} schema entry is {item!r}")
    stored = _array(strings["values"])
    if len(stored) != len(names) or len(set(names)) != len(names):
        raise StateTransformError(f"a {PRODUCT} table names {len(names)} controls and holds {len(stored)} values")
    for name, value in zip(names, stored, strict=True):
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise StateTransformError(f"{name} holds {value!r}")
        if isinstance(value, float) and not math.isfinite(value):
            raise StateTransformError(f"{name} holds {value}")
        out[name] = float(value) if isinstance(value, (int, float)) else value
    return out


def program_name(state: bytes) -> str | None:
    """The program name the patch was saved under, or None when it holds none."""
    match = _NAME.search(state)
    name = None if match is None else json.loads(match[1])
    return name if isinstance(name, str) else None


def _is_state(state: bytes) -> bool:
    try:
        values(state)
    except StateTransformError:
        return False
    return True


@register
def lounge_lizard_ep4() -> PluginMap:
    measured = MeasuredState(StateRung.VERBATIM, (StateEvidence.STRUCTURAL,))
    return PluginMap(
        id="aas.lounge-lizard-ep-4",
        name=PRODUCT,
        version="4",
        scope="4.0.3 VST2; the engine's Lua table, read only",
        vendor="Applied Acoustics Systems",
        vst2=PluginFormat(LOUNGE_LIZARD_EP4_VST2, PRODUCT, VERBATIM, measured, plan_name="lounge_lizard_ep4_vst2"),
        layouts=(Layout("version 6 parameters table", _is_state),),
    )


# -- Lounge Lizard 3 -----------------------------------------------------------

LOUNGE_LIZARD_3_VST2 = Vst2Id(1280080435)
PRODUCT_3 = "Lounge Lizard 3"
_AASZ_HEADER = 16
_AASZ = re.compile(rb"AASZ [0-9]{1,7} +")
_MAX_INFLATED = 4 * 1024 * 1024
_MODULE = re.compile(r"(\S+)\s+(\S+)\s+version\s+([0-9]+)")


@dataclasses.dataclass(frozen=True, slots=True)
class Lounge3Patch:
    """The patch a Lounge Lizard 3 device plays: the bank's selected program name and its working fields."""

    program: str | None
    fields: dict[str, str]


def _lounge3_fields(text: str) -> dict[str, str]:
    """``"type:instance/field"`` to the raw value, for every module of a program's text (module names are unique)."""
    fields: dict[str, str] = {}
    depth, pending, module = 0, None, [""]
    for line in (row.strip() for row in text.splitlines()):
        if not line:
            continue
        header = _MODULE.fullmatch(line)
        if line == "{" and pending is not None:
            module.append(pending)
            depth, pending = depth + 1, None
        elif line == "}" and depth and pending is None:
            module.pop()
            depth -= 1
        elif header is not None and pending is None:
            pending = f"{header[1]}:{header[2]}"
        elif depth and pending is None and len(parts := line.split(None, 1)) == 2:
            key = f"{module[-1]}/{parts[0]}"
            if key in fields:
                raise StateTransformError(f"a {PRODUCT_3} program sets {key} twice")
            fields[key] = parts[1]
        else:
            raise StateTransformError(f"a {PRODUCT_3} program has a line {line[:40]!r} out of place")
    if depth or pending is not None or not fields:
        raise StateTransformError(f"a {PRODUCT_3} program's modules do not close")
    return fields


def lounge3(state: bytes) -> Lounge3Patch:
    """The patch inside a Lounge Lizard 3 state: ``AASZ``, the inflated length, then its zlib-compressed bank XML.

    The device plays the document's ``locked_data``, which is the bank's selected program as last edited.
    """
    if _AASZ.fullmatch(state[:_AASZ_HEADER]) is None:
        raise StateTransformError(f"not a {PRODUCT_3} state")
    declared = int(state[5:_AASZ_HEADER])
    stream = zlib.decompressobj()
    try:
        inflated = stream.decompress(state[_AASZ_HEADER:], _MAX_INFLATED + 1)
    except zlib.error as error:
        raise StateTransformError(f"a {PRODUCT_3} state does not inflate: {error}") from None
    if len(inflated) != declared or not stream.eof or not inflated.endswith(b"\x00") or b"<!ENTITY" in inflated:
        raise StateTransformError(f"a {PRODUCT_3} state is not the {declared} bytes of XML it declares")
    try:
        root = ET.fromstring(inflated[:-1])
    except ET.ParseError as error:
        raise StateTransformError(f"a {PRODUCT_3} state's XML does not parse: {error}") from None
    bank, locked = root.find("mpv-midi-bank"), root.find("document/locked_data")
    if root.tag != "mpv-state" or bank is None or locked is None or not locked.text:
        raise StateTransformError(f"a {PRODUCT_3} state holds no bank or working patch")
    selected = bank.find(f"mpv-midi-program[@number='{bank.get('current-program')}']")
    return Lounge3Patch(None if selected is None else selected.get("name"), _lounge3_fields(locked.text))


def _is_lounge3(state: bytes) -> bool:
    try:
        lounge3(state)
    except StateTransformError:
        return False
    return True


@register
def lounge_lizard_3() -> PluginMap:
    measured = MeasuredState(StateRung.VERBATIM, (StateEvidence.STRUCTURAL,))
    return PluginMap(
        id="aas.lounge-lizard-3",
        name=PRODUCT_3,
        version="3",
        scope="VST2; its compressed bank XML, the working patch read only",
        vendor="Applied Acoustics Systems",
        vst2=PluginFormat(
            LOUNGE_LIZARD_3_VST2, "LoungeLizard3Vst", VERBATIM, measured, plan_name="lounge_lizard_3_vst2"
        ),
        layouts=(Layout("AASZ bank", _is_lounge3),),
    )
