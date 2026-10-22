# Plugin maps and conversions

Coverage as of 2026-10-22. This inventory distinguishes reusable plugin knowledge,
implemented conversions, and experiments that still need integration.

A **plugin map** collects knowledge about one plugin/version. Its **byte maps**
describe saved fields; its **parameter maps** relate controls to those fields.
Codecs read and write the measured formats, which can contain several nested
encodings. **Converters** separately define a source-to-target correspondence.
See [the domain model](../../GLOSSARY.md) and
[state model interfaces](../../doc/PLUGIN_STATE_MODELS.md).

“Native checked” means a measured plugin consumed or saved the test state.
It does not establish sound equivalence for every preset. Coverage below is
limited to the recorded formats, builds, controls and fixtures.

## Packaged maps and codecs

Only the two Trash maps currently compose their knowledge into the new
`PluginMap` model. The other packaged codecs and tables remain available through
their existing interfaces.

| Plugin/version | Saved format and byte knowledge | Parameter knowledge | Implementation |
| --- | --- | --- | --- |
| iZotope Trash 1 | Bounded 16-program banks, revisions 21/22; revision 21 has 296 typed fields; revision 22 program contents remain opaque | 182 sampled public controls; 48 named algorithms across ten selectors | [PluginMap and bank codec](state/izotope_trash.py) |
| iZotope Trash 2, measured VST2 version word 2050 | Template-scoped codec; 64 saved fields; ten algorithm selectors; bounded single-band stage polarity edits | 16 executable normalized control curves; 52 named algorithm values; additional saved-field checks | [PluginMap and state codec](state/izotope_trash2.py) |
| FabFilter Pro-Q 1/2 and Pro-Q 3 | Legacy host float banks; Pro-Q 3 FFBS processor and FFed editor state | Version-specific EQ band/global layouts, units, shapes and slope correspondence | [FabFilter codecs and EQ maps](state/fabfilter.py) |
| FabFilter Pro-C 1/2 | Legacy host float banks; target FabF state encoding | 31 source control names; derived Pro-C 2 transfer table for 46 controls | [Compressor correspondence](state/fabfilter.py), [target table](state/data/pro-c-2.json) |
| FabFilter Saturn 2, Timeless 3, Volcano 3 | FFBS processor/editor framing; named editor-state support is product-specific | Full semantic control maps are not packaged | [FFBS and editor codecs](state/fabfilter.py) |
| Other legacy FabFilter float-bank products | Shared host `FxBk` bank codec preserves names and float values | Generic array structure; a product's presence here does not supply its control semantics | [Host bank codec](state/fxbk.py) |
| Arturia legacy Analog Lab, observed schema 7 | Root Boost text metadata and paired length-prefixed payloads; child state preserved opaque | Wrapper identity/version fields only | [Metadata codec](state/arturia.py) |
| Serato Sample | JSON saved state; sample-reference reads and edits | Sample paths and relinking metadata, rather than a full sound-control map | [Parser](parsers/serato.py) |
| Native Instruments Maschine 2 | Nested NI chunks and framed UTF-16 strings | Sample/kit references; read-only because ancestor lengths and offsets are unresolved | [Parser](parsers/maschine.py) |
| xfadelooper / xfadelooper.64 | Measured fixed 652-byte layout, preset name, sample path and 88 float words | Safe fixed-width sample-path edits; float control meanings remain unmapped | [Parser](parsers/xfadelooper.py) |

Generic format detection and framing helpers live in [state families](state/families.py).
Recognizing ZIP, compressed XML, JSON, a host bank or another envelope does not
establish the meaning of the enclosed plugin state.

## Implemented conversions

“Configurable” means the transform exists but the caller must provide a valid
target identity and any required controller state. “Prepared” means the caller
supplies a previously validated native result bound to the exact source hash.

