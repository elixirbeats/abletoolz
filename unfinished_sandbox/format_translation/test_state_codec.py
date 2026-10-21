from __future__ import annotations

import io
import json
import struct
import zipfile
import zlib

import pytest
import state_codec
from juce_container import PluginState, unwrap, wrap
from state_codec import decode, equivalent


def bank(programs: tuple[tuple[float, ...], ...]) -> bytes:
    header = struct.pack(">4sI4sIIII128x", b"CcnK", 0, b"FxBk", 1, 1234, 2, len(programs))
    entries = []
    for index, values in enumerate(programs):
        name = f"program {index}".encode().ljust(28, b"\0")
        entries.append(
            struct.pack(">4sI4sIIII", b"CcnK", 48 + 4 * len(values), b"FxCk", 1, 1234, 2, len(values))
            + name
            + struct.pack(f">{len(values)}f", *values)
        )
    return header + b"".join(entries)


def chunk_bank(chunk: bytes) -> bytes:
    return struct.pack(">4sI4sIIII128xI", b"CcnK", len(chunk) + 152, b"FBCh", 1, 1234, 2, 1, len(chunk)) + chunk


def test_experimental_arturia_edit_rebuilds_both_enclosing_lengths() -> None:
    body = b"22 serialization::archive 10 opaque 1 0 0 0 6 MASTER 0.5 trailing"
    raw = chunk_bank(struct.pack("<QQ", len(body), len(body)) + body + body)
    assert not decode(raw).fields
    view = decode(raw, experimental_arturia=True)
    edited = view.encode({"chunk/section[1]/MASTER": "0.173"})
    assert struct.unpack_from(">I", edited, 4)[0] == len(edited) - 8
    assert struct.unpack_from(">I", edited, 156)[0] == len(edited) - 160
    a, b = struct.unpack_from("<QQ", edited, 160)
    assert a == len(body)
    assert b == len(body) + 2
    assert edited[176 : 176 + a] == body
    assert edited[176 + a :] == body.replace(b"0.5", b"0.173")
    assert view.encode({}) == raw


def test_experimental_arturia_refuses_ambiguous_maps() -> None:
    raw = b"22 serialization::archive 10 1 0 0 0 1 A 0.5 suffix 1 0 0 0 1 B 0.25 end"
    with pytest.raises(ValueError):
        decode(raw, experimental_arturia=True)


def test_all_programs_are_editable_without_byte_order_corruption() -> None:
    raw = bank(((0.25, 0.5), (0.75, 1.0)))
    view = decode(raw)
    assert view.fields["program[1]/float[0]"] == 0.75
    updated = view.encode({"program[1]/float[0]": 0.125})
    offset = 156 + 64 + 56
    assert struct.unpack_from(">f", updated, offset)[0] == 0.125
    assert updated[:offset] == raw[:offset]
    assert updated[offset + 4 :] == raw[offset + 4 :]
    assert view.encode({}) == raw


def test_vst3_controller_and_unknown_xml_attributes_survive_processor_edit() -> None:
    processor = b"FFBS" + struct.pack("<IIff", 1, 2, 0.25, 0.5) + b"opaque editor"
    raw = wrap(PluginState(processor, b"unparsed controller"))
    updated = decode(raw).encode({"IComponent/float[1]": -0.75})
    back = unwrap(updated)
    assert back.controller == b"unparsed controller"
    assert struct.unpack_from("<f", back.component, 16)[0] == -0.75
    assert back.component[20:] == b"opaque editor"


def test_nested_xml_rebuilds_both_length_headers_and_preserves_trailer() -> None:
    xml = b'<Parameters Mix="0.5"><PARAM id="gain" value="0.0"/></Parameters>'
    juce = b"VC2!" + struct.pack("<I", len(xml)) + xml + b"\0PRIVATE"
    raw = b"VstW" + bytes(12) + chunk_bank(juce) + b"BANKTAIL"
    updated = decode(raw).encode({"bank/chunk/xml/Parameters/@Mix": "0.1730000000001"})
    assert updated.endswith(b"\0PRIVATEBANKTAIL")
    size = struct.unpack_from(">I", updated, 16 + 156)[0]
    xml_size = struct.unpack_from("<I", updated, 16 + 160 + 4)[0]
    assert size == xml_size + 8 + len(b"\0PRIVATE")
    assert decode(updated).fields["bank/chunk/xml/Parameters/@Mix"] == "0.1730000000001"


