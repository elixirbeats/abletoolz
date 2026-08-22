"""Shrink a real Ableton set into a KB-scale version fixture.

Real sets are MB-scale, mostly plugin state and repeated tracks. The version
matrix only needs the schema: root attributes, master/main track, a couple of
tracks of each type, and the sample/plugin reference shapes. This tool prunes
everything else and writes ``<version>.als`` plus a ground-truth entry in
``expected.json`` harvested by direct XPath — deliberately independent of the
abletoolz parsing code the matrix puts under test.

Usage:
    python test/tools/extract_version_fixture.py SRC.als [SRC2.xml ...] -o test/version_fixtures/skeletons

Accepts .als (gzip) or raw .xml dumps. Deterministic output (gzip mtime=0).
"""

from __future__ import annotations

import argparse
import gzip
import json
import pathlib
import re
import sys
from xml.etree import ElementTree as ET

TRACK_TAGS = ("AudioTrack", "MidiTrack", "GroupTrack", "ReturnTrack")
KEEP_PER_TYPE = 2
# Tracks kept on top of the per-type quota because they carry a rare shape.
KEEP_NOTABLE = 3
VERSION_RE = re.compile(r"Ableton Live (\d{1,2})\.(\d{1,3})(?:\.?(\d{1,3}))?(b\d*)?")

# Path segments that are generic system/Live structure, safe to keep verbatim.
_KEEP_SEGMENTS = {
    "program files",
    "program files (x86)",
    "programdata",
    "windows",
    "users",
    "documents",
    "desktop",
    "downloads",
    "ableton",
    "user library",
    "core library",
    "resources",
    "devices",
    "audio effects",
    "midi effects",
    "instruments",
    "samples",
    "imported",
    "presets",
    "vstplugins",
    "vst64",
    "vst3",
    "common files",
    "vst",
}
_SEG_RE = re.compile(r"[/\\]")
_AUDIO_FILE_RE = re.compile(r"\.(?:wav|aif|aiff|mp3|flac|ogg|m4a|wv|asd)$", re.I)
_PATHISH_RE = re.compile(r"^(?:[A-Za-z]:[/\\]|\\\\|//)|[/\\].+[/\\]")


def load_root(path: pathlib.Path) -> ET.Element:
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return ET.fromstring(raw.decode("utf-8"))


def version_key(root: ET.Element) -> str:
    creator = root.get("Creator", "")
    m = VERSION_RE.search(creator)
    if not m:
        raise ValueError(f"Cannot parse version from Creator: {creator!r}")
    major, minor, patch, beta = m.group(1), m.group(2), m.group(3) or "0", m.group(4)
    return f"{major}.{minor}.{patch}" + ("b" if beta else "")


def is_notable(track: ET.Element) -> bool:
    """Whether a track carries a shape too rare to lose to the per-type quota.

    Keeping the first couple of tracks of each type is right for ordinary
    structure and wrong for exactly the shapes a fixture usually gets harvested
    for, which turn up on one track out of forty. Measured on the 22 generated
    sets in the library: every stub ``VstPluginInfo`` and every Pack
    ``SampleRef`` in all of them sat outside the first two tracks of its type,
    so the quota alone threw away the whole reason to harvest them.
    """
    return any(info.find("Category") is None for info in track.iter("VstPluginInfo")) or any(
        ref.find("LastModDate") is None for ref in track.iter("SampleRef")
    )


def prune(root: ET.Element) -> None:
    """Drop excess tracks and plugin buffer payloads; keep the schema."""
    liveset = root.find("LiveSet")
    if liveset is None:
        raise ValueError("No LiveSet element")
    tracks_el = liveset.find("Tracks")
    if tracks_el is not None:
        kept: dict[str, int] = {}
        notable = 0
        for track in list(tracks_el):
            if notable < KEEP_NOTABLE and is_notable(track):
                notable += 1
                continue
            kept[track.tag] = kept.get(track.tag, 0) + 1
            if kept[track.tag] > KEEP_PER_TYPE:
                tracks_el.remove(track)
    # Plugin state is opaque hex and routinely hundreds of KB; the schema
    # around it (PluginDesc, Path, UniqueId) is what version tests need.
    for buffer_el in root.iter("Buffer"):
        buffer_el.text = None
        for child in list(buffer_el):
            buffer_el.remove(child)


def _scrub_segment(seg: str) -> str:
    """Deterministic pseudonym: same input segment always maps to the same token."""
    import zlib

    lower = seg.lower()
    if lower in _KEEP_SEGMENTS or lower.startswith("live "):
        return seg
    stem, dot, ext = seg.rpartition(".")
    if dot and stem and len(ext) <= 5:
        return f"p{zlib.crc32(stem.encode()) % 100000:05d}.{ext}"
    return f"p{zlib.crc32(seg.encode()) % 100000:05d}"