| Source → target | Availability | What is implemented | Limits and evidence |
| --- | --- | --- | --- |
| Trash 1 → Trash 2 | [Explicit converter API](converters/izotope_trash.py) | Selected revision-21 program; two single-band distortion stages; 47 algorithm correspondences; physical gain/drive/style/mix values and required polarity | Experimental. Refuses active unmapped modules, multiband/multirate modes, unsupported values and algorithm 36. Returns notices for known differences. Five historical examples match native target state and closely match reference audio; other settings can differ. Container identity and automation are separate; no automatic set migration route. |
| FabFilter Pro-Q 1 → Pro-Q 3 | Configurable `custom:fabfilter-q1-to-q3` | Decode normalized bank, translate EQ values, emit target processor state; named controller encoder available | Historical listening evidence. The source band channel-placement encoding is unresolved: every band becomes Stereo, including source bands with non-stereo placement. Automation indices need separate handling. |
| FabFilter Pro-Q 2 → Pro-Q 3 | Configurable `custom:fabfilter-q2-to-q3` | Decode source bank and emit target EQ state | Native hosted checks; scope follows supported source shapes and modes. |
| FabFilter Pro-C 1 → Pro-C 2 | Configurable `custom:fabfilter-pro-c-1` | Explicit source/target control correspondence through the derived target table | Experimental; dropped controls and expanded enum domains require review. Does not promise identical compressor DSP. |
| FabFilter Pro-C 2 VST2 → VST3 | Configurable `custom:fabfilter-pro-c-2` | Re-encode host float bank into FabF processor state | Nine real patches restored byte-exact with all 46 controls agreeing in native checks. |
| FFBS-generation FabFilter VST2 → VST3 | Configurable `fabfilter` state policy | Separate processor state from editor framing | Product-specific identity/controller configuration required. Current measured products include Pro-Q 3, Saturn 2, Timeless 3 and Volcano 3. |
| kHs Distortion / Filter VST2 → VST3 | Built-in rules | Wrap payload in the target Kilohearts envelope | Historical listening checks. Other kHs products need explicit configuration and their own evidence. |
| Serum, Ghz Tupe 3, Serato Sample, Prophet V3, Timeless 3, Pro-Q 3 VST2 → VST3 | [Built-in identity rules](container/format_translation.py) | Rewrite device container/identity; seed rules declare verbatim state | Evidence differs per product; Pro-Q 3 seed identity alone has no listening confirmation. For FabFilter, prefer an explicitly measured state/controller policy for the actual source framing. |
| Measured iZotope VST2 wrapper → VST3 processor | Configurable `izotope` state policy | Remove validated length/name wrapper | [Wrapper transform](state/izotope.py); not an Ozone or Trash version upgrader. Ozone 4/8 Elements need further re-encoding; Ozone 9 Exciter has measured reframing evidence. |
| Native Instruments Massive 1.7 VST2 → VST3 | Configurable verbatim policy | Shared inner state accepted in both native formats | [Hosted compatibility evidence](state/measured.py); not a general NI version or 32-bit compatibility claim. |
| Previously validated native result → set device | [Prepared VST2 migration](migration.py) / [prepared VST3 recovery](recovery.py) | Apply exact source-bound state and measured target identity atomically | Used by retained Analog Lab and Surge experiments. Requires explicit prepared outputs; refuses unsupported configured/mapped/referenced parameters. No general preset translator is implied. |

The callable transform implementations for FabFilter are in
[state/fabfilter.py](state/fabfilter.py). Container rules and state transforms are
separate from plugin maps. [Measured compatibility](state/measured.py) records
evidence, including products for which no converter has been implemented.
Configured targets override built-in rules by source name. The compatibility
registry does not choose a different state policy automatically; selecting the
FFBS policy for Timeless 3 or Pro-Q 3 requires an explicit target override.

## Bundled wrapper recovery

These routes replace a wrapped VST2 reference with a pinned native x64 target.
They are limited to the source names, state hashes, selected programs and binary
hashes in [the route manifest](data/native_recovery.json). A route is not a full
parameter map or a newer-product conversion. Original x86 audio equivalence has
not been established by these route checks.

| Plugin | Recorded source states | Native recovery evidence | Boundary |
| --- | ---: | --- | --- |
| Audio Damage DubStation 1.5 | 23 | Byte-exact banks and selected program | Same product |
| Sonimus Satson Channel | 7 | Byte-exact banks and selected program | Same product |
| Sonimus Satson Buss | 1 | Byte-exact bank and selected program | Same product |
| Audio Damage Ratshack Reverb 2 | 6 | Byte-exact banks and selected program | Same product |
| Sonic Charge Permut8 | 3 | Program names and 540 bank-core float words preserved | Global controls 0–2 and trailer outside checks |
| FabFilter Pro-MB | 21 | Byte-exact banks and selected program | Same product |
| FabFilter Pro-G | 9 | Byte-exact banks and selected program | Same product |
| FabFilter Timeless 2 | 8 | Byte-exact banks and selected program | Does not convert to Timeless 3 |
| FabFilter Saturn 1 | 7 | All 899 saved float words preserved | Does not convert to Saturn 2 |
| ValhallaShimmer | 8 | All 12 parameter words and other numeric XML fields preserved | Decimal text can normalize |
| Sugar Bytes Effectrix | 20 | Native legacy-reader retention; scalar and nested-field checks; program-capacity metadata update | Two other banks excluded; parameter bindings and original-generation DSP remain unverified |

