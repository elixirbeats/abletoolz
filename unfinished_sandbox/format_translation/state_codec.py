"""Decode editable state fields through nested host and serialization envelopes.

Unknown bodies remain opaque. An unchanged view returns the original bytes;
editing a field rebuilds its enclosing lengths and preserves unrelated payloads.
"""

from __future__ import annotations

import gzip
import io
import json
import math
import re
import struct
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from arturia_archive import single_float_map
from juce_container import juce_base64_decode, juce_base64_encode

type Scalar = str | int | float | bool | None
MAX_BYTES = 16 * 1024 * 1024
MAX_DEPTH = 10


@dataclass(frozen=True)
class View:
    fields: Mapping[str, Scalar]
    layers: tuple[str, ...]
    encode: Callable[[Mapping[str, Scalar]], bytes]


def number(value: Scalar) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int) and abs(value) > 2**53:
        return None
    try:
        converted = float(value)
    except ValueError:
        return None
    return converted if math.isfinite(converted) else None


def equivalent(left: Scalar, right: Scalar) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, int) and isinstance(right, int):
        return left == right
    if (
        isinstance(left, str)
        and isinstance(right, str)
        and re.fullmatch(r"[+-]?\d{1,64}", left)
        and re.fullmatch(r"[+-]?\d{1,64}", right)
    ):
        return int(left) == int(right)
    a, b = number(left), number(right)
    if a is not None and b is not None:
        return math.isclose(a, b, abs_tol=1e-6, rel_tol=2e-7)
    return left == right


def _join(raw: bytes, children: Mapping[str, View], build: Callable[[dict[str, bytes]], bytes], layer: str) -> View:
    fields = {f"{name}/{key}": value for name, child in children.items() for key, value in child.fields.items()}

    def encode(edits: Mapping[str, Scalar]) -> bytes:
        if not edits:
            return raw
        unknown = edits.keys() - fields.keys()
        if unknown:
            raise ValueError(f"Unknown fields: {sorted(unknown)}")
        return build(
            {
                name: child.encode(
                    {key[len(name) + 1 :]: value for key, value in edits.items() if key.startswith(name + "/")}
                )
                for name, child in children.items()
            }
        )

    layers = (layer, *(f"{name}/{part}" for name, child in children.items() for part in child.layers))
    return View(fields, layers, encode)


def _floats(raw: bytes, offset: int, count: int, endian: str, label: str) -> View:
    if count < 0 or offset + count * 4 > len(raw):
        raise ValueError("Float array runs beyond saved state")
    values = struct.unpack_from(f"{endian}{count}f", raw, offset)
    fields: dict[str, Scalar] = {f"float[{i}]": float(value) for i, value in enumerate(values)}

    def encode(edits: Mapping[str, Scalar]) -> bytes:
        out = bytearray(raw)
        for key, value in edits.items():
            if key not in fields or number(value) is None:
                raise ValueError(f"Invalid float edit {key}: {value}")
            index = int(key[6:-1])
            struct.pack_into(endian + "f", out, offset + index * 4, number(value))
        return bytes(out)

    return View(fields, (label,), encode)


def _xml_fields(root: ET.Element) -> tuple[dict[str, Scalar], dict[str, tuple[ET.Element, str | None]]]:
    fields: dict[str, Scalar] = {}
    locations: dict[str, tuple[ET.Element, str | None]] = {}

    def visit(element: ET.Element, path: str) -> None:
        for key, value in element.attrib.items():
            field = f"{path}/@{key}"
            fields[field] = value
            locations[field] = (element, key)
        if element.text and element.text.strip():
            fields[path + "/text()"] = element.text.strip()
            locations[path + "/text()"] = (element, None)
        for index, child in enumerate(element):
            identity = child.get("id", child.get("name", str(index)))
            visit(child, f"{path}/{child.tag}[{index}:{identity}]")

    visit(root, str(root.tag))
    return fields, locations


def _xml(raw: bytes) -> View:
    fields, _ = _xml_fields(ET.fromstring(raw.rstrip(b"\x00")))

    def encode(edits: Mapping[str, Scalar]) -> bytes:
        if not edits:
            return raw
        if edits.keys() - fields.keys():
            raise ValueError("XML edit names a missing field")
        root = ET.fromstring(raw.rstrip(b"\x00"))
        _, locations = _xml_fields(root)
        for key, value in edits.items():
            element, attribute = locations[key]
            if attribute is None:
                element.text = str(value)
            else:
                element.set(attribute, str(value))
        result: bytes = ET.tostring(root, encoding="utf-8")
        return result

    return View(fields, ("XML",), encode)


