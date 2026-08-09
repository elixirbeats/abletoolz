"""abletoolz.asd.writer  —  author Live-12 warp grids: rewrite existing .asd or cold-synthesize.

Strategy (see FORMAT.md):

* If the ``.asd`` exists (Live already analyzed the file), load it, keep every analysis
  section verbatim, and only author the grid fields (markers, IsWarped, MarkersGenerated,
  AufTaktData reset).
* If it does not exist, synthesize a minimal Live-12 SampleData document from scratch:
  correct OriginalFileSize (stale detection), unset analysis sections, empty overview.
  Live-side acceptance of cold-synthesized files still needs verification in Live.
"""

from __future__ import annotations

import wave
from pathlib import Path

from abletoolz.asd.parser import (
    PRE_DOC_BYTES,
    PRIM_BOOL,
    PRIM_DOUBLE,
    PRIM_FLOAT,
    PRIM_INT,
    UNSET_INT,
    ArrayValue,
    AsdFile,
    ClassDef,
    Document,
    ListValue,
    Obj,
    PrimArray,
    unset_auf_takt_data,
)

_DEFAULT_WARP_MODE = 3  # matches the user's Live default seen in both Live-12 fixtures (Re-Pitch)


def _live12_sample_data_schema() -> list[ClassDef]:
    """The 18-class Live-12 SampleData schema, in Live's own emission order."""
    return [
        ClassDef(
            "SampleData",
            36,
            [
                ("LoopStart", "RemoteableDouble"),
                ("LoopEnd", "RemoteableDouble"),
                ("SampleOffset", "RemoteableDouble"),
                ("HiddenLoopStart", "RemoteableDouble"),
                ("HiddenLoopEnd", "RemoteableDouble"),
                ("OutMarker", "RemoteableDouble"),
                ("Sync", "RemoteableBool"),
                ("HiQ", "RemoteableBool"),
                ("Fade", "RemoteableBool"),
                ("IsWarped", "RemoteableBool"),
                ("SampleVolume", "UserFloat"),
                ("VelocityAmount", "UserFloat"),
                ("PitchCoarse", "UserFloat"),
                ("PitchFine", "UserFloat"),
                ("WarpMode", "RemoteableEnum"),
                ("TransientResolution", "RemoteableEnum"),
                ("GranularityTones", "UserFloat"),
                ("GranularityTexture", "UserFloat"),
                ("FluctuationTexture", "UserFloat"),
                ("TransientLoopMode", "RemoteableEnum"),
                ("TransientEnvelope", "UserFloat"),
                ("ComplexProFormants", "UserFloat"),
                ("ComplexProEnvelope", "UserFloat"),
                ("TimeSignature", "RemoteableTimeSignature"),
                ("ColorIndex", "RemoteableInt"),
                ("WarpMarkers", "RemoteableList"),
                ("MarkersGenerated", "RemoteableBool"),
                ("LaunchMode", "RemoteableEnum"),
                ("LoopOn", "RemoteableBool"),
                ("LaunchQuantisation", "RemoteableEnum"),
                ("OnSets", "OnSets"),
                ("UserOnsets", "OnsetArray"),
                ("AufTaktData", "AufTaktData"),
                ("ExtraLength", "RemoteableInt"),
                ("OriginalFileSize", "RemoteableInt"),
                ("OverView", "SampleOverView"),
            ],
        ),
        ClassDef("List<SampleOverViewLevel>", -1, []),
        ClassDef("TimeSignatureDenominator", 1, [("Value", PRIM_FLOAT)]),
        ClassDef("RemoteableDouble", 1, [("Value", PRIM_DOUBLE)]),
        ClassDef("RemoteableBool", 1, [("Value", PRIM_BOOL)]),
        ClassDef("TimeSignatureNumerator", 1, [("Value", PRIM_FLOAT)]),
        ClassDef("UserFloat", 1, [("Value", PRIM_FLOAT)]),
        ClassDef("RemoteableEnum", 1, [("Value", PRIM_INT)]),
        ClassDef(
            "RemoteableTimeSignature",
            3,
            [
                ("Numerator", "TimeSignatureNumerator"),
                ("Denominator", "TimeSignatureDenominator"),
                ("Time", "RemoteableDouble"),
            ],
        ),
        ClassDef("RemoteableInt", 1, [("Value", PRIM_INT)]),
        ClassDef("RemoteableList", -1, []),
        ClassDef(
            "OnSets",
            4,
            [("Positions", 0x35), ("TransitionEnergies", 0x40), ("IsSet", PRIM_BOOL), ("Version", PRIM_INT)],
        ),
        ClassDef("OnsetArray", 2, [("UserOnsets", "RemoteableArray"), ("HasUserOnsets", "RemoteableBool")]),
        ClassDef("RemoteableArray", -3, []),
        ClassDef("OnsetEvent", 3, [("Time", PRIM_DOUBLE), ("Energy", PRIM_DOUBLE), ("IsVolatile", PRIM_BOOL)]),
        ClassDef(
            "AufTaktData",
            4,
            [
                ("PreprocessedDataChunk", 0x31),
                ("UnbiasedTempoEstimate", PRIM_DOUBLE),
                ("IsSet", PRIM_BOOL),
                ("Version", PRIM_INT),
            ],
        ),
        ClassDef(
            "SampleOverView",
            4,
            [
                ("OverViewLevels", "List<SampleOverViewLevel>"),
                ("SamplesPerBinLog2", PRIM_INT),
                ("ChannelCount", PRIM_INT),
                ("Version", PRIM_INT),
            ],
        ),
        ClassDef("SampleOverViewLevel", 1, [("InterleavedBinData", 0x32)]),
    ]


