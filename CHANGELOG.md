# Changelog

User-facing changes and migration notes, newest first. Current usage belongs in
the [README](README.md); `abletoolz --plugin-maps` prints plugin-by-plugin support.

## 2.2.0 — unreleased

Changes since 2.1.0.

### License

- abletoolz is now licensed under the Mozilla Public License 2.0 (MPL-2.0).
  Versions up to and including 2.1.0 remain under the GNU GPL v3.

### Sample indexing and recovery

- The sample database is SQLite, with indexed filename, size and CRC lookups and
  full-text filename/folder search through `--search-samples`. JSON sample
  databases are no longer read or written.
- `--db` with no folders scans `sample_database.paths` from config.yaml.
- `--db` skips a subfolder it can't open, such as `System Volume Information`
  on a drive root, with a warning instead of stopping the scan.
- `--fix-samples-collect` is refused with `--output`: collecting copies into each
  set's own project, which `--output` leaves alone.
- `--db` refreshes selected folders incrementally. Unchanged files reuse their
  checksum, stale entries are removed within scanned folders, and failed SQLite
  updates roll back. Entries outside the selected folders stay in the database.
- A missing file in the database is never used to fix a set. When fixing runs
  into some, it warns once per set; `--db-prune` removes every file from the
  database that no longer exists.
- Native directory batches on Windows and macOS reduce discovery calls; Linux
  uses `scandir`. `--db-backend portable` selects the portable reader.
- The database stores the same checksum Live stores for each sample, so size plus
  a nonzero checksum finds a renamed sample; a checksum of `0` is treated as unknown.
- Sample references now handle legacy hex paths, transitional path representations
  and `OriginalCrc` metadata. Empty sample slots are skipped and relative-only
  references are retained. Hex text decoding also handles whitespace in saved data.

### Batch processing and records

- Set parsing uses worker processes. `-j` / `--jobs` accepts a positive count or
  `auto`, with available CPUs as the default and set count/executor bounds.
  `--db-workers` separately controls checksum reader threads. A run with many
  workers no longer runs out of memory: numpy loads only for `--gradient-tracks`,
  with one math thread per worker.
- Workers share a frozen database revision, duplicate input sets are processed
  once, and each set's captured console output stays together.
- Cached sidecar results refresh when plugin support code, shipped data,
  configured plugin databases, configuration or machine context changes.
  User notes and custom fields survive. Legacy sidecars refresh on their next scan.
- A plugin-check-only rescan answers an unchanged set from its sidecar without
  opening it, so a second pass over a library takes a fraction of the first.
  Sample checks always look at the disk.
- Sets Live 8.2's betas saved in Live 8.1's layout are refused as unsupported,
  like older sets, instead of failing partway through a run. A run report's
  `error` for a set it could not read says why.
- Run reports list every missing file a relink found but left as it was, with the
  reason, under `refused`.
- Saving in place copies the original to `abletoolz_backup` and then replaces it in
  one step, so an interrupted save leaves the original set where it was. A failed
  write is reported as that set's error instead of a success.
- A clip file's sidecar is `<name>.alc.meta.yaml`, so a set and a clip of the same
  name keep separate records.

### Plugins

- `plugins plan` writes a short file listing every plugin in the sets, each with
  `plugin`, `format` and `convert_to: null`, and the names it can become in a
  comment above it. Writing one of those names, exactly, converts every device of
  that plugin; `plugins apply` reads the sets again and converts. Every format in
  a plugin map carries its plan name, written by hand (`serum_vst3`), and two that
  share one stop the catalog loading.
- `plugins plan` and `plugins apply` read sets on every CPU core, like `-j` does
  for the other commands.
