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

`MinorVersion` encodes `<line>_<build>`, and `SchemaChangeCount` counts
schema changes within one `MinorVersion`, not within a Live line: Live 10.0.1
and 10.0.6 share `10.0_370` with counts 2 and 11, and 11.0 to 11.0.2 write no
count at all. The pair is the schema; `abletoolz.schema.structure.RELEASES`
holds the root attributes every measured release wrote. Compatibility is
one-directional by design: newer Live opens old sets, old Live refuses new
`MinorVersion` values ("Unsupported MinorVersion (12.0_12203)", Live
11.3.42). Any down-version write must rewrite all of them, not just `Creator`.

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

## Writing a set for an older release

`abletoolz.live_set.save_as` writes a copy of a set for an older release
(`resolve_target("11.3")`, then `AbletonSet.save_as`). What it does follows from
what Live 11.3.42 did with real Live 12 sets, opened on 2026-10-21:

| what the file held | Live 11.3.42 |
|---|---|
| a Live 12 header | refused: "Unsupported MinorVersion (12.0_12203)" |
| an 11 header, Live 12 attributes left in | refused as corrupt: "Unknown attribute 'SelectedToolPanel'" |
| an 11 header, everything newer removed but one Live 12 device | refused as corrupt: "Unknown class 'AutoFilter2' encountered" |
| written by `save_as` for 11.3.42: three 12.2.6 sets and one 12.4.5 beta set, two with AutoFilter2 | all four opened ("Loaded document was created by Ableton Live 11.3.42") |

So nothing newer than the target may stay, devices included: unlike a missing
plugin, an unknown device class takes the whole set down. The writer removes
every element and attribute the target never wrote, by the census grammar
rather than a per-feature list, and puts an empty rack of the same kind where
each newer device was, named `Missing: <device>`, its info text saying what
was there. Envelopes that pointed into anything removed go too. Renames and
moved settings are respelled rather than removed. There are no per-feature
downgrades to maintain: a new Live release needs a new census run.