def _json(raw: bytes) -> View:
    # JSON.parse is the serialization boundary; leaves are checked before exposure.
    document: object = json.loads(raw)
    fields: dict[str, Scalar] = {}

    def visit(value: object, path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                visit(item, path + "/" + str(key).replace("~", "~0").replace("/", "~1"))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, path + "/" + str(index))
        elif value is None or isinstance(value, (str, int, float, bool)):
            fields[path] = value

    visit(document, "")

    def encode(edits: Mapping[str, Scalar]) -> bytes:
        if not edits:
            return raw
        updated: object = json.loads(raw)
        for path, value in edits.items():
            if path not in fields or not path:
                raise ValueError("JSON edit names a missing/non-container field")
            components = [key.replace("~1", "/").replace("~0", "~") for key in path.split("/")[1:]]
            parent = updated
            for key in components[:-1]:
                if isinstance(parent, list):
                    parent = parent[int(key)]
                elif isinstance(parent, dict):
                    parent = parent[key]
                else:
                    raise ValueError("JSON path crosses a scalar")
            if isinstance(parent, list):
                parent[int(components[-1])] = value
            elif isinstance(parent, dict):
                parent[components[-1]] = value
        return json.dumps(updated, separators=(",", ":"), allow_nan=False).encode()

    return View(fields, ("JSON",), encode)


