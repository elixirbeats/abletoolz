# Live 11 → 12 schema diffs

What actually changed in the `.als` XML between Live 11 and 12, harvested by
structurally diffing real sets with `test/tools/schema_census.py`. This is the
ground truth behind the `RENAMES` table in `scripts/gen_vocab.py` and the raw
material for a future down-version converter.

## Version gate (root attributes)

The `Ableton` root element carries the compatibility gate older Live versions
check before opening a file:

| attribute | Live 11 | Live 12 | Live 12 beta |
|---|---|---|---|
| `MajorVersion` | `5` | `5` | `5` |
| `MinorVersion` | `11.0_11300` | `12.0_12203` | `12.0_12402` |
| `SchemaChangeCount` | `7` | `3` | `5` |
| `Creator` | `Ableton Live 11.3.42` | `Ableton Live 12.2.6` | `Ableton Live 12.4.5b10` |

`MinorVersion` encodes `<line>_<build>`; `SchemaChangeCount` resets per line
and increments as the schema evolves within it. Compatibility is
one-directional by design: newer Live opens old sets, old Live refuses new
`MinorVersion` values. Any down-version write must rewrite all four
attributes, not just `Creator`.

## Renames (the master/slave purge + typo fixes)

Live 12 renamed a family of tags. Where one names a setting abletoolz
measures, the setting's name finds both spellings (`schema.settings.path_for`);
the census checks each declared rename against the corpus. The rest are
confirmed but untouched:

| Live 11 | Live 12 | resolved by name |
|---|---|---|
| `MasterTrack` | `MainTrack` | yes (`tracks.main()`) |
| `ViewStateSesstionTrackWidth` | `ViewStateSessionTrackWidth` | yes (`Mixer.VIEW_STATE_SESSION_TRACK_WIDTH`) — exact boundary unconfirmed: the corpus has no 12.0 sets and its earliest Live 12 set (12.1.5) already has the fix; the floor sits at 12.0.0 until a real 12.0.x set proves otherwise |
| `ColorIndex` | `Color` (renamed in 11.0, listed for completeness) | yes (`Track.COLOR`, `Clip.COLOR`) |
| `AutoColorPickerForReturnAndMasterTracks` | `AutoColorPickerForReturnAndMainTracks` | no |
| `ReWireSlaveMidiTargetId` | `ReWireDeviceMidiTargetId` | yes (`Track.RE_WIRE_DEVICE_MIDI_TARGET_ID`) |
| `TrackSendHolder/Active` | `TrackSendHolder/EnabledByUser` | no — kept as two settings: the 12.1 sets in the corpus carry neither, so it is not clear one replaced the other |
| `ScaleInformation/RootNote` | `ScaleInformation/Root` | no — kept as two settings: the values seen do not line up (0–11 against 0–3) |
| `ViewStateSessionMixerHeight` | `ViewStateSessionMixerVolumeSectionHeight` | no |
| `ExpressionLane` | `MidiEditorLaneModel` | no |
| `SongMasterValues/SessionScrollerPos` | `SessionScrollPos` (flattened to `LiveSet` level) | no |

Removed outright in 12: `VelocityDetail`, `ChooserBar`,
`ViewStateArrangerHasDetail`, `ViewStateSessionHasDetail`,
`ViewStateDetailIsSample`, and the coarse `ViewStates/ArrangerMixer` /
`SessionMixer` toggles (split into per-section flags:
`ArrangerMixerVolume`/`IO`/`Sends`/`Returns`/`CrossFade`/…).

## Additions in 12 (would need stripping on down-conversion)

- **Tuning systems**: `TuningSystems` at set level, `IsTuned` per track,
  `MpePitchBendUsesTuning` sprinkled into every Mixer, routing, sequencer,
  and PluginDevice element.
- **Wrappers**: `ArrangementClipsListWrapper`, `TakeLanesListWrapper`,
  `GroovesListWrapper` — empty scaffolding elements on every track and pool.
- **Mixer/device extras**: `BreakoutIsExpanded`,
  `CrossFadeState/MidiControllerRange`, `KeepRecordMonitoringLatency`,
  sequencer `ComplexPro*`/`Transient*` modulation targets.
- **GroovePool** gained `DefaultGrooveId` + nested `Grooves/Groove` shape.
- **Track attributes**: `SelectedToolPanel`, `SelectedGeneratorName`,
  `SelectedTransformationName` on every track element (MIDI tools).
- Misc view state: `NoteSpellingPreference`, `NoteAlgorithms`,
  `WaveformVerticalZoomFactor`, per-window clip/device detail flags.

12 stable → 12 beta is additive only (e.g. `UserTempoAutomation`,
`AutomationEnvelopesListWrapper`, device `Modulation_*` params) — no renames,
which is why one `(12, 0, 0)` floor covers both in `RENAMES`.

Feature paths abletoolz relies on — `Tempo/Manual`, `FloatEvent`,
`TrackUnfolded`, `IsFolded`, `LaneHeight` (inside `AutomationLane`),
`CurrentEnd`, `SampleRef/FileRef/Path`, `PluginDesc` — are all unchanged
between 11 and 12.

## Potential feature: down-version converter (`--convert-to 11`)

Experimental idea, unbuilt. Most 11→12 changes are renames plus additive
elements, and Live tolerates missing optional elements far better than
unknown ones, so a 12→11 morph looks feasible:

1. Rewrite the four root attributes to the target line.
2. Reverse-apply the rename table above.
3. Strip 12-only elements and attributes (the additions list).
4. Leave unknown plugin devices alone — an unsupported plugin fails to load
   in older Live, which is non-fatal.

Open questions before building it: whether stable Live 11/12 actually
rejects leftover unknown elements or silently drops them (needs a live
test), and what `SchemaChangeCount` value the target line expects. Ships
behind a loud disclaimer if it ships at all.
