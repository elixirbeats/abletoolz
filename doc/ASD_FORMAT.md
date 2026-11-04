# Ableton Live 12 `.asd` format

Live 12.4.25b1 on Windows accepts authored warp grids when the header's clip-defaults flag is set. The writer is available as `abletoolz.formats.asd.write_grid`; it can rewrite existing analysis or create a sidecar for an unanalyzed WAV.

```python
from pathlib import Path
from abletoolz.formats.asd import write_grid

audio = Path("track.wav")
write_grid(Path(str(audio) + ".asd"), audio_path=audio, bpm=137.0, anchor_seconds=0.321)
```

Write the sidecar before importing the audio into Live. An already loaded clip keeps its own settings.

Established by byte-exact structural parses of files Live wrote (fixtures are in `test/asd_fixtures/`, parser in `abletoolz/formats/asd/`):

- `no_warp_markers.asd` — Live 12, MP3 source, before auto-warp analysis
- `with_warp_markers.asd` — Live 12, the same MP3 after auto-warp
- `pine-021.wav.asd` — older Live, WAV source (legacy schema)
- `mono-live-12.wav.asd` — Live 12.4.25b1, eight-second mono WAV, before saving clip defaults
- `mono-live-12-saved.wav.asd` — the same file after Live's Save button writes clip defaults

All five fixtures round-trip byte-for-byte. Live acceptance is checked separately below.

## Top-level file layout

```
offset 0:  02 bytes   file magic 06 49
offset 2:  u64 LE     N = leading-table entry count
offset 10: u32 LE ×N  leading table (see below)
then:      u32 LE     0 in the measured fixtures
           u32 LE     100 in the measured fixtures
           u32 LE     channel-data byte count M (2 for mono, 4 for stereo)
           M bytes    channel data (zero in the measured fixtures)
           u8         clip-defaults flag: 0 = analysis only, 1 = load saved clip settings
then:      1..n documents, back to back, each starting with magic AB 1E 56 78
EOF exactly after the last document.
```

### Leading table

Ascending u32 values, final entry 0 (padding/terminator, included in N). Values are **sample
positions** spanning the whole file (fixtures: N=11980, last real value 11,681,806 ≈ 4:24.9 @44.1 kHz;
pine: N=16, last real 13,492), average spacing ~975 samples ≈ 22 ms → interpreted as Live's
transient/segment grid (Beats-warp granularity). **Byte-identical between the warped and unwarped
fixture**, i.e. audio-derived, not warp-related. Meaning of individual entries not needed for grid
writing; preserve verbatim on rewrite. Cold synthesis writes N=0, accepted in the WAV probes below.

## Document layout

```
AB 1E 56 78      magic
u8               version byte = 0x05
u32              unknown; 365 in doc1 of all fixtures, 0 in doc2. Preserve/replicate.
ascii-str        root class name         ("SampleData" doc1, "AufTaktData" doc2)
i32              class-def count
class-def ×count schema table
data             root object serialized per schema, immediately after the table
```

String encodings:

- **ascii-str** (class/type names): `00` + u8 length + ASCII bytes.
- **utf16-str** (field names, string values): u32 LE char count + UTF-16-LE bytes.

### Class definition

```
ascii-str   class name
i32         field count   (>=0 normal; -1 = list container; -3 = array container; containers have no field defs)
per field:  utf16-str field name, then EITHER an ascii-str (named class ref, first byte 00)
            OR a single byte primitive type id (first byte != 0)
```

Primitive type ids observed:

| id   | type    | wire size |
|------|---------|-----------|
| 0x10 | bool    | 1 byte |
| 0x11 | int     | i32 LE |
| 0x12 | float   | f32 LE |
| 0x14 | string  | utf16-str (older schema only, `Name` field) |
| 0x17 | double  | f64 LE |
| 0x31 | array of u8  | u32 count + count×1 B |
| 0x32 | array of u16 | u32 count + count×2 B |
| 0x35 | array of u32 | u32 count + count×4 B |
| 0x40 | array of f32 | u32 count + count×4 B |

### Container serialization (in data section)