def _scrub_path(value: str) -> str:
    """Anonymize a path-like string, preserving root, structure, and extensions."""
    parts = _SEG_RE.split(value)
    seps = _SEG_RE.findall(value) + [""]
    out: list[str] = []
    after_users = False
    for part in parts:
        if not part or (len(part) == 2 and part[1] == ":"):
            out.append(part)  # empty (UNC lead-in) or drive letter
        elif after_users:
            out.append("someone")
            after_users = False
        else:
            out.append(_scrub_segment(part))
        if part.lower() == "users":
            after_users = True
    return "".join(p + s for p, s in zip(out, seps, strict=True))


def _scrub_hex_utf16(text: str) -> str | None:
    """Scrub a hex-encoded UTF-16 path blob (pre-11 FileRef Data), keeping its layout."""
    stripped = text.replace("\t", "").replace(" ", "").replace("\n", "")
    if not stripped or len(stripped) % 2:
        return None
    try:
        decoded = bytes.fromhex(stripped).decode("utf-16")
    except (ValueError, UnicodeDecodeError):
        return None
    if not _PATHISH_RE.search(decoded):
        return None
    scrubbed = _scrub_path(decoded.rstrip("\x00"))
    hex_out = "".join(f"{b:02X}" for b in scrubbed.encode("utf-16-le")) + "0000"
    lines = text.splitlines()
    levels = lines[1].count("\t") if len(lines) > 1 else 1
    indent = "\t" * levels
    wrapped = [hex_out[i : i + 80] for i in range(0, len(hex_out), 80)]
    return "\n" + "\n".join(indent + w for w in wrapped) + "\n" + "\t" * max(levels - 1, 0)


def scrub(root: ET.Element) -> int:
    """Anonymize every path-bearing value in the tree. Returns replacement count."""
    count = 0
    for el in root.iter():
        for attr, value in el.attrib.items():
            if _PATHISH_RE.search(value):
                el.set(attr, _scrub_path(value))
                count += 1
            elif attr == "Dir" and value:
                el.set(attr, _scrub_segment(value))
                count += 1
            elif _AUDIO_FILE_RE.search(value):
                el.set(attr, _scrub_segment(value))
                count += 1
        if el.tag == "Data" and el.text:
            replaced = _scrub_hex_utf16(el.text)
            if replaced is not None:
                el.text = replaced
                count += 1
        # Clip names are imported-filename remnants (rip-site suffixes and all) —
        # scrub the lot; track names are user-typed and stay.
        if el.tag in ("AudioClip", "MidiClip"):
            name_el = el.find("Name")
            if name_el is not None and name_el.get("Value"):
                name_el.set("Value", _scrub_segment(name_el.get("Value", "").strip()))
                count += 1
    return count


def _value(parent: ET.Element | None, tag: str) -> str | None:
    if parent is None:
        return None
    el = parent.find(tag)
    return el.get("Value") if el is not None else None


def _midi_track_clips(liveset: ET.Element) -> list[ET.Element]:
    """Every real (track-owned) MidiClip: session ClipSlot + arrangement ClipTimeable.

    Mirrors the session/arrangement split ``AbletonTrack.clips_clipview``/
    ``clips_arrangement`` already establish, written directly against the XML
    so harvest() stays independent of the abletoolz code under test.
    Deliberately excludes ``GroovePool/Grooves/Groove/Clip/Value/MidiClip``: a
    groove-extraction template copy that belongs to no track and never plays
    back, not a session or arrangement clip.
    """
    clips: list[ET.Element] = []
    tracks_el = liveset.find("Tracks")
    if tracks_el is None:
        return clips
    for track in tracks_el:
        if track.tag != "MidiTrack":
            continue
        for clip_slot in track.iter("ClipSlot"):
            midi_clip = clip_slot.find("ClipSlot/Value/MidiClip")
            if midi_clip is not None:
                clips.append(midi_clip)
        clip_timeable = track.find(".//ClipTimeable")
        if clip_timeable is not None:
            clips.extend(clip_timeable.iter("MidiClip"))
    return clips


