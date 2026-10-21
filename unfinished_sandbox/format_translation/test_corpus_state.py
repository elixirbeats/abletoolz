from __future__ import annotations

import gzip
import json
import struct
from dataclasses import replace
from pathlib import Path

import pytest
from corpus_state import Corpus, Device, adapt_vst2, adapt_vst3, extract, read_corpus
from juce_container import PluginState, unwrap, wrap


def source(payload: bytes, *, programs: int = 2, parameters: int = 2, uid: int = 1234) -> Device:
    return Device(0, "vst2", "Test", uid, None, parameters, programs, payload, None, "1", "2")


def float_bank() -> bytes:
    bank = bytearray(156)
    struct.pack_into(">4sI4sIIII", bank, 0, b"CcnK", 0, b"FxBk", 1, 1234, 5, 2)
    for index in range(2):
        program = struct.pack(">4sI4sIIII", b"CcnK", 0, b"FxCk", 1, 1234, 5, 2)
        bank.extend(program + bytes([index]) * 28 + struct.pack(">2f", 0.0, 0.0))
    return bytes(bank)


def float_payload() -> bytes:
    return (
        b"first".ljust(28, b"\0")
        + struct.pack("<2f", 0.25, -0.0)
        + b"second".ljust(28, b"\0")
        + bytes.fromhex("0100807f0000803f")
    )


def test_every_program_and_float_bit_pattern() -> None:
    original = float_bank()
    payload = float_payload()
    result, form = adapt_vst2(source(payload), original)
    assert form == "FxBk"
    assert result[:156] == original[:156]
    for index in range(2):
        cursor, start = 156 + 64 * index, 36 * index
        assert result[cursor : cursor + 28] == original[cursor : cursor + 28]
        assert result[cursor + 28 : cursor + 56] == payload[start : start + 28]
        assert result[cursor + 56 : cursor + 60] == payload[start + 28 : start + 32][::-1]
        assert result[cursor + 60 : cursor + 64] == payload[start + 32 : start + 36][::-1]


@pytest.mark.parametrize(
    "device",
    [
        replace(source(float_payload()), unique_id=4321),
        replace(source(float_payload()), program_count=1),
        replace(source(float_payload()), parameter_count=3),
    ],
)
def test_identity_and_count_mismatch(device: Device) -> None:
    with pytest.raises(ValueError):
        adapt_vst2(device, float_bank())


@pytest.mark.parametrize("bad", [float_bank() + b"trailing", float_bank()[:-1]])
def test_malformed_or_trailing_bank(bad: bytes) -> None:
    with pytest.raises(ValueError):
        adapt_vst2(source(float_payload()), bad)


def test_source_payload_trailing_bytes_refused() -> None:
    with pytest.raises(ValueError, match="payload length"):
        adapt_vst2(source(float_payload() + b"\0"), float_bank())


def test_later_program_identity_and_count_are_checked() -> None:
    for offset in (16, 24):
        bank = bytearray(float_bank())
        struct.pack_into(">I", bank, 156 + 64 + offset, 9)
        with pytest.raises(ValueError):
            adapt_vst2(source(float_payload()), bytes(bank))


def test_opaque_bank_lengths_and_observed_count() -> None:
    bank = bytearray(float_bank()[:156])
    bank[8:12] = b"FBCh"
    bank.extend(struct.pack(">I", 3) + b"old")
    struct.pack_into(">I", bank, 4, len(bank) - 8)
    device = source(b"longer chunk")
    with pytest.raises(ValueError, match="host-parameter-count"):
        adapt_vst2(device, bytes(bank))
    result, form = adapt_vst2(device, bytes(bank), 2)
    assert form == "FBCh"
    assert result[160:] == b"longer chunk"
    assert struct.unpack_from(">I", result, 156)[0] == len(b"longer chunk")
    assert struct.unpack_from(">I", result, 4)[0] == len(result) - 8
    with pytest.raises(ValueError, match="host parameter count"):
        adapt_vst2(device, bytes(bank), 9)
    with pytest.raises(ValueError, match="byteSize"):
        adapt_vst2(device, bytes(bank) + b"extra", 2)


@pytest.mark.parametrize("controller", [b"", b"different controller\0\xff"])
def test_vst3_pair_is_exact(controller: bytes) -> None:
    device = Device(0, "vst3", "Test3", None, (1, 2, 3, 4), None, None, b"processor\0", controller, "1", "2")
    state, form = adapt_vst3(device, wrap(PluginState(b"default", b"stale")))
    assert form == "JUCE VST3"
    assert unwrap(state) == PluginState(b"processor\0", controller)
    with pytest.raises(ValueError, match="trailing"):
        adapt_vst3(device, wrap(PluginState(b"default", b"stale")) + b"x")


def test_vst3_missing_pair_or_malformed_body_refused() -> None:
    device = Device(0, "vst3", "Test3", None, (1, 2, 3, 4), None, None, b"p", None, "1", "2")
    template = wrap(PluginState(b"default", b"stale"))
    with pytest.raises(ValueError, match="pair is absent"):
        adapt_vst3(device, template)
    document = b"<VST3PluginState><IComponent>7.A</IComponent><IEditController>0.</IEditController></VST3PluginState>"
    malformed = b"VC2!" + struct.pack("<I", len(document)) + document + b"\0"
    with pytest.raises(ValueError, match="noncanonical"):
        adapt_vst3(replace(device, controller=b""), malformed)


def test_listing_and_extraction_preserve_provenance(tmp_path: Path) -> None:
    path = tmp_path / "test.als"
    payload = float_payload()
    xml = (
        '<Ableton Creator="Test" MinorVersion="fixture"><LiveSet><PluginDesc><VstPluginInfo Id="7">'
        '<PlugName Value="Test"/><UniqueId Value="1234"/><NumberOfPrograms Value="2"/>'
        '<NumberOfParameters Value="2"/><Preset><VstPreset Id="8"><Buffer>'
        + payload.hex()
        + "</Buffer></VstPreset></Preset></VstPluginInfo></PluginDesc></LiveSet></Ableton>"
    )
    path.write_bytes(gzip.compress(xml.encode()))
    original_set = path.read_bytes()
    corpus = read_corpus(path)
    assert corpus.root_attributes == {"Creator": "Test", "MinorVersion": "fixture"}
    assert corpus.devices[0].processor == payload
    template = tmp_path / "template.state"
    template.write_bytes(float_bank())
    output = tmp_path / "extracted"
    metadata = extract(corpus, 0, template, output)
    assert metadata["device_ordinal"] == 0
    assert json.loads((output / "provenance.json").read_text())["source_set_sha256"] == corpus.sha256
    assert (output / "source.state").read_bytes() == payload
    assert path.read_bytes() == original_set
    with pytest.raises(FileExistsError):
        extract(corpus, 0, template, output)


def test_vst3_identity_limit_is_explicit(tmp_path: Path) -> None:
    device = Device(0, "vst3", "Test3", None, (1, 2, 3, 4), None, None, b"p", b"c", "1", "2")
    corpus = Corpus(tmp_path / "source.als", "sourcehash", {}, (device,))
    template = tmp_path / "template.state"
    template.write_bytes(wrap(PluginState(b"default", b"stale")))
    result = extract(corpus, 0, template, tmp_path / "output")
    assert result["class_id_fields_vst3"] == (1, 2, 3, 4)
    assert result["template_identity_check"] == "not_verified_template_has_no_class_id"
    assert result["restoration_verified"] is False