def _d(value: float) -> Obj:
    return Obj("RemoteableDouble", {"Value": value})


def _b(value: int) -> Obj:
    return Obj("RemoteableBool", {"Value": value})


def _f(value: float) -> Obj:
    return Obj("UserFloat", {"Value": value})


def _e(value: int) -> Obj:
    return Obj("RemoteableEnum", {"Value": value})


def _i(value: int) -> Obj:
    return Obj("RemoteableInt", {"Value": value})


def _default_sample_data(original_file_size: int, channel_count: int) -> Obj:
    """SampleData populated with the defaults observed in the Live-12 fixtures, analysis unset."""
    return Obj(
        "SampleData",
        {
            "LoopStart": _d(0.0),
            "LoopEnd": _d(0.0),
            "SampleOffset": _d(0.0),
            "HiddenLoopStart": _d(0.0),
            "HiddenLoopEnd": _d(0.0),
            "OutMarker": _d(0.0),
            "Sync": _b(1),
            "HiQ": _b(0),
            "Fade": _b(0),
            "IsWarped": _b(1),
            "SampleVolume": _f(1.0),
            "VelocityAmount": _f(0.0),
            "PitchCoarse": _f(0.0),
            "PitchFine": _f(0.0),
            "WarpMode": _e(_DEFAULT_WARP_MODE),
            "TransientResolution": _e(6),
            "GranularityTones": _f(30.0),
            "GranularityTexture": _f(65.0),
            "FluctuationTexture": _f(25.0),
            "TransientLoopMode": _e(2),
            "TransientEnvelope": _f(100.0),
            "ComplexProFormants": _f(100.0),
            "ComplexProEnvelope": _f(128.0),
            "TimeSignature": Obj(
                "RemoteableTimeSignature",
                {
                    "Numerator": Obj("TimeSignatureNumerator", {"Value": 4.0}),
                    "Denominator": Obj("TimeSignatureDenominator", {"Value": 4.0}),
                    "Time": _d(0.0),
                },
            ),
            "ColorIndex": _i(-1),
            "WarpMarkers": ListValue([]),
            "MarkersGenerated": _b(0),
            "LaunchMode": _e(0),
            "LoopOn": _b(1),
            "LaunchQuantisation": _e(0),
            "OnSets": Obj(
                "OnSets",
                {
                    "Positions": PrimArray(0x35, b""),
                    "TransitionEnergies": PrimArray(0x40, b""),
                    "IsSet": 0,
                    "Version": UNSET_INT,
                },
            ),
            "UserOnsets": Obj(
                "OnsetArray",
                {"UserOnsets": ArrayValue("OnsetEvent", []), "HasUserOnsets": _b(0)},
            ),
            "AufTaktData": unset_auf_takt_data(),
            "ExtraLength": _i(0),
            "OriginalFileSize": _i(original_file_size),
            "OverView": Obj(
                "SampleOverView",
                {
                    "OverViewLevels": ListValue([]),
                    "SamplesPerBinLog2": 7,
                    "ChannelCount": channel_count,
                    "Version": 2,
                },
            ),
        },
    )


