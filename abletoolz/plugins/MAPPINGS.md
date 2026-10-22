# Plugin maps and conversions

Supported capabilities as of 2026-10-22. Each entry describes its implemented
scope; unknown state is preserved and unsupported operations are refused.
Unfinished implementations stay on development branches. See the separate
[partial research and integration status](../../doc/PLUGIN_MAPPING_RESEARCH.md)
for progress that is not yet an installed capability.

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
| FabFilter Pro-C 2 | Legacy host float banks; target FabF state encoding | Derived transfer table for 46 controls | [Compressor codec](state/fabfilter.py), [target table](state/data/pro-c-2.json) |
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
target identity and any required controller state; see the
[configuration example](../../README.md#plugins). “Prepared” means the caller
supplies a previously validated native result bound to the exact source hash.

| Source → target | Availability | What is implemented | Limits and evidence |
| --- | --- | --- | --- |
| Trash 1 → Trash 2 | [Explicit converter API](converters/izotope_trash.py) | Selected revision-21 program; two single-band distortion stages; 47 algorithm correspondences; physical gain/drive/style/mix values and required polarity | Distortion-only scope. Refuses active unmapped modules, multiband/multirate modes, unsupported values and algorithm 36. Returns notices for known differences. Five historical examples match native target state and closely match reference audio; other settings can differ. Container identity and automation are separate; no automatic set migration route. |
| FabFilter Pro-Q 1 → Pro-Q 3 | Configurable `custom:fabfilter-q1-to-q3` | Decode normalized bank, translate EQ values, emit target processor state; named controller encoder available | Historical listening evidence. Stereo bands supported; non-stereo band placement is refused. Automation indices need separate handling. |
| FabFilter Pro-Q 2 → Pro-Q 3 | Configurable `custom:fabfilter-q2-to-q3` | Decode source bank and emit target EQ state | Native hosted checks; scope follows supported source shapes and modes. |
| FabFilter Pro-C 2 VST2 → VST3 | Configurable `custom:fabfilter-pro-c-2` | Re-encode host float bank into FabF processor state | Nine real patches restored byte-exact with all 46 controls agreeing in native checks. |
| FFBS-generation FabFilter VST2 → VST3 | Configurable `fabfilter` state policy | Separate processor state from editor framing | Product-specific identity/controller configuration required. Current measured products include Pro-Q 3, Saturn 2, Timeless 3 and Volcano 3. |
| kHs Distortion / Filter VST2 → VST3 | Built-in rules | Wrap payload in the target Kilohearts envelope | Historical listening checks. Other kHs products need explicit configuration and their own evidence. |
| Serum, Ghz Tupe 3, Serato Sample, Prophet V3, Timeless 3 VST2 → VST3 | [Built-in identity rules](container/format_translation.py) | Rewrite device container/identity; seed rules declare verbatim state | Historical listening checks. For Timeless 3, configure the FFBS policy and controller state when full editor-state preservation is required. |
| Measured iZotope VST2 wrapper → VST3 processor | Configurable `izotope` state policy | Remove validated length/name wrapper | [Wrapper transform](state/izotope.py); validated envelope conversion, rather than a product-version upgrader. |
| Native Instruments Massive 1.7 VST2 → VST3 | Configurable verbatim policy | Shared inner state accepted in both native formats | [Hosted compatibility evidence](state/measured.py); not a general NI version or 32-bit compatibility claim. |
| Previously validated native result → set device | [Prepared VST2 migration](migration.py) / [prepared VST3 recovery](recovery.py) | Apply exact source-bound state and measured target identity atomically | Requires explicit prepared outputs; refuses unsupported configured/mapped/referenced parameters. This applies a validated result rather than deriving new settings. |

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

**How to add a plugin:** Capture a baseline and single-control saves with a headless plugin host such as DawDreamer, then compare them with state extracted from a real set through `PluginDevice.saved_state`. Build a codec that preserves unknown data, describe the saved fields in a byte map, and record control units and curves in a parameter map. Check predicted edits by reloading them in the original plugin, and compare rendered audio when matching sound is the goal. Keep source-to-target rules in a separate converter, refuse unsupported settings, and add native reference fixtures and tests before moving the supported scope from a development branch into the package. See [the state model guide](../../doc/PLUGIN_STATE_MODELS.md) for the interfaces.