## Research maps awaiting integration

These maps and conversion experiments are retained under the local
`unfinished_sandbox/format_translation/` research tree. They are not installed
library capabilities. The artifact paths below are relative to that tree; some
are ignored research data and will not exist in a fresh clone.

| Plugin/version | Mapping established | Conversion status / remaining gap | Local evidence |
| --- | --- | --- | --- |
| CS-80V V1, standard / 10 voices | Extracted 5392-byte buffers; V1 semantics unverified | Original V1 DLLs still needed; do not infer compatibility from length | `priority_plugin_mapping_20261021/cs80_focus_20261021/RESULTS.md` |
| CS-80 V2, standard / 10 voices | Bounded codec; 549 public controls verified across all 12 source states and four edited vectors; 460 additional static internal coordinates | Coupled setter effects and internal coordinates need further native validation | Same CS-80 report; `cs80_state.py` |
| CS-80 V3 / V4 | Saved-field/display maps; routing, polarity and DSP curves; headless renders and audition examples | Partial legacy preset projections tested; no packaged universal upgrader | CS-80 `routing_curves_20261021/`, `v4_version_map_20261021/`, `v4_routing_audio_20261021/` |
| NI Pro-53 / Arturia Prophet-5 V | Source packed values; target fields and core oscillator/filter/envelope/poly-mod calibration; native Function envelope method with held-out checks | Mixed-wave phase, resonance, pulse balance and polyphonic behavior retain differences; no production cross-plugin converter | `pro53_source_20261021/`, `prophet5_target_20261021/`, `pro53_prophet_calibration_20261021/` |
| Arturia Analog Lab legacy | Root identity and child-payload structure; native import and sandbox-set experiments | Prepared state application supported; no general child-synth parameter translator | `analog_lab_20261020/` |
| minimoog V Original | 45 of 48 public controls have native-verified isolated float bindings | Three width controls showed no saved change; newer-version correspondence pending | `priority_plugin_mapping_20261021/resume_20261021/native_verification/minimoog_original.verified-control-map.v1.json` |
| Stutter Edit 1 | Full 128-slot bank reader; 203 typed coordinates per gesture; three typed edits verified natively | Most parameter semantics/curves need predictive checks; no Stutter Edit 2 converter | `priority_plugin_mapping_20261021/resume_20261021/stutter_edit/` |
| Lounge Lizard EP-4 | 77 of 82 public control bindings verified against the actual shared engine | Pickup rounding and coupled/inactive FX controls remain; combined 77-control vector pending | `priority_plugin_mapping_20261021/resume_20261021/RESULTS.md` |
| Lounge Lizard 3 | Bounded AASZ/zlib XML banks; 128 slots; 275 selected-program fields | Original DLL missing; no LL3 → EP-4 correspondence | Same resume report; `lounge3/structure_map.json` |
| Ohmicide | 100 controls mapped to big-endian float fields; two full predictive vectors match native state/getters/displays | Melo-record rewriting unresolved; no cross-build conversion | Same resume report; `verified_maps/ohmicide.verified-control-map.json` |
| Surge legacy / Surge XT | All 15 extracted legacy revision-10 states have native import evidence; bounded predictive edits | Prepared upgrade route remains limited to its prior 14 hashes and name guard; macro getter discrepancy unresolved | Same resume report; `recovery_continuation_20261020/surge_codec/prepared/manifest.json` |
| Korg LegacyCell | All 26 banks walked; 129 program records; final edited record and child envelopes retained | Child DSP consumption and newer Korg compatibility unverified | Same resume report; `legacycell/RESULTS.md` |
| Spectrasonics Trilogy | Bounded 60-scalar-record codec plus opaque instrument blob; 70 unique corpus states round-trip exactly | Scalar meanings, native edits, library dependencies and modern target correspondence unresolved | Same resume report; `trilogy/corpus_structure.json` |
| TimewARP2600 | One host bank: 28-byte name and 175 float words; exact round-trip | Original DLL missing; values are not all normalized; no newer-version transform | Same resume report; `timewarp2600/host_bank_map.json` |
| NI Absynth 3 | Source buffers inventoried | Original DLL missing; Absynth 5 legacy-reader acceptance and source semantics unverified | Same resume report |

For future additions, record the measured plugin/build, saved format, byte and
parameter coverage, native evidence, and the exact conversion scope separately.
An exact codec round-trip, accepted native state and matching audio answer
different questions.