def synthesize_asd(asd_path: Path, audio_path: Path, channel_count: int = 2) -> AsdFile:
    """Build a minimal Live-12 .asd for an audio file Live has never analyzed.

    Mirrors the fixture structure: empty leading table, SampleData document with unset
    analysis sections and the correct OriginalFileSize, plus the trailing unset
    AufTaktData document. Grid fields are authored afterwards via ``AsdFile.set_grid``.
    """
    doc1 = Document(
        version=5,
        doc_id=365,
        schema=_live12_sample_data_schema(),
        root=_default_sample_data(audio_path.stat().st_size, channel_count),
    )
    doc2 = Document(
        version=5,
        doc_id=0,
        schema=[
            ClassDef(
                "AufTaktData",
                4,
                [
                    ("PreprocessedDataChunk", 0x31),
                    ("UnbiasedTempoEstimate", PRIM_DOUBLE),
                    ("IsSet", PRIM_BOOL),
                    ("Version", PRIM_INT),
                ],
            )
        ],
        root=unset_auf_takt_data(),
    )
    return AsdFile(path=asd_path, lead_table=[], pre_doc=PRE_DOC_BYTES, documents=[doc1, doc2])


def _wav_info(audio_path: Path) -> tuple[float, int] | None:
    """(duration_seconds, channel_count) for WAV files, None for other formats."""
    if audio_path.suffix.lower() != ".wav":
        return None
    with wave.open(str(audio_path), "rb") as wav:
        return wav.getnframes() / wav.getframerate(), wav.getnchannels()


def write_grid(
    asd_path: Path,
    *,
    bpm: float,
    anchor_seconds: float,
    audio_path: Path | None = None,
    warp_mode: int | None = None,
) -> None:
    """Write a constant-tempo warp grid into ``asd_path`` (created if missing).

    If the .asd already exists (Live analyzed the audio), it is rewritten in place with
    all analysis data preserved. Otherwise a minimal .asd is cold-synthesized, which
    requires ``audio_path`` (for the source-size stale check and, for WAV, the duration
    so the tempo-pinning second marker can sit at the track end).

    Args:
        asd_path: the ``<audio file>.asd`` sidecar path.
        bpm: exact constant tempo.
        anchor_seconds: audio time of the downbeat that becomes beat 0.
        audio_path: source audio; required for cold synthesis, optional otherwise.
        warp_mode: optional Live warp-mode enum override.
    """
    duration: float | None = None
    channels = 2
    if audio_path is not None:
        info = _wav_info(audio_path)
        if info is not None:
            duration, channels = info
    if asd_path.exists():
        asd = AsdFile.load(asd_path)
    else:
        if audio_path is None:
            raise ValueError(f"{asd_path} does not exist; cold synthesis requires audio_path")
        asd = synthesize_asd(asd_path, audio_path, channel_count=channels)
    asd.set_grid(bpm=bpm, anchor_seconds=anchor_seconds, duration_seconds=duration, warp_mode=warp_mode)
    asd.save(backup=False)