def test_xml_edits_do_not_accumulate_between_trials() -> None:
    raw = b'<Root gain="0.5" mode="same"/>'
    view = decode(raw)
    first = view.encode({"Root/@gain": "0.1"})
    second = view.encode({"Root/@gain": "0.9"})
    assert decode(first).fields["Root/@gain"] == "0.1"
    assert decode(second).fields["Root/@gain"] == "0.9"
    assert view.encode({}) == raw


def test_zip_compressed_json_keeps_other_members_and_suffix() -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.comment = b"keep me"
        archive.writestr("patch", zlib.compress(b'{"gain":0.5,"name":"initial"}') + b"trailer")
        archive.writestr("opaque", b"\x00\xffunchanged")
    raw = out.getvalue()
    updated = decode(raw).encode({"patch/body//gain": 0.173})
    with zipfile.ZipFile(io.BytesIO(updated)) as archive:
        assert archive.comment == b"keep me"
        assert archive.read("opaque") == b"\x00\xffunchanged"
        assert archive.read("patch").endswith(b"trailer")
        assert json.loads(zlib.decompress(archive.read("patch")))["gain"] == 0.173


def test_binary_value_tree_numeric_edit_preserves_identifiers() -> None:
    # Public JUCE stream format: type, compressed property count, name, sized variant, child count.
    raw = b"Parameters\0\0\x01\x01PARAM\0\x01\x02id\0\x01\x06\x05gain\0value\0\x01\x09\x04"
    raw += struct.pack("<d", 0.5) + b"\0"
    view = decode(raw)
    assert view.layers == ("JUCE binary ValueTree",)
    field = "Parameters/PARAM[0:gain]/@value"
    updated = view.encode({field: -18.25})
    assert updated[:-9] == raw[:-9]
    assert struct.unpack_from("<d", updated, len(updated) - 9)[0] == -18.25
    assert decode(updated).fields[field] == -18.25
    with pytest.raises(ValueError, match="numeric"):
        view.encode({"Parameters/PARAM[0:gain]/@id": "other"})


def test_unrecognized_binary_has_no_invented_writable_fields() -> None:
    raw = b"\x99\x00\x00\x80\x3f"
    view = decode(raw)
    assert not view.fields
    assert view.encode({}) == raw
    with pytest.raises(ValueError, match="Opaque"):
        view.encode({"float[0]": 0.5})


@pytest.mark.parametrize("raw", [b"VC2!\xff\xff\xff\xff<Root/>", b"FFBS" + struct.pack("<II", 1, 500)])
def test_invalid_claimed_lengths_refuse_instead_of_guessing(raw: bytes) -> None:
    with pytest.raises(ValueError):
        decode(raw)


def test_decompression_has_a_real_byte_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state_codec, "MAX_BYTES", 128)
    raw = zlib.compress(b"x" * 129)
    with pytest.raises(ValueError, match="oversized"):
        decode(raw)


def test_truncated_compression_is_not_accepted_as_plain_binary() -> None:
    with pytest.raises(ValueError, match="truncated"):
        decode(zlib.compress(b'<Root gain="0.5"/>')[:-2])


def test_large_integers_remain_exact_when_comparing_and_editing() -> None:
    value = 9007199254740993
    raw = b"State\0\x01\x01id\0\x01\x09\x06" + struct.pack("<q", value) + b"\0"
    view = decode(raw)
    assert view.encode({"State/@id": value}) == raw
    assert not equivalent(value, value - 1)
    assert not equivalent(1000000, 1000001)
    assert not equivalent(str(value), str(value - 1))
    with pytest.raises(ValueError, match="exact integer"):
        view.encode({"State/@id": 0.25})


def test_xml_text_whitespace_survives_an_unrelated_later_edit() -> None:
    view = decode(b'<Root gain="0.5"><text>  keep  </text></Root>')
    view.encode({"Root/text[0:0]/text()": "changed"})
    changed_gain = view.encode({"Root/@gain": "0.9"})
    assert b">  keep  <" in changed_gain


def test_two_sized_sections_preserve_both_payloads() -> None:
    first = b"22 serialization::archive example"
    second = b"opaque"
    raw = struct.pack("<QQ", len(first), len(second)) + first + second
    view = decode(raw)
    assert view.layers == ("two u64 section lengths", "section[0]/opaque Boost text archive", "section[1]/opaque")
    assert view.encode({}) == raw
