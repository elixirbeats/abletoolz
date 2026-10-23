# Plugin mapping research

This is what we've looked at so far, including things that are only partly figured out. The [plugin tables](../abletoolz/plugins/MAPPINGS.md) show what you can use in abletoolz today.

The unfinished code is on `codex/plugin-mapping-research`. The new conversion design is separate from that work. Neither is needed to use the current converters.

Pro-53 → Prophet-5 V sounded close in the listening tests. The next step is turning that work into a converter, handling the two source variants and adding real presets as tests. We don't need to keep tuning the sound before doing that. Trash's tested distortion-only converter is already included; the other modules still need work.

The evidence paths below refer to the local research folders. The DLLs, recordings and full captures are kept there, rather than bundled with the library.

| Plugin/version | Mapping established | Conversion status / remaining gap | Local evidence |
| --- | --- | --- | --- |
| CS-80V V1, standard / 10 voices | Extracted 5392-byte buffers; V1 semantics unverified | Original V1 DLLs still needed; do not infer compatibility from length | `priority_plugin_mapping_20261021/cs80_focus_20261021/RESULTS.md` |
| CS-80 V2, standard / 10 voices | Bounded codec; 549 public controls verified across all 12 source states and four edited vectors; 460 additional static internal coordinates | Coupled setter effects and internal coordinates need further native validation | Same CS-80 report; `cs80_state.py` |
| CS-80 V3 / V4 | Saved-field/display maps; routing, polarity and DSP curves; headless renders and audition examples | Partial legacy preset projections tested; no packaged universal upgrader | CS-80 `routing_curves_20261021/`, `v4_version_map_20261021/`, `v4_routing_audio_20261021/` |
| NI Pro-53 / Arturia Prophet-5 V | Close user-auditioned core sounds; source/target maps and native oscillator, filter, envelope and Poly-Mod calibration | Ready to turn into a converter. Handle the 69-control engine and 70-control instrument variant, add real presets as tests, and say which settings are covered. | `pro53_source_20261021/`, `prophet5_target_20261021/`, `pro53_prophet_calibration_20261021/` |
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

| FabFilter Pro-C 1 → 2 | Historical prototype generates accepted target states, but source/target time, ratio and threshold curves remain assumed | Prototype retained on the research branch; release attempts using its old configured name refuse conversion. Establish source curves and unmapped audible settings before promotion. | `codex/plugin-mapping-research` |

Move each map or converter into the library when the part it supports works and has tests. It can still be a partial map. See [adding research to the library](PLUGIN_STATE_MODELS.md#promoting-research).
