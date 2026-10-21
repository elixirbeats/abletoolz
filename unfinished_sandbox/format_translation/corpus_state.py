"""Extract real set states and adapt them to an explicitly supplied host template."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from juce_container import PluginState, juce_base64_decode, juce_base64_encode, wrap


@dataclass(frozen=True, slots=True)
class Device:
    ordinal: int
    kind: Literal["vst2", "vst3"]
    name: str
    unique_id: int | None
    class_id_fields: tuple[int, ...] | None
    parameter_count: int | None
    program_count: int | None
    processor: bytes | None
    controller: bytes | None
    info_id: str | None
    preset_id: str | None

    def summary(self) -> dict[str, object]:
        return {
            "device_ordinal": self.ordinal,
            "format": self.kind,
            "plugin_name": self.name,
            "unique_id_vst2": self.unique_id,
            "class_id_fields_vst3": self.class_id_fields,
            "parameter_count": self.parameter_count,
            "program_count": self.program_count,
            "state_present": self.processor is not None,
            "state_length": len(self.processor) if self.processor is not None else None,
            "state_sha256": digest(self.processor) if self.processor is not None else None,
            "controller_present": self.controller is not None,
            "controller_length": len(self.controller) if self.controller is not None else None,
            "controller_sha256": digest(self.controller) if self.controller is not None else None,
            "info_id": self.info_id,
            "preset_id": self.preset_id,
        }


@dataclass(frozen=True, slots=True)
class Corpus:
    source: Path
    sha256: str
    root_attributes: dict[str, str]
    devices: tuple[Device, ...]

    def provenance(self) -> dict[str, object]:
        return {
            "source_set": str(self.source),
            "source_set_sha256": self.sha256,
            "source_root_attributes": self.root_attributes,
            "source_engine_sample_rate_hz": None,
            "source_engine_sample_rate_note": "Sample-reference rates do not establish the host engine rate",
            "ordinal_definition": "Zero-based VstPluginInfo/Vst3PluginInfo document order",
        }


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _value(element: ET.Element, tag: str) -> str | None:
    child = element.find(tag)
    return child.get("Value") if child is not None else None


def _integer(element: ET.Element, tag: str) -> int | None:
    value = _value(element, tag)
    return int(value) if value is not None else None


def _buffer(element: ET.Element, tag: str) -> bytes | None:
    child = element.find(tag)
    return bytes.fromhex("".join((child.text or "").split())) if child is not None else None


def read_corpus(source: Path) -> Corpus:
    raw = source.read_bytes()
    root = ET.fromstring(gzip.decompress(raw))
    devices: list[Device] = []
    for info in root.iter():
        if info.tag not in ("VstPluginInfo", "Vst3PluginInfo"):
            continue
        vst2 = info.tag == "VstPluginInfo"
        presets = info.findall("Preset/VstPreset" if vst2 else "Preset/Vst3Preset")
        if len(presets) > 1:
            raise ValueError("Multiple preset elements in one plugin info are unsupported")
        preset = presets[0] if presets else ET.Element("AbsentPreset")
        fields = preset.find("Uid")
        class_id: tuple[int, ...] | None = None
        if fields is not None:
            values = [_integer(fields, f"Fields.{index}") for index in range(4)]
            if any(value is None for value in values):
                raise ValueError("Incomplete VST3 class ID")
            class_id = tuple(value for value in values if value is not None)
        uid = _integer(info, "UniqueId")
        if uid is not None and not -(1 << 31) <= uid < (1 << 32):
            raise ValueError("VST2 UniqueId is outside the 32-bit range")
        devices.append(
            Device(
                ordinal=len(devices),
                kind="vst2" if vst2 else "vst3",
                name=_value(info, "PlugName" if vst2 else "Name") or "",
                unique_id=uid & 0xFFFFFFFF if uid is not None else None,
                class_id_fields=class_id,
                parameter_count=_integer(info, "NumberOfParameters"),
                program_count=_integer(info, "NumberOfPrograms"),
                processor=_buffer(preset, "Buffer" if vst2 else "ProcessorState"),
                controller=None if vst2 else _buffer(preset, "ControllerState"),
                info_id=info.get("Id"),
                preset_id=presets[0].get("Id") if presets else None,
            )
        )
    return Corpus(source.resolve(), digest(raw), dict(root.attrib), tuple(devices))


def _check_size(data: bytes, offset: int, end: int) -> None:
    size = struct.unpack_from(">I", data, offset + 4)[0]
    # JUCE's measured FXB writer uses zero for byteSize in both header levels.
    if size not in (0, end - offset - 8):
        raise ValueError("FXB byteSize disagrees with its declared extent")


def adapt_vst2(device: Device, template: bytes, host_parameter_count: int | None = None) -> tuple[bytes, str]:
    if len(template) < 156 or template[:4] != b"CcnK":
        raise ValueError("Host template is not a complete VST2 bank")
    source = device.processor
    if source is None or device.unique_id is None or device.program_count is None or device.parameter_count is None:
        raise ValueError("Source VST2 state, ID, or parameter/program count is absent")
    if device.program_count < 1 or device.parameter_count < 0:
        raise ValueError("Invalid source program/parameter count")
    uid, programs = struct.unpack_from(">I", template, 16)[0], struct.unpack_from(">I", template, 24)[0]
    if uid != device.unique_id:
        raise ValueError("Source UniqueId differs from host template fxID")
    if programs != device.program_count:
        raise ValueError("Source program count differs from host template")
    if host_parameter_count is not None and host_parameter_count != device.parameter_count:
        raise ValueError("Declared host parameter count differs from source")
    _check_size(template, 0, len(template))
    form = template[8:12]
    if form == b"FBCh":
        if len(template) < 160 or 160 + struct.unpack_from(">I", template, 156)[0] != len(template):
            raise ValueError("Malformed or trailing bytes in opaque host bank")
        if host_parameter_count is None:
            raise ValueError("FBCh has no parameter count; supply observed --host-parameter-count")
        result = bytearray(template[:156])
        result.extend(struct.pack(">I", len(source)))
        result.extend(source)
        if struct.unpack_from(">I", template, 4)[0]:
            struct.pack_into(">I", result, 4, len(result) - 8)
        return bytes(result), "FBCh"
    if form != b"FxBk":
        raise ValueError("Unsupported VST2 bank form")
    stride = 28 + 4 * device.parameter_count
    if len(source) != programs * stride:
        raise ValueError("Source float payload length disagrees with all program/parameter counts")
    result = bytearray(template)
    cursor = 156
    for program in range(programs):
        end = cursor + 28 + stride
        if end > len(template) or template[cursor : cursor + 4] != b"CcnK":
            raise ValueError("Incomplete FxCk program")
        if template[cursor + 8 : cursor + 12] != b"FxCk":
            raise ValueError("Expected FxCk at program boundary")
        if struct.unpack_from(">I", template, cursor + 16)[0] != uid:
            raise ValueError("Program fxID differs from bank fxID")
        if struct.unpack_from(">I", template, cursor + 24)[0] != device.parameter_count:
            raise ValueError("Source parameter count differs from host program")
        _check_size(template, cursor, end)
        start = program * stride
        result[cursor + 28 : cursor + 56] = source[start : start + 28]
        floats = source[start + 28 : start + stride]
        # Reverse each float's bytes without normalizing NaNs or signed zero.
        result[cursor + 56 : end] = b"".join(floats[index : index + 4][::-1] for index in range(0, len(floats), 4))
        cursor = end
    if cursor != len(template):
        raise ValueError("Trailing bytes after declared FXB programs")
    return bytes(result), "FxBk"


def adapt_vst3(device: Device, template: bytes) -> tuple[bytes, str]:
    if len(template) < 9 or template[:4] != b"VC2!":
        raise ValueError("Host template is not a JUCE VST3 state")
    size = struct.unpack_from("<I", template, 4)[0]
    if len(template) != 9 + size or template[-1:] != b"\0":
        raise ValueError("Malformed or trailing bytes in JUCE VST3 envelope")
    root = ET.fromstring(template[8 : 8 + size])
    if (
        root.tag != "VST3PluginState"
        or root.attrib
        or (root.text or "").strip()
        or [child.tag for child in root] != ["IComponent", "IEditController"]
    ):
        raise ValueError("Unexpected JUCE VST3 document shape")
    for child in root:
        if len(child) or child.attrib or (child.tail or "").strip():
            raise ValueError("Unexpected nested JUCE state element")
        text = child.text or ""
        if juce_base64_encode(juce_base64_decode(text)) != text:
            raise ValueError("Malformed or noncanonical JUCE state body")
    if device.processor is None or device.controller is None:
        raise ValueError("Source VST3 processor/controller pair is absent")
    return wrap(PluginState(device.processor, device.controller)), "JUCE VST3"


def extract(
    corpus: Corpus, ordinal: int, template_path: Path, output: Path, host_parameter_count: int | None = None
) -> dict[str, object]:
    if ordinal < 0 or ordinal >= len(corpus.devices):
        raise ValueError("Device ordinal is outside this set's listing")
    device = corpus.devices[ordinal]
    template = template_path.read_bytes()
    if device.kind == "vst2":
        state, form = adapt_vst2(device, template, host_parameter_count)
        identity = "source_unique_id_matches_template_fxid"
    else:
        state, form = adapt_vst3(device, template)
        identity = "not_verified_template_has_no_class_id"
    provenance = (
        corpus.provenance()
        | device.summary()
        | {
            "host_template": str(template_path.resolve()),
            "host_template_sha256": digest(template),
            "observed_host_format": form,
            "host_parameter_count_declared": host_parameter_count,
            "template_identity_check": identity,
            "host_state_file": "host.state",
            "host_state_sha256": digest(state),
            "host_state_length": len(state),
            "restoration_verified": False,
        }
    )
    output.mkdir(parents=True, exist_ok=False)
    (output / "host.state").write_bytes(state)
    if device.processor is not None:
        (output / "source.state").write_bytes(device.processor)
    if device.controller is not None:
        (output / "source.controller").write_bytes(device.controller)
    (output / "provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    return provenance


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List plugin devices in zero-based document order")
    listing.add_argument("set", type=Path)
    extraction = commands.add_parser("extract", help="Build host.state without loading the set or plugin")
    extraction.add_argument("set", type=Path)
    extraction.add_argument("--device", required=True, type=int)
    extraction.add_argument("--host-template", required=True, type=Path)
    extraction.add_argument("--host-parameter-count", type=int, help="Observed host inventory count; required for FBCh")
    extraction.add_argument("--out", required=True, type=Path, help="New output directory")
    args = parser.parse_args()
    corpus = read_corpus(args.set)
    if args.command == "list":
        report = corpus.provenance() | {"devices": [device.summary() for device in corpus.devices]}
    else:
        report = extract(corpus, args.device, args.host_template, args.out, args.host_parameter_count)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
