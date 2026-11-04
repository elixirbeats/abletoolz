# Developing abletoolz

If you want to use abletoolz in a script, start with the [Python tutorial](LIBRARY.md). This guide is for changing the library and teaching it about plugins it doesn't support yet. The [README](../README.md) covers the command line, and the [glossary](../GLOSSARY.md) explains the names used in the code.

- [Install a development copy](#install-a-development-copy)
- [Map a new plugin](#mapping-a-new-plugin)
- [Build an upgrader](#building-an-upgrader)
- [Salvage an old plugin](#salvaging-an-old-plugin)
- [Put the map in the library](#putting-the-map-in-the-library)
- [Find your way around the code](#how-the-library-is-built)
- [Research notes and design decisions](#research-notes-and-design-decisions)

## Install a development copy

Use Python 3.12 or newer. Plugin data and test fixtures use Git LFS, so install Git LFS before cloning, then install with [uv](https://docs.astral.sh/uv/) (all real python developers use uv):

```bash
git lfs install
git clone https://github.com/elixirbeats/abletoolz.git
cd abletoolz
uv venv
uv pip install -e ".[dev]"
```

The editable install runs this checkout's code, so changes you make are picked up without reinstalling, including features that haven't reached a release yet.

Run the checks from the repository root with `.venv` activated:

```bash
python -m pytest test
python -m ruff check .
python -m mypy abletoolz
```

## Mapping a new plugin

Most maps started with a plugin I used in my own projects. If yours is missing, a useful contribution can be as small as locating its sample paths or establishing one working format swap. You don't have to decode the entire synth first.

Support belongs to a particular plugin, version and operation. A plugin can be recognized without being convertible, or have readable sample paths without editable sound controls. Give a map the capabilities you've checked, and leave the others unavailable.

### What is already handled?

There are two kinds of wrapper to keep apart:

- Live's device XML says which plugin to load and carries its saved state. Abletoolz's container code handles the measured VST2, VST3 and AU shapes and their Live-version requirements.
- The saved state can have another envelope around the plugin's patch: a header, length prefix, archive or vendor/framework-specific framing. Several of these are already implemented in `plugins/state/framings.py` and the vendor modules.

A format swap may only need a different envelope around the same patch, but that must be checked for the specific plugin. Sharing a format or a vendor doesn't prove the payload is interchangeable. VST3 processor and controller state can both matter; don't assume an empty controller state will restore the same patch.

### Start with a pair of saves

1. Record the plugin version, platform and format, and save a baseline patch from the real plugin.
2. Save it again without changing anything. Differences here help identify timestamps, generated ids or other incidental data.
3. Change one control, save again, and compare with the baseline. Repeat at several values, including the ends of its range and switch boundaries.
4. Try independent patches. A field that seems to be at a fixed offset may move when a preset name gets longer or another module is enabled.

Separate the wrapper from the payload before interpreting differences. If the payload is compressed, compare its decoded contents; a small setting change can alter most of the compressed bytes. Preserve fields you haven't understood.

The names and numbers a host exposes can help join controls to saved fields, but a host's normalized 0–1 value is not necessarily the value written into the patch. A frequency may be stored as hertz, a logarithm or an index into a table. Changing one knob can also update several fields.

### Tools that help

[DawDreamer](https://dirt.design/DawDreamer/api_reference/index.html) can host plugins, change parameters, load and save state, and render audio from Python. [Pedalboard](https://spotify.github.io/pedalboard/reference/pedalboard.html) hosts VST3 plugins and, on macOS, Audio Units, with parameter and raw-state access. These are useful for repeating the same experiment across many values and presets.

A host's saved-state file can include its own packaging. Compare what the host exposes with what Live actually stores before treating them as the same byte sequence.

If experiments leave a field unexplained, [Ghidra](https://github.com/NationalSecurityAgency/ghidra) can help inspect the plugin's save/load routines and trace how it encodes a value. Native saves still provide the examples to check your interpretation against.

### Check a format swap

Capture equivalent patches from both formats and identify their plugin ids and state envelopes. Test whether the inner patch can be carried unchanged or needs a transformation. Check all state components, then load the result in the target plugin, save it back and inspect the values.

Finally, open a converted set in Live and listen. A host accepting the bytes is useful evidence, but it doesn't establish that the patch restored correctly in Live or that the sound is unchanged. Record which of these checks you've actually done.

## Building an upgrader

For a newer version of the same plugin, its own preset importer is a useful place to start. If the new version can import old presets, it can give you pairs of old and new states to study:

1. Save a known patch in the old version. Keep the original preset and its settings.
2. Import that preset through the newer plugin's own importer, then save it in the newer format.
3. Decode both states and compare. Look for controls that moved, changed scale, split into several controls or disappeared. New controls may need particular defaults to preserve the old behavior.
4. Repeat with varied presets and controlled changes in the old plugin. Include modulation, routing and enabled/disabled modules, not just the visible front-panel knobs.
5. Use some pairs to build the converter and keep other pairs aside to check it. Your own converted output should not be the only expected result in a test.

The importer may do more than copy numbers: it can renumber modulation slots, alter depths, change ranges or choose a compatibility mode. Prefer reproducing those observed choices over assuming that identically named controls store identical values.

There are two comparisons to make. First compare abletoolz's output with the newer plugin's own imported patch. Then compare the older and newer plugins' sound. Even a faithful reproduction of the importer can sound different when the vendor changed the engine.

Use the same MIDI or audio, sample rate and render conditions. Account for random oscillators, modulation, latency and effect tails when interpreting differences. Listen as well as inspecting settings. Record what doesn't carry, and surface approximations or dropped settings in the conversion's warnings.

If the newer plugin has no importer, pair controls through measurements of both plugins. Treat this as a separate converter with its own evidence, including any settings it cannot reproduce.

## Salvaging an old plugin

Some plugins have no newer version at all: the company is gone, the plugin only exists as 32-bit, or its copy protection no longer runs. The settings are still in your sets, though, and a map of the old plugin is what lets you get at them.

1. **Map the original.** Record its identity and how it saves its state. Many old VST2s save their whole bank: every program's name and control values, with the device's program number picking the one that plays. If you still have the plugin on an old computer, load it there and read each control's name and printed value. That confirms the order and the units, such as the Hz or dB a knob position means.
2. **Choose what it becomes.** That's a current plugin with similar controls (write a converter, as in [Building an upgrader](#building-an-upgrader)), or a rebuild of the plugin itself, for example a JUCE port written from its manual and measurements.
3. **If you rebuild it, have the rebuild read the original's saved state itself.** Then abletoolz only hands over the old patch, and the rebuild keeps every setting it understands. Keep its controls in the original's order and ranges, so automation and MIDI mappings can line up later.
4. **Map the target and declare the converter** between the two maps. Test with presets that came with the plugin, not your own songs.
5. **Check the result in the target:** load a converted patch and compare its controls with what the original printed. Then listen.

A map of the original is worth contributing even when the target is a private rebuild: anyone holding sets with that plugin can then read what its devices saved, and convert them however they choose.

## Putting the map in the library

A saved patch has three layers, and each has its own home:

1. **How Live carries it.** `container.device` reads any format's info element into one `PluginDevice` and writes it back. A change within a format edits the existing element; a change of format rebuilds it ([ADR 0001](adr/0001-two-write-modes-for-plugin-devices.md)). `plugins.conversion` works out which: `as_format` for the same plugin in another format, `as_plugin` through a converter.
2. **How the plugin serializes it.** A `PluginMap` in its vendor's module under `plugins/state/` holds the plugin's id and framing in each format, and the files its patch names. Each format also gets a `plan_name`, the word a plan file's `convert_to` takes (`serum_vst3`): written by hand, because the names Live shows aren't always what a musician would type, and checked for duplicates when the catalog loads. It is declared with `@register`, and `state.all_plugins` imports every vendor module into `CATALOG`. Framings shared across a framework or a vendor's range are in `state/framings.py`.
3. **What its values mean.** A byte map in the vendor's codec locates saved fields within the patch (`FixedByteMap` for fixed offsets), and `MappedState` reads and edits them by name.

A converter from one plugin to another is a function declared with `@converter(source, target, measured)` in `plugins/converters/`, keyed by the two map ids. It takes the source plugin's own patch and the selected program, and returns the target's; the two maps handle the formats on either side.

The short version of the measurement loop:

1. Save a baseline from the real plugin, change one control, save again and compare bytes. Identify headers, lengths, records and encodings.
2. Repeat across values and independent presets to separate control changes from names, UI state and incidental data.
3. Build a reader and writer that preserves unknown regions and updates affected lengths, offsets and coupled fields.
4. Predict an edit, load it in the plugin, save and read it back. Compare rendered audio when the claim is about sound.

The rules a map is held to:

- **Preserve what you don't understand.** Parsing and encoding without edits reproduces every byte. A partial codec can carry an `opaque_sha256` guard over the unmapped bytes, which refuses other templates instead of editing them.
- **Edits are all or nothing.** `MappedState.with_values` validates every requested value before writing a new copy.
- **An unsupported revision stays opaque.** It can be re-encoded, but it never borrows an older revision's field coordinates.
- **Evidence names its scope.** A byte round trip proves preservation, a native save and reload proves acceptance, and only a listen proves sound. Each map and converter records what was measured, on which build, and when.
- **Ship only finished work.** Unfinished codecs, converters and research stay on their own branches. Package data and tests run without a plugin, a DAW or a remote machine, and a fixture comes from a native save, not from the codec's own output.

The docstrings in `plugin_map.py`, `catalog.py` and `model.py` are the reference for each type.

## How the library is built

Every module sits at exactly one depth and depends only downward.

```
  apply_ops.py   describe.py        <- the doors: JSON in, JSON out
        |             |
      clips  devices  tracks  samples  plugins  transport
        |             |                              <- the domain: typed objects
      sections.py   sample_ref.py
        |                                            <- the mechanics: elements and ids
      xml_edit.py   persistence.py   settings.py   schema/
```

`live_set/document.py` is where the layers meet: `AbletonSet` owns the parsed tree and hands out one accessor per domain (`clips`, `devices`, `tracks`, `samples`, `plugin_files`, `files`, `plugins`, `transport`, and the callable `describe`). Accessors are the only way in; nothing reaches across to another domain's internals.

| Package | What it does |
| --- | --- |
| `live_set` | The set as typed objects, one module per domain (below). |
| `samples` | The sample and plugin files databases, and the lookup that relinks from them. |
| `plugins` | Plugin identity, the XML of plugin devices, plugin maps, converters and plans. |
| `schema` | Live's names: `Tag` and `Attr` for structure, generated settings enums for every native device and for tracks, mixers, clips and the transport, and Live's own schemas per version. |
| `formats` | The files abletoolz reads and writes, as pure codecs: Live's gzipped XML, hex data, VST2 banks. |
| `run` | One set per job and batches of them in worker processes, with the report, the console lines and the sidecar built from the same findings. |
| `machine` | Where Live, plugins and abletoolz's own files live on this computer. |

| `live_set` module | Layer | Job |
| --- | --- | --- |
| `describe.py` | door | a set as a tiered JSON document with clip content deduplicated into a pattern table |
| `apply_ops.py` | door | a JSON ops document validated whole, then run; one typed result per op |
| `clips.py` | domain | the MIDI note model, note reading and writing per schema generation, audio clips, arrangement placement |
| `devices.py` | domain | device chains per track, grafting a chain from a donor set, and `walk()` over every device of a kind, racks included |
| `tracks.py` | domain | every track, the main and cue tracks included, with their settings and their mixers'; view state, folding, colors, routing |
| `samples.py`, `sample_ref.py` | domain | sample references, missing-file checks and relinking; `sample_ref` holds the path shapes of each version |
| `plugin_files.py` | domain | the files plugin patches point at, through each plugin map's `file_paths` |
| `files.py` | domain | both kinds together: one check and one relink for every file a set points at |
| `plugins.py` | domain | the set's plugin devices and whether this machine can load them |
| `save_as.py` | domain | a copy for an older Live release and its report |
| `transport.py` | domain | the transport's settings, tempo and timeline length |
| `domain.py` | domain | the base every domain shares: the set it reads through, its version and its root |
| `sections.py` | mechanics | where clips live on a timeline: the `Events` lists, session slots, spans, the rules for clips crossing an edge, automation segments |
| `xml_edit.py` | mechanics | the set-wide Pointee id space, indentation, number formatting, remote bindings |
| `persistence.py` | mechanics | loading and saving, backup naming, file times |
| `settings.py` | mechanics | the one place a settings member meets the XML: resolves it for the set's version and fills list positions |

The rules it is built on:

- **Version differences live in two places and nowhere else.** A setting Live renamed or moved is one settings member that records every spelling, and `settings.resolve` picks the set's. Logic that genuinely differs by version gets a `@versioned` override, the way `clips.py` carries one `_note_attributes` per schema generation. Feature code never hardcodes a spelling or branches on a version number inline.
- **Readers and writers see the same clips.** Anything that finds arrangement clips goes through `sections.arrangement_events()`, so a reader never reports a clip no op can touch.
- **Ids are set-wide and Live checks them.** `Pointee`, every `*AutomationTarget` and `*ModulationTarget`, and `ControllerTargets.<N>` share one counter at `LiveSet/NextPointeeId`. A subtree grafted or cloned into a set is renumbered above it by `xml_edit.renumber_pointee_ids`, the only thing that does this. Clip-local ids, `KeyTrack/@Id` and `MidiNoteEvent/@NoteId`, are a separate space.
- **A write path is not done until Live has loaded it.** The version fixtures under `test/version_fixtures/skeletons/` are XML oracles that crash Live when opened; verify on a real set.
- **Sections are never trimmed.** A clip crossing a section edge is reported and left alone.
- **Nothing returns English to a program.** Writers answer with typed reports, and `run/console.py` renders the prose from the same fields.

The set model does not yet read locators, scenes or the time signature, so `transport.length()` assumes 4/4. `describe()` leaves out mixer settings and automation, which are reachable through `setting()` and `sections.py`.

## Inside the plugins package

`abletoolz.plugins` answers three separate questions about a plugin device: which plugin it is (`identity`), how Live writes it into a set (`container`), and what its saved patch holds (`state`). `converters` turns one plugin's patch into another's, `plan` is what every plugin device in some sets can become and applying what you pick, and `config` is the `config.yaml` they read. The package docstring explains how the parts fit.

### The plugin database

It reads Live's own plugin database, then adds the VST3 class ids from `plugin_database.uid_db` and from the vendors' `moduleinfo.json` files in this OS's standard plugin folders and the folders under `plugin_database.paths` in `config.yaml`. A plugin Live has never scanned is in it only when one of those names its class id. Each rebuild replaces the file, so an uninstalled plugin leaves it.

`read_plugin_db(path)` only reads the file, and fails when there is none. Whether Live can load a plugin right now is never stored, because it changes when a plug-in folder is switched off in Live's preferences: `abletoolz.plugins.loadable` asks Live once per run.

### Plugin settings

`decode_state` takes an explicit decoder and `set_state_model` encodes before it changes the XML; neither guesses the format, changes the plugin's identity or rewrites automation. `program_number` is Live's zero-based `ProgramNumber`; a bank alone does not say which program plays, so the caller chooses one. An edit of a VST3's processor state leaves its controller half as it was.

## Inside the file databases

`--db` and `database.create_or_update_db` scan the supplied content folders once and fill two catalogs: audio in the sample database, and other suffixes declared by plugin maps in `plugin_files.sqlite3` beside it. `--db` uses `file_database.paths` when no folders are supplied; Python callers pass the folder list explicitly. Users must include their instrument, kit, ensemble and other content libraries; `plugin_database.paths` instead points to installed plugin binaries. Python callers pass `load_plugin_files_db()` alongside `load_db()` when relinking both kinds.

- **Only the folders you pass are refreshed.** Records under other folders stay, including drives that are not connected. Files gone from a scanned folder leave the database.
- **Rescans are fast.** A file whose size, date and file id are unchanged keeps its checksum, so a rescan only lists folders. A first scan reads up to 16 KiB of each indexed file.
- **Database writes are transactional.** Each catalog refresh commits its changes together or rolls them back on failure. The audio and plugin-file catalogs are updated separately.
- **A scan skips** file and directory links, and the `._` files macOS leaves beside every file it copies to a foreign drive.
- `backend="portable"` uses `os.scandir` instead of the platform's native directory reader; the results are the same.


## Research notes and design decisions

- [Plugin device writes](adr/0001-two-write-modes-for-plugin-devices.md) explains why a format swap rebuilds a device's info element while same-format edits preserve unknown fields.
- [ASD format notes](ASD_FORMAT.md) describe the sidecar writer, its binary layout, and the Live acceptance checks.