def harvest(root: ET.Element) -> dict[str, object]:
    """Ground truth by direct XPath, independent of abletoolz code."""
    creator = root.get("Creator", "")
    m = VERSION_RE.search(creator)
    assert m is not None
    version = [int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)]

    liveset = root.find("LiveSet")
    assert liveset is not None
    master = liveset.find("MasterTrack")
    main = liveset.find("MainTrack")
    master_tag = "MasterTrack" if master is not None else "MainTrack" if main is not None else None
    master_el = master if master is not None else main

    bpm: float | None = None
    if master_el is not None:
        for chain_tag in ("DeviceChain", "MasterChain"):
            tempo = master_el.find(f"{chain_tag}/Mixer/Tempo")
            if tempo is None:
                continue
            manual = _value(tempo, "Manual")
            if manual is not None:
                bpm = float(manual)
                break
            event = tempo.find("ArrangerAutomation/Events/FloatEvent")
            if event is not None and event.get("Value") is not None:
                bpm = float(event.get("Value", ""))
                break

    tracks: list[dict[str, object]] = []
    tracks_el = liveset.find("Tracks")
    if tracks_el is not None:
        for track in tracks_el:
            name_el = track.find("Name")
            color = _value(track, "Color")
            color_index = _value(track, "ColorIndex")
            tracks.append(
                {
                    "tag": track.tag,
                    "name": _value(name_el, "UserName") or _value(name_el, "EffectiveName"),
                    "color": int(color) if color is not None else None,
                    "color_index": int(color_index) if color_index is not None else None,
                }
            )

    sample_refs = root.findall(".//SampleRef")
    refs_with_abs = 0
    for ref in sample_refs:
        file_ref = ref.find("FileRef")
        if file_ref is None:
            continue
        path_el = file_ref.find("Path")
        data_el = file_ref.find("Dir/Data") if file_ref.find("Dir/Data") is not None else file_ref.find("Data")
        if (path_el is not None and path_el.get("Value")) or (data_el is not None and (data_el.text or "").strip()):
            refs_with_abs += 1

    current_ends = [float(el.get("Value", 0)) for el in root.iter("CurrentEnd")]
    au_plugins = []
    for element in root.findall(".//AuPluginInfo"):
        identifier = [_value(element, tag) for tag in ("ComponentType", "ComponentSubType", "ComponentManufacturer")]
        au_plugins.append(
            {
                "name": _value(element, "Name"),
                "manufacturer": _value(element, "Manufacturer"),
                "identifier": [int(value) if value is not None else None for value in identifier],
            }
        )

    return {
        "creator": creator,
        "version": version,
        "beta": bool(m.group(4)),
        "master_tag": master_tag,
        "bpm": bpm,
        "width_tag_typo_count": sum(1 for _ in root.iter("ViewStateSesstionTrackWidth")),
        "width_tag_fixed_count": sum(1 for _ in root.iter("ViewStateSessionTrackWidth")),
        "tracks": tracks,
        "au_plugins": au_plugins,
        "vst3_plugin_names": [el.get("Value") for el in root.findall(".//Vst3PluginInfo/Name")],
        "sample_ref_count": len(sample_refs),
        "sample_refs_with_abs": refs_with_abs,
        "track_unfolded_count": sum(1 for _ in root.iter("TrackUnfolded")),
        "is_folded_count": sum(1 for _ in root.iter("IsFolded")),
        "prehear_present": liveset.find("PreHearTrack") is not None,
        "group_mixer_isfolded_count": sum(
            1 for group in liveset.findall("Tracks/GroupTrack") if group.find("DeviceChain/Mixer/IsFolded") is not None
        ),
        "lane_height_count": sum(1 for _ in root.iter("LaneHeight")),
        "furthest_bar": int(max(current_ends) / 4) if current_ends else 0,
        "midi_note_counts": sum(len(clip.findall(".//MidiNoteEvent")) for clip in _midi_track_clips(liveset)),
    }


def extract(src: pathlib.Path, out_dir: pathlib.Path) -> tuple[str, dict[str, object]]:
    root = load_root(src)
    key = version_key(root)
    prune(root)
    scrubbed = scrub(root)
    report = harvest(root)
    report["source"] = src.name

    xml_bytes = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root) + b"\n"
    out_path = out_dir / f"{key}.als"
    out_path.write_bytes(gzip.compress(xml_bytes, mtime=0))
    print(f"{key:>10}.als  {out_path.stat().st_size / 1024:7.1f} KB  scrubbed={scrubbed:<4} <- {src.name}")
    return key, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="+", type=pathlib.Path)
    parser.add_argument("-o", "--out-dir", type=pathlib.Path, required=True)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    expected_path = args.out_dir / "expected.json"
    expected: dict[str, object] = {}
    if expected_path.exists():
        expected = json.loads(expected_path.read_text(encoding="utf-8"))

    written: dict[str, pathlib.Path] = {}
    for src in args.sources:
        key = version_key(load_root(src))
        if key in written:
            print(f"SKIP {src.name}: {key} already extracted this run from {written[key].name}")
            continue
        written[key] = src
        key, report = extract(src, args.out_dir)
        expected[key] = report

    expected_path.write_text(json.dumps(expected, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"expected.json: {len(expected)} fixtures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