def decode(raw: bytes, depth: int = 0, *, experimental_arturia: bool = False) -> View:
    if len(raw) > MAX_BYTES or depth > MAX_DEPTH:
        raise ValueError("State exceeds decoder byte/depth budget")

    def child(data: bytes) -> View:
        return decode(data, depth + 1, experimental_arturia=experimental_arturia)

    if raw.startswith(b"VC2!"):
        if len(raw) < 8:
            raise ValueError("Truncated JUCE header")
        size = struct.unpack_from("<I", raw, 4)[0]
        if 8 + size > len(raw):
            raise ValueError("JUCE XML length exceeds buffer")
        payload, trailer = raw[8 : 8 + size], raw[8 + size :]
        root = ET.fromstring(payload)
        if root.tag == "VST3PluginState":
            children = {
                element.tag: child(juce_base64_decode(element.text or "0."))
                for element in root
                if element.tag in ("IComponent", "IEditController")
            }

            def build_vst3(parts: dict[str, bytes]) -> bytes:
                copy = ET.fromstring(payload)
                for child in copy:
                    if child.tag in parts:
                        child.text = juce_base64_encode(parts[child.tag])
                xml: bytes = ET.tostring(copy, encoding="utf-8")
                return b"VC2!" + struct.pack("<I", len(xml)) + xml + trailer

            return _join(raw, children, build_vst3, "JUCE VST3")
        return _join(
            raw,
            {"xml": _xml(payload)},
            lambda parts: b"VC2!" + struct.pack("<I", len(parts["xml"])) + parts["xml"] + trailer,
            "JUCE XML",
        )
    if raw.startswith(b"VstW") and len(raw) >= 16:
        return _join(raw, {"bank": child(raw[16:])}, lambda parts: raw[:16] + parts["bank"], "VstW")
    if raw.startswith(b"CcnK") and len(raw) >= 160 and raw[8:12] == b"FBCh":
        size = struct.unpack_from(">I", raw, 156)[0]
        if 160 + size > len(raw):
            raise ValueError("FXB chunk length exceeds buffer")

        def build_fxb(parts: dict[str, bytes]) -> bytes:
            header = bytearray(raw[:160])
            delta = len(parts["chunk"]) - size
            byte_count = struct.unpack_from(">I", header, 4)[0]
            if byte_count:
                struct.pack_into(">I", header, 4, byte_count + delta)
            struct.pack_into(">I", header, 156, len(parts["chunk"]))
            return bytes(header) + parts["chunk"] + raw[160 + size :]

        return _join(raw, {"chunk": child(raw[160 : 160 + size])}, build_fxb, "FXB chunk")
    if raw.startswith(b"CcnK") and raw[8:12] == b"FxBk":
        if len(raw) < 156:
            raise ValueError("Truncated parameter bank")
        programs = struct.unpack_from(">I", raw, 24)[0]
        if not 1 <= programs <= 4096:
            raise ValueError("Parameter bank program count exceeds budget")
        start = 156
        children = {}
        for index in range(programs):
            if raw[start : start + 4] != b"CcnK" or raw[start + 8 : start + 12] != b"FxCk":
                raise ValueError("Parameter bank lacks FxCk program")
            if start + 56 > len(raw):
                raise ValueError("Truncated program")
            count = struct.unpack_from(">I", raw, start + 24)[0]
            end = start + 56 + count * 4
            if end > len(raw):
                raise ValueError("Program float count exceeds bank")
            children[f"program[{index}]"] = _floats(raw[start:end], 56, count, ">", "normalized float32 BE")
            start = end
        trailing = raw[start:]
        return _join(
            raw, children, lambda parts: raw[:156] + b"".join(parts.values()) + trailing, "VST2 parameter bank"
        )
    if raw.startswith(b"FFBS") and len(raw) >= 12:
        return _floats(raw, 12, struct.unpack_from("<I", raw, 8)[0], "<", "FFBS")
    if raw.startswith(b"FabF") and len(raw) >= 20:
        size = struct.unpack_from("<I", raw, 8)[0]
        if 20 + size > len(raw):
            raise ValueError("FabF name exceeds buffer")
        count = struct.unpack_from("<I", raw, 16 + size)[0]
        return _floats(raw, 20 + size, count, "<", "FabF")
    if raw.startswith(b"PK\x03\x04"):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            if len(infos) > 256 or sum(info.file_size for info in infos) > MAX_BYTES:
                raise ValueError("Archive exceeds decoder budget")
            if len({info.filename for info in infos}) != len(infos):
                raise ValueError("Duplicate ZIP member names are ambiguous")
            contents = {info.filename: archive.read(info) for info in infos}

        def build_zip(parts: dict[str, bytes]) -> bytes:
            out = io.BytesIO()
            with zipfile.ZipFile(out, "w") as archive:
                archive.comment = zipfile.ZipFile(io.BytesIO(raw)).comment
                for info in infos:
                    archive.writestr(info, parts[info.filename])
            return out.getvalue()

        return _join(raw, {name: child(data) for name, data in contents.items()}, build_zip, "ZIP")
    if raw.startswith(b"\x1f\x8b") or (len(raw) >= 2 and raw[0] == 0x78 and raw[1] in (1, 0x9C, 0xDA)):
        is_gzip = raw.startswith(b"\x1f\x8b")
        inflater = zlib.decompressobj(31 if is_gzip else 15)
        body = inflater.decompress(raw, MAX_BYTES + 1)
        if len(body) > MAX_BYTES or not inflater.eof:
            raise ValueError("Compressed payload is oversized or truncated")
        suffix = inflater.unused_data
        compress = gzip.compress if is_gzip else zlib.compress
        return _join(
            raw,
            {"body": child(body)},
            lambda parts: compress(parts["body"]) + suffix,
            "gzip" if is_gzip else "zlib",
        )
    stripped = raw.lstrip()
    if stripped.startswith(b"<"):
        return _xml(raw)
    if stripped.startswith((b"{", b"[")):
        return _json(raw)
    if len(raw) >= 4 and struct.unpack_from("<I", raw)[0] == len(raw) - 4:
        return _join(
            raw,
            {"body": child(raw[4:])},
            lambda parts: struct.pack("<I", len(parts["body"])) + parts["body"],
            "u32 length prefix",
        )
    if len(raw) >= 16:
        first, second = struct.unpack_from("<QQ", raw)
        if first + second == len(raw) - 16:
            children = {
                "section[0]": child(raw[16 : 16 + first]),
                "section[1]": child(raw[16 + first :]),
            }
            return _join(
                raw,
                children,
                lambda parts: (
                    struct.pack("<QQ", len(parts["section[0]"]), len(parts["section[1]"]))
                    + parts["section[0]"]
                    + parts["section[1]"]
                ),
                "two u64 section lengths",
            )
    if raw.startswith(b"22 serialization::archive"):
        if experimental_arturia:
            mapping = single_float_map(raw)
            fields: dict[str, Scalar] = {field.name: field.value_text for field in mapping.fields}

            def encode_arturia(edits: Mapping[str, Scalar]) -> bytes:
                numeric: dict[str, str | int | float] = {}
                for key, value in edits.items():
                    if isinstance(value, bool) or not isinstance(value, (str, int, float)) or number(value) is None:
                        raise ValueError(f"Invalid Arturia numeric edit: {key}")
                    numeric[key] = value
                return mapping.encode(numeric)

            return View(fields, ("experimental Arturia counted float map; remaining archive opaque",), encode_arturia)
        return View({}, ("opaque Boost text archive",), lambda edits: _opaque(raw, edits))
    if re.match(rb"[A-Za-z_][A-Za-z_0-9.-]{0,127}\x00", raw):
        try:
            return _value_tree(raw)
        except (ValueError, UnicodeDecodeError, struct.error):
            # ValueTree has no magic; only a complete valid tree establishes this family.
            pass
    return View({}, ("opaque",), lambda edits: _opaque(raw, edits))


