from __future__ import annotations

import struct

import pytest
from arturia_preset_metadata import HEADER, MetadataError, parse_root_metadata, retarget, split_payloads


def string(value: str) -> bytes:
    encoded = value.encode()
    return str(len(encoded)).encode() + b" " + encoded


def body(
    *,
    instrument: int = 1,
    version: str = "",
    second_flag: int = 0,
    comment: str = "Comment 123 0 0 0",
    tags: tuple[str, ...] = ("Ambient", "Bright"),
) -> bytes:
    return (
        HEADER
        + string("Test café")
        + b" "
        + string("Factory")
        + b" "
        + str(instrument).encode()
        + b" "
        + string("Author")
        + b" "
        + string("Pad")
        + b" 0 0 "
        + str(len(tags)).encode()
        + b" 0 "
        + b" ".join(string(tag) for tag in tags)
        + b" 1 "
        + str(second_flag).encode()
        + b" "
        + string(comment)
        + b" 1534427804 "
        + string(version)
        + b" opaque child ID 6 version 3.9.1.1843\0\xff"
    )


@pytest.mark.parametrize("second_flag", [0, 1])
def test_metadata_spans_and_exact_opaque_preservation(second_flag: int) -> None:
    raw = body(second_flag=second_flag)
    parsed = parse_root_metadata(raw)
    assert parsed.name == "Test café"
    assert parsed.instrument_id == 1 and parsed.version == ""
    assert parsed.tags == ("Ambient", "Bright")
    assert parsed.flags == (True, bool(second_flag))
    assert parsed.comment == "Comment 123 0 0 0"
    assert raw[parsed.instrument_id_span.start : parsed.instrument_id_span.end] == b"1"
    assert raw[parsed.version_span.start : parsed.version_span.end] == b"0 "
    edited = parsed.encode_identity(60, "5.11.2.6235")
    actual = parse_root_metadata(edited)
    assert actual.instrument_id == 60 and actual.version == "5.11.2.6235"
    assert edited[actual.version_span.end :] == raw[parsed.version_span.end :]
    assert edited.count(b"child ID 6 version 3.9.1.1843") == 1


def test_two_payload_sizes_and_explicit_standalone_mode() -> None:
    first, second = body(), body(version="4.1.1.3554")
    framed = struct.pack("<QQ", len(first), len(second)) + first + second
    converted = retarget(framed, 60, "5.11.2.6235")
    a, b = split_payloads(converted)
    assert parse_root_metadata(a).instrument_id == parse_root_metadata(b).instrument_id == 60
    assert parse_root_metadata(a).version == parse_root_metadata(b).version == "5.11.2.6235"
    assert retarget(first, 60, "5.11.2.6235", standalone=True) == a
    with pytest.raises(MetadataError):
        retarget(first, 60, "5.11.2.6235")
    with pytest.raises(MetadataError):
        retarget(framed + b"trailing", 60, "5.11.2.6235")


@pytest.mark.parametrize("raw", [body(instrument=60), body(version="bad-version"), body()[:70]])
def test_wrong_id_version_and_truncation_refused(raw: bytes) -> None:
    with pytest.raises(MetadataError):
        retarget(raw, 60, "5.11.2.6235", standalone=True)


@pytest.mark.parametrize("version", ["", "5.11.2", "bad", "5.11.2.6235 trailing"])
def test_invalid_target_version_refused(version: str) -> None:
    with pytest.raises(MetadataError):
        retarget(body(), 60, version, standalone=True)


def test_id_only_patch_preserves_source_version_and_all_other_bytes() -> None:
    raw = body(version="4.1.1.3554")
    metadata = parse_root_metadata(raw)
    edited = metadata.encode_identity(60, None)
    span = metadata.instrument_id_span
    assert edited == raw[: span.start] + b"60" + raw[span.end :]
    assert parse_root_metadata(edited).version == "4.1.1.3554"


def test_id_only_envelope_changes_only_ids_and_lengths() -> None:
    parts = [body(version="4.1.1.3554"), body()]
    raw = struct.pack("<QQ", len(parts[0]), len(parts[1])) + b"".join(parts)
    converted = retarget(raw, 60)
    actual = split_payloads(converted)
    for original, edited in zip(parts, actual, strict=True):
        metadata = parse_root_metadata(original)
        span = metadata.instrument_id_span
        assert edited == original[: span.start] + b"60" + original[span.end :]
        assert parse_root_metadata(edited).version == metadata.version
    assert len(converted) == len(raw) + 2


def test_actual_empty_comment_prefix_keeps_timestamp_and_version_aligned() -> None:
    raw = HEADER + b"0  0  1 0  7 Unknown 0 0 0 0 0 0 0  0 10 4.1.1.3554 opaque"
    parsed = parse_root_metadata(raw)
    assert parsed.tags == ()
    assert parsed.flags == (False, False)
    assert parsed.comment == ""
    assert parsed.timestamp == 0
    assert parsed.version == "4.1.1.3554"
    assert raw[parsed.version_span.start : parsed.version_span.end] == b"10 4.1.1.3554"


def test_empty_and_nonempty_comments_do_not_change_boolean_schema() -> None:
    for comment in ("", "Comment with spaces"):
        parsed = parse_root_metadata(body(comment=comment, tags=(), version="4.1.1.3554"))
        assert parsed.comment == comment
        assert parsed.tags == ()
        assert parsed.flags == (True, False)
        assert parsed.timestamp == 1534427804
        assert parsed.version == "4.1.1.3554"
    with pytest.raises(MetadataError, match="boolean"):
        parse_root_metadata(body(second_flag=2))