- A conversion never refuses over what it can't carry: automation and clip
  envelopes, MIDI and macro mappings and an arranged list of controls are removed,
  settings with no match in the target are left out, and each is said in the plan
  and under the device after apply; see
  [what converting costs](doc/PLUGIN_CONVERSION.md#what-converting-costs). Only a
  device that can't be converted at all stays as it was, with the reason.
- In Python, `file_targets` and `experiments` add what a plan file never offers:
  another installed file of the same VST2, and an installed plugin named like an
  unmapped one, never from another company.
- Plugin maps are found by stored identity and describe supported formats,
  state framing and file references. Converters separately name source and target
  maps. Coverage includes measured AU/VST2/VST3 crossings, newer-plugin conversions
  and bounded native recovery routes; each map and converter records its own.
- FabFilter Pro-L 2, Pro-MB, Pro-G and Pro-DS devices convert from VST2 to VST3,
  like Pro-C 2. The plugin reads every converted patch back as written; Pro-DS
  can round one value to its own last bit.
- FabFilter Pro-C devices, its side-chain build and the jBridged `.64` copies
  included, convert to Pro-C 2, each control matched by what both plugins print.
  An attack or release past Pro-C 2's range is written at its end and said.
- FabFilter Pro-L devices and their jBridged copies convert to Pro-L 2 the same
  way, with Pro-L 2's true-peak limiting off as Pro-L limited sample peaks.
- FabFilter Saturn devices, the x64 and jBridged builds included, convert to
  Saturn 2 the same way, its modulation slots and new controls included as
  FabFilter's own importer sets them.
- FabFilter Volcano 2 devices convert to Volcano 3 the same way, filters laid
  out and modulation depths scaled as FabFilter's importer does, a depth on a
  gain moving the gain with it. Banks from the later 757-control Volcano 2 build
  are left as they are.
- FabFilter Timeless 2 devices convert to Timeless 3 as FabFilter's own importer
  converts its presets: the two delays become one with a time pan, their
  feedback one feedback and cross mix, and modulation keeps the slots Timeless 3
  can hold.
- Serum 2 is supported as a VST3 and an Audio Unit, and converts between the
  two. Serum's Audio Unit from a Mac set converts to its VST2 or VST3 with the
  patch; a plan marks that crossing unproved, since the AU side was only
  measured in the host rig.
- Experimental, not finished: Serum patches convert to Serum 2 the way Serum 2's
  own Serum preset import builds them, including routing, voicing, LFO shapes and
  modes, the mod matrix, the FX rack and embedded wavetables. Most conversions
  sound the same as Serum 2's own import. Drawn warp and distortion curves,
  velocity and note curves, custom tuning, and octave or semitone knobs set
  between steps don't carry yet, and each converted device names what it left
  out. Patches from Serum versions abletoolz hasn't measured, including its
  earliest layout, are refused. Audition before trusting.
- Pro-C to Pro-C 2 is labelled with Pro-C 2's refusal of Pro-C patches; the
  re-encode it used to cite was withdrawn when Pro-C's Threshold curve turned out
  to differ.
- A Pro-Q 1 converted to Pro-Q 3 keeps its output gain and pan, display range,
  linear-phase mode, channel mode, bypass, MIDI and analyzer settings, measured on
  the original plugin and read back by Pro-Q 3.
- A converted device gets the list of controls Live would make for the new VST3,
  read from the installed plugin.
- Audio Units can be written into sets from Live 11.2 on, rather than 11.3.41,
  following Live's own schemas for each version.
- `--check-samples` and the sample-fix flags also visit plugin-held files through
  their maps. Serato Sample, Serum 2, xfadelooper, Battery 4, Kontakt 5, Maschine 2,
  Texture, CMI V and Reaktor 6 support relinking; iZotope Iris and MPC Essentials files are
  reported for manual repair. `--db` also indexes the files only those plugins
  load (kits, instruments, ensembles, projects), into `plugin_files.sqlite3`
  beside the sample database. Collecting copies their audio into the project;
  those files are relinked where they are and never copied.
- Trash 2's 16 checked controls can be set by their public values, through curves
  that reproduce the plugin's own saves; Trash 1's 182 controls carry their sampled
  values and how far each was checked.
- Typed state models and byte maps support inspection and editing independently
  of conversion.
- Native Instruments Pro-53 devices are recognized, and their working patch reads
  and edits by control name in the units the plugin prints (semitones, cents,
  milliseconds, hertz, percent, on/off).
- Arturia Prophet-5 V devices are recognized and their 96 core controls read
  and edit by name in the plugin's units; they write as VST2 or VST3.
- Pro-53 converts into Prophet-5 V control by control, each by calibration,
  knob-by-knob renders of both plugins, a shared unit or switch, or a display
  match, and the converted device says which. The delay goes into Prophet-5 V's
  FX section, and master volume and the saved mod wheel carry over. Controls
  Prophet-5 V has no counterpart for, such as velocity and the delay's invert,
  are listed on the device. A patch that depends on one it cannot imitate, such
  as the high-pass filter mode, converts as near as Prophet-5 V allows and warns
  that it will likely sound different.
- KORG's legacy MS-20 devices are recognized, including jBridge wrappers, and
  their working patch reads and edits by control name in the units the plugin
  prints, patch-panel cables included.
- KORG Legacy Cell devices are recognized: each cell's synths, effects and the
  MS-20 patch inside it can be read.
- Arturia MS-20 V devices are recognized and their controls, voice mode and
  patch bay read and edit by name; they write as VST2 or VST3.
- Experimental: MS-20 and Legacy Cell patches convert into MS-20 V. Filters, VCO
  levels, pulse width and EG2 are matched knob by knob against renders of both
  plugins, other controls are calibrated or matched by panel position, and
  cables carry by jack name. A cell's effects and mixer, and cables MS-20 V has
  no jack for, are listed on the device. A cell holding only Polysix is refused.

### Library and authoring

- Named settings cover native devices, tracks, mixers, clips and transport across
  measured Live schemas, including setting operations in JSON edit documents.
- Whether a set's version has a setting is answered by Live's own schema of the
  set's `MinorVersion`, packed from the 203 schemas Live 12.4.7 ships, rather
  than by which versions the measured sets happened to include. Releases no
  measured set came from are covered where two releases around them share a
  schema, and a setting a real set held is never refused.
- Measured tables, plugin data and plugin state fixtures are JSON and binary
  files stored with Git LFS; a source checkout needs `git lfs install` before
  cloning.
- `AbletonSet.save_as()` writes copies for supported older Live releases and
  reports removed features and replacement devices. Unresolved required target
  structure is refused. This is experimental: some copies still crash Live.
- `plan_sets`, `write_plan`, `read_plan` and `apply_in_place` in
  `abletoolz.plugins.plan` do from Python what `plugins plan` and `plugins apply` do.
- `set_audio_output` refuses a track name other than the main or cue track; `Tag`
  in `abletoolz.schema` names them, and `Tag.MAIN_TRACK` finds the main track on any
  Live version.
- `AbletonSet.version` is set as soon as the set is parsed; it used to stay `None`
  until something else read the version first.
- The [library examples](doc/LIBRARY.md#5-do-it-for-a-whole-folder) work on whole folders of sets:
  `find_sets` finds them the way the command line does.
- The README, [library guide](doc/LIBRARY.md) and
  [plugin conversion guide](doc/PLUGIN_CONVERSION.md) are step-by-step guides that
  start simple. How the code is built moves to [doc/DEVELOPMENT.md](doc/DEVELOPMENT.md).

### Migration

- `--append-bars-bpm` and `--prepend-version`, deprecated in 2.1.0, are gone. The
  `.meta.yaml` sidecar beside each set records its bars, bpm and Live version.
- The sample database is now `sample_db.sqlite3`. JSON sample databases are no
  longer read: run `--db` on your sample folders to build the new one.
- `--db` no longer removes missing files outside the folders it scans; run
  `--db-prune` for that.
- `--repair-plugins`, `--upgrade-plugins` and `--suggest-plugin-mappings` are
  retired in favor of `plugins plan` / `plugins apply`.
- `--analyze-plugins`, `--fix-plugins` and `--list-parsers` are retired. Use
  `--dump-plugins` for inspection, the sample flags for plugin-held files, and
  `--plugin-maps` for supported capabilities.
- The `plugin_translation.targets`, `plugin_upgrades` and `plugin_parsers` sections
  of config.yaml are ignored, with a warning; a plan's `convert_to` holds the choices.
- Python modules have moved into `samples`, `run`, `schema`, `formats` and
  `plugins`. Update internal imports using the current library examples and
  [library guide](doc/LIBRARY.md).
- The `abletoolz.asd` package (the `.asd` parser, grid writer and librosa BPM
  detection) and the `analysis` install extra are gone. The format notes are in
  [doc/ASD_FORMAT.md](doc/ASD_FORMAT.md).

## 2.1.0 — 2026-08-23

Command names here describe that version; see the migration notes above for
their replacements.

- Added patch-preserving plugin repair/translation and an installed-plugin
  database, with `--repair-plugins` and `--suggest-plugin-mappings`.
- Added per-set YAML sidecars with user-owned status/notes, cached scan results,
  JSON run reports and output-copy workflows.
- Added macOS VST3 bundle discovery and Audio Unit registration checks.
- Expanded set authoring with MIDI notes, arrangement-section copying, device
  chain grafting and JSON describe/apply operations.
- Expanded sampler parsers for files held inside plugin state.
- Deprecated filename tagging with `--append-bars-bpm` and `--prepend-version`;
  sidecars record those facts without renaming sets.

## 2.0.0 — 2026-08-09

- Added Live 12 schema support, including renamed track and session-view elements.
- Split the command-line implementation into a Python library with `AbletonSet`
  accessors for transport, tracks, samples and plugins.
- Added the experimental plugin parser framework for inspecting and repairing
  saved plugin state.
- Serialized sets before backing up originals and writing replacements.
- Added concurrent batch processing, per-set failures and nonzero failure exits.
- Added DJ crate generation as pre-warped `.alc` clips from track-analysis TSVs.
- Required Python 3.12 or newer and modernized packaging and typing.

## 1.1.1 — 2023-03-25

- Packaging fixes for PyPI.

## 1.1.0 — 2023-03-25

The first release on PyPI: `pip install abletoolz` installs the `abletoolz` command.

- `--db` builds a database of your sample folders, and `--fix-samples-absolute`
  and `--fix-samples-collect` relink missing samples from it; collect also copies
  them into the project, like Live's collect and save.
- `--gradient-tracks` colors tracks and clips with random color gradients.

## Before PyPI (2020–2022)

Run from source with `python main.py`.

- 2020: checking samples and plugins, listing tracks, folding/unfolding tracks,
  track heights and widths, master/cue outputs and XML dumps, on one set (`-f`) or
  a folder of sets (`-d`). `-s` saves with the original moved to `abletoolz_backup`,
  and the set's creation and modification times are kept. `--append-bars-bpm`
  adds the length and bpm to the file name. Made for Ableton 10 sets, with some
  commands on Ableton 8 and 9.
- 2022: Ableton 11 sets, including the 11.1 beta.