def _value_tree(raw: bytes) -> View:
    """JUCE ValueTree stream, corroborated against JUCE's public stream readers.

    Numeric variants are patched in place. Strings and other variant payloads
    retain their original bytes; this mapper does not invent their encodings.
    """
    cursor = 0
    fields: dict[str, Scalar] = {}
    locations: dict[str, tuple[int, int, int]] = {}

    def take(size: int) -> bytes:
        nonlocal cursor
        if size < 0 or cursor + size > len(raw):
            raise ValueError("Truncated ValueTree")
        result = raw[cursor : cursor + size]
        cursor += size
        return result

    def integer() -> int:
        first = take(1)[0]
        if first & 0x7F > 4:
            raise ValueError("Invalid compressed integer")
        value = int.from_bytes(take(first & 0x7F), "little")
        return -value if first & 0x80 else value

    def string() -> str:
        end = raw.find(b"\x00", cursor, min(cursor + 4096, len(raw)))
        if end < 0:
            raise ValueError("Unterminated ValueTree name")
        return take(end - cursor + 1)[:-1].decode("utf-8")

    def visit(parent: str, index: int, depth: int) -> None:
        if depth > MAX_DEPTH:
            raise ValueError("ValueTree nesting exceeds budget")
        tag = string()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9.-]*", tag):
            raise ValueError("Invalid ValueTree type")
        count = integer()
        if not 0 <= count <= 4096:
            raise ValueError("ValueTree property count exceeds budget")
        properties: dict[str, tuple[Scalar, tuple[int, int, int]]] = {}
        for _ in range(count):
            name = string()
            if not name or name in properties:
                raise ValueError("Empty or duplicate ValueTree property")
            size = integer()
            start = cursor
            payload = take(size)
            if not payload:
                value: Scalar = None
                marker = 0
            else:
                marker = payload[0]
                body = payload[1:]
                if marker in (1, 6, 4):
                    format = {1: "<i", 6: "<q", 4: "<d"}[marker]
                    if len(body) != struct.calcsize(format):
                        raise ValueError("Wrong numeric variant length")
                    numeric: int | float = struct.unpack(format, body)[0]
                    value = numeric
                elif marker in (2, 3):
                    if body:
                        raise ValueError("Wrong boolean variant length")
                    value = marker == 2
                elif marker == 5:
                    if not body.endswith(b"\x00"):
                        raise ValueError("Unterminated string variant")
                    value = body[:-1].decode("utf-8")
                else:
                    continue
            properties[name] = (value, (start, size, marker))
        identity = properties.get("id", properties.get("name", (str(index), (0, 0, 0))))[0]
        path = f"{parent}/{tag}[{index}:{identity}]" if parent else tag
        for name, (value, location) in properties.items():
            fields[path + "/@" + name] = value
            locations[path + "/@" + name] = location
        children = integer()
        if not 0 <= children <= 4096:
            raise ValueError("ValueTree child count exceeds budget")
        for child in range(children):
            visit(path, child, depth + 1)

    visit("", 0, 0)
    if cursor != len(raw):
        raise ValueError("Trailing data after ValueTree")

    def encode(edits: Mapping[str, Scalar]) -> bytes:
        out = bytearray(raw)
        for key, value in edits.items():
            if key not in locations:
                raise ValueError("Missing ValueTree field")
            start, size, marker = locations[key]
            if marker in (1, 6, 4):
                format = {1: "<i", 6: "<q", 4: "<d"}[marker]
                if marker == 4:
                    numeric = number(value)
                    if numeric is None:
                        raise ValueError("Non-numeric ValueTree edit")
                    replacement = struct.pack(format, numeric)
                else:
                    if isinstance(value, int) and not isinstance(value, bool):
                        integer_value = value
                    elif isinstance(value, str) and re.fullmatch(r"[+-]?\d{1,64}", value):
                        integer_value = int(value)
                    elif isinstance(value, float) and value.is_integer() and abs(value) <= 2**53:
                        integer_value = int(value)
                    else:
                        raise ValueError("ValueTree integer edit must be an exact integer")
                    replacement = struct.pack(format, integer_value)
                out[start + 1 : start + size] = replacement
            elif marker in (2, 3):
                out[start] = 2 if bool(value) else 3
            elif value != fields[key]:
                raise ValueError("Only numeric ValueTree variants are writable")
        return bytes(out)

    return View(fields, ("JUCE binary ValueTree",), encode)


def _opaque(raw: bytes, edits: Mapping[str, Scalar]) -> bytes:
    if edits:
        raise ValueError("Opaque state has no writable fields")
    return raw
