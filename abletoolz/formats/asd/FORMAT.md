# Ableton Live 12 `.asd` format — reverse-engineered field map

Established by byte-exact structural parses of the fixtures in
`test/asd_fixtures/`:

- `no_warp_markers.asd` — Live 12, MP3 source, before auto-warp analysis
- `with_warp_markers.asd` — Live 12, the same MP3 after auto-warp
- `pine-021.wav.asd` — older Live, WAV source (legacy schema)

All three parse **to the last byte** with the rules below (verified: walker
end offset == file size). No public OSS project covers Live 12
(`AbletonParsing` explicitly states Live 12 is unsupported).

## Top-level file layout

```
offset 0:  02 bytes   file magic 06 49
offset 2:  u64 LE     N = leading-table entry count
offset 10: u32 LE ×N  leading table (see below)
then:      17 bytes   pre-doc constants: 00 00 00 00 | 64 00 00 00 | 04 00 00 00 | 00 00 00 00 | 00
                      (u32 0, u32 100, u32 4, u32 0, one 0 byte — identical in all 3 fixtures)
then:      1..n documents, back to back, each starting with magic AB 1E 56 78
EOF exactly after the last document.
```

### Leading table

Ascending u32 values, final entry 0 (padding/terminator, included in N). Values are **sample
positions** spanning the whole file (fixtures: N=11980, last real value 11,681,806 ≈ 4:24.9 @44.1 kHz;
pine: N=16, last real 13,492), average spacing ~975 samples ≈ 22 ms → interpreted as Live's
transient/segment grid (Beats-warp granularity). **Byte-identical between the warped and unwarped
fixture**, i.e. audio-derived, not warp-related. Meaning of individual entries not needed for grid
writing; preserve verbatim on rewrite. Cold synthesis writes N=0 (unverified — see open unknowns).

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

**Key finding: Live 12's auto-warp writes NO warp markers into the `.asd`.** The two Live-12
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

Rewrite/synthesize `SampleData` with: `IsWarped=1`, `MarkersGenerated=1`, `WarpMarkers` =
`[(anchor_s → beat 0), (second point at track end or +1 bar → slope = bpm/60)]`, WarpMarker class
def inserted into the schema table. On rewrite of a Live-generated file everything else is
preserved verbatim; AufTaktData is set to the unset sentinel so Live cannot prefer its own tempo
analysis over our markers. Cold synthesis additionally sets OriginalFileSize (+ ExtraLength=0,
WAV recommended as source) and leaves analysis sections unset/empty.

## Open unknowns (unverified against a running Live)

1. Does Live honor `.asd` WarpMarkers + IsWarped on first import/clip creation, or discard the
   file and re-analyze? If it re-analyzes, does that preserve the markers?
2. Does clearing AufTaktData to the unset sentinel actually stop Live from preferring its own
   tempo analysis over explicit markers?
3. Does Live tolerate cold-synthesized files: empty leading table, unset OnSets/AufTaktData/
   OverView (the waveform overview presumably builds lazily)?
4. `MarkersGenerated` semantics — which flag state tells Live "markers are authoritative"?
5. Placement of the WarpMarker class def within the schema table (inserted after
   RemoteableList's def; readers should treat the table as a name→def dictionary, but unverified).
6. Meaning of doc-header u32 = 365, pre-doc constants (100, 4), and ExtraLength=526 for MP3
   (decoder-delay-ish; transcoding to WAV sidesteps it).
7. Whether stale detection uses OriginalFileSize only, or also an mtime kept elsewhere
   (no mtime/sample-rate/sample-count field exists in the format).