- **List** (field count -1; `RemoteableList`, `List<SampleOverViewLevel>`):
  `u32 count`, then per element: `ascii-str element class name` + `u32 element index (0-based)` +
  element payload (fields per that class's schema def), then a 2-byte terminator `00 00`
  (reads as an empty ascii-str). An empty list is 6 bytes: `00 00 00 00 00 00`.
- **Array** (field count -3; `RemoteableArray`): `u32 count` + `ascii-str element class name`
  (written once, even when count=0) + count× element payload. No terminator.

### Unset-analysis sentinel pattern

Sub-objects Live has not analyzed use: empty arrays, `IsSet=0`, `Version = INT_MIN (0x80000000)`,
doubles = `DBL_MAX` (1.7976931348623157e308). Seen in no_warp's AufTaktData.

## Doc 1: `SampleData` — Live-12 schema (18 classes; +`WarpMarker` when markers exist)

Field order in the data section (= schema order). Fixture values right column
(no_warp / with_warp identical unless noted):

| field | type | fixture value | meaning |
|---|---|---|---|
| LoopStart | RemoteableDouble | 0.0 | loop start, **beats** rel. beat 0 |
| LoopEnd | RemoteableDouble | 0.0 | loop end, beats (0 = not materialized) |
| SampleOffset | RemoteableDouble | 0.0 | start-marker offset, beats |
| HiddenLoopStart | RemoteableDouble | 0.0 | stored loop bounds while loop off |
| HiddenLoopEnd | RemoteableDouble | 0.0 | |
| OutMarker | RemoteableDouble | 0.0 | clip end marker, beats |
| Sync | RemoteableBool | 1 | |
| HiQ | RemoteableBool | 0 | |
| Fade | RemoteableBool | 0 (pine: 1) | clip-edge declick |
| IsWarped | RemoteableBool | 1 | **warp on/off** |
| SampleVolume | UserFloat | 1.0 | |
| VelocityAmount | UserFloat | 0.0 | |
| PitchCoarse | UserFloat | 0.0 | |
| PitchFine | UserFloat | 0.0 | |
| WarpMode | RemoteableEnum | 3 (pine: 0) | 0 Beats … 3 Re-Pitch … (als enum) |
| TransientResolution | RemoteableEnum | 6 | |
| GranularityTones | UserFloat | 30.0 | |
| GranularityTexture | UserFloat | 65.0 | |
| FluctuationTexture | UserFloat | 25.0 | |
| TransientLoopMode | RemoteableEnum | 2 | |
| TransientEnvelope | UserFloat | 100.0 | |
| ComplexProFormants | UserFloat | 100.0 | |
| ComplexProEnvelope | UserFloat | 128.0 | |
| TimeSignature | RemoteableTimeSignature | 4/4, Time 0.0 | Numerator/Denominator f32, Time f64 |
| ColorIndex | RemoteableInt | -1 | |
| WarpMarkers | RemoteableList | **empty in all 3 fixtures** | see below |
| MarkersGenerated | RemoteableBool | 0 | markers materialized flag |
| LaunchMode | RemoteableEnum | 0 | |
| LoopOn | RemoteableBool | 1 | loop enabled |
| LaunchQuantisation | RemoteableEnum | 0 | 0 = global |
| OnSets | OnSets | 1435 onsets, IsSet=1, Version=5 | Positions: u32 sample positions (0x35); TransitionEnergies: f32 (0x40) |
| UserOnsets | OnsetArray | empty, HasUserOnsets=0 | RemoteableArray of OnsetEvent {Time f64, Energy f64, IsVolatile bool} |
| AufTaktData | AufTaktData | no_warp: unset; with_warp: 317,904-B chunk, IsSet=1, Version=5, UnbiasedTempoEstimate=0.0 | **auto-warp tempo analysis, opaque blob** |
| ExtraLength | RemoteableInt | 526 (pine WAV: 0) | MP3 decoder-delay-ish extra length; 0 for WAV |
| OriginalFileSize | RemoteableInt | 10,794,273 | **source audio file size in bytes** (stale detection) |
| OverView | SampleOverView | 4 levels | waveform peaks; List of SampleOverViewLevel, each = InterleavedBinData u16 array; levels downsample by 128 (SamplesPerBinLog2=7); ChannelCount=2, Version=2 |

Older (pine) schema additionally has `Name` (RemoteableString), `BeatTrackState`, `PitchMarks`,
`OnSets.{Bpms,Probabilities,InitialBPM}`, `EstimatedDownBeatLocation`/`EstimatedBarLength` (bare
ints at top level) and no AufTaktData.

## Doc 2: `AufTaktData`

Single-class document duplicating doc1's AufTaktData (unset in no_warp/318 KB chunk in with_warp).
Present in both Live-12 fixtures, absent in pine. Likely a fast-access copy.

## Warp markers

**In the two Live-12 auto-warp fixtures, no explicit warp markers were written into the `.asd`.** The two Live-12
fixtures differ *only* in AufTaktData content (first differing byte 0xF366 = AufTaktData.
PreprocessedDataChunk count) — the grid Live shows after auto-warp is derived at load time from
the opaque AufTaktData blob. `IsWarped=1`, `MarkersGenerated=0`, `WarpMarkers=[]` in both.

Explicit markers (written when the user adjusts warp and Live saves the clip defaults) use the
standard list encoding, as observed in a real marker-bearing Live-12 file: 32-byte record chains
of `00 0A "WarpMarker"` + u32 index + f64 SecTime + f64 BeatTime, a u32 count immediately before
the first record, and the `WarpMarker` class def earlier in the file (which reads as a decoy
record with id=2 — 2 is its field count):

- schema table gains `class WarpMarker { SecTime: double(0x17), BeatTime: double(0x17) }`
  → its def starts `00 0A WarpMarker 02 00 00 00 …`;
- data: `u32 count` + per marker `00 0A WarpMarker` + u32 index (0,1,2…) + SecTime f64 +
  BeatTime f64 + final `00 00` terminator.

SecTime = seconds into the audio; BeatTime = beats. Constant tempo ⇒ two markers pin the grid:
slope beats/second = BPM/60. There is **no explicit tempo/fixed-tempo field** anywhere in the
Live-12 SampleData schema — tempo is representable only via ≥2 markers (or Live's own AufTaktData).

## Grid-writing strategy

`write_grid` writes two markers: the anchor at beat zero, then the WAV's end at the requested tempo. If duration is unavailable, the second marker is one bar after the anchor. It inserts the WarpMarker schema entry and sets `IsWarped=1`, `MarkersGenerated=1`, and the final header byte to `1`. The header flag is necessary for Live to load the grid.

Zero-valued `LoopEnd`, `HiddenLoopEnd`, and `OutMarker` take the second marker's beat position; otherwise enabling saved defaults creates a zero-length clip. Existing nonzero end positions are preserved. AufTaktData is reset in both documents; other analysis data is preserved on rewrite. Cold synthesis sets OriginalFileSize, ExtraLength=0, and leaves analysis sections unset/empty. WAV is the runtime-tested source format.

## Live acceptance: 2026-11-03

Observed in Live 12.4.25b1 on Windows, importing fresh audio paths through `ClipSlot.create_audio_clip` and reading clip fields back through Live's Python API. The source is an eight-second, 44.1 kHz PCM WAV. Each probe has its own audio path to avoid reusing a loaded sample.

The control import produced a 120 BPM grid. Saving that clip's defaults with Live's Save button changed the final header byte from `0` to `1`, as well as writing the clip's bounds and markers.

The decisive A/B test used a 137 BPM grid anchored at 0.321 seconds. Each B file differed from its A file in **only the final header byte**. “Cold” means a sidecar created from scratch for previously unanalyzed audio:

| Sidecar | Flag 0 | Flag 1 |
|---|---|---|
| Live-generated analysis rewritten with our grid | Grid ignored; sidecar retained | Exact authored markers loaded; sidecar retained |
| Cold mono, 15-byte header | Grid ignored; sidecar replaced | Exact authored markers loaded; sidecar retained |
| Cold stereo, 17-byte header | Grid ignored; sidecar replaced | Exact authored markers loaded; sidecar retained |

All three flag-1 clips contained `(0.321 seconds, 0 beats)` and `(8 seconds, 17.53371666666667 beats)`, giving exactly 137 BPM. This establishes the byte's observed effect; the internal field name is unknown.

The corrected `write_grid` was then tested end-to-end on new mono, stereo, and rewrite paths. All three loaded those exact marker values through the API, had positive end and loop positions of approximately 17.53371654 beats, and left the sidecar byte-for-byte unchanged. [Captured API values and sidecar hashes](../test/asd_fixtures/live-12-acceptance.json) record the results. Empty analysis sections and the inserted WarpMarker schema entry were accepted in these probes. Playback and waveform rebuilding were not tested.

## Remaining unknowns

- Runtime acceptance on other Live versions, other MP3 header classes and sample rates, and other compressed audio formats. The legacy fixture still round-trips; that does not establish legacy runtime acceptance.
- The full meaning of the leading table, channel-data payload, first two header integers, document ID 365, and MP3 ExtraLength.
- Stale detection beyond the OriginalFileSize field.
- Whether existing nonempty tempo analysis can override saved markers when it is retained. The writer clears it in these tests.

## MP3 alignment: 2026-11-03

An adjacent `<original audio filename>.asd` was tested on the original collection file, **L 33 – Chase.mp3**, in Live 12.4.25b1. Live loaded all three authored markers without modifying the sidecar. This MP3 is stereo, 44.1 kHz, with an Info frame and a gapless extension declaring 576 samples of encoder delay.

Cue Finder exports times in FFmpeg's timeline. The [crate exporter's existing correction](../abletoolz/dj_crates.py) adds `576 + 529 + 1152 = 2257` samples, or 0.051179138322 seconds, for this header class. ASD marker times need the same correction as ALC marker times. The format writer expects times already translated into Live's timeline; it does not apply decoder compensation itself.

The test used the catalog's corrected 172 BPM grid. The translated anchor is `(0.131471861142 seconds, beat 0)` and the drop is `(67.108216047188 seconds, beat 192)`. Those exact API values were read from the newly created clip using the original MP3 path.

Audio alignment was checked independently of marker serialization: Live recorded corrected and uncorrected copies simultaneously through internal Pre FX routing. A four-second passage around the drop was compared with the FFmpeg decode. The uncorrected copy was 2257 samples late; the corrected copy matched at zero samples of displacement, with correlation 0.999998817. [Captured values](../test/asd_fixtures/live-12-mp3-acceptance.json) retain the measurement. ALC generation from the same one-row Cue Finder TSV also produced the same corrected marker positions; ALC import was not retested in this run.

One ASD holds one set of clip defaults. Separate full/drop clip variants still require separate ALC files. This test wrote only one original sidecar; bulk ASD output is not wired into the crate CLI yet.
