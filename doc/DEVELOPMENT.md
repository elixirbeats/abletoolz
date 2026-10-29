# Developing abletoolz

How the code is laid out, and how to add support for a new plugin. For using abletoolz, start with the [README](../README.md) and [the library guide](LIBRARY.md). The [glossary](../GLOSSARY.md) defines the words the code uses for plugin devices, plugin maps and Live versions, and [doc/adr](adr) records design decisions.

The plugin data and test fixtures are stored with Git LFS, so install `git lfs` before cloning. Run the tests with `python -m pytest test`.

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

## Inside the sample database

- **Only the folders you pass are refreshed.** Records under other folders stay, including drives that are not connected. Files gone from a scanned folder leave the database.
- **Rescans are fast.** A file whose size, date and file id are unchanged keeps its checksum, so a rescan only lists folders. A first scan reads 16 KiB of every file.
- **A failed scan changes nothing.** SQLite commits the whole scan at once.
- **A scan skips** file and directory links, and the `._` files macOS leaves beside every file it copies to a foreign drive.
- `backend="portable"` uses `os.scandir` instead of the platform's native directory reader; the results are the same.

## Mapping a new plugin

A plugin map teaches abletoolz one plugin. [How support varies](PLUGIN_CONVERSION.md#why-some-plugins-convert-and-others-dont) explains what a map has to establish for each operation; this section is where the pieces go.

A saved patch has three layers, and each has its own home:

1. **How Live carries it.** `container.device` reads any format's info element into one `PluginDevice` and writes it back. A change within a format edits the existing element; a change of format rebuilds it ([ADR 0001](adr/0001-two-write-modes-for-plugin-devices.md)). `plugins.conversion` works out which: `as_format` for the same plugin in another format, `as_plugin` through a converter.
2. **How the plugin serializes it.** A `PluginMap` in its vendor's module under `plugins/state/` holds the plugin's id and framing in each format, and the files its patch names. Each format also gets a `plan_name`, the word a plan file's `convert_to` takes (`serum_vst3`): written by hand, because the names Live shows aren't always what a musician would type, and checked for duplicates when the catalog loads. It is declared with `@register`, and `state.all_plugins` imports every vendor module into `CATALOG`. Framings shared across a framework or a vendor's range are in `state/framings.py`.
3. **What its values mean.** A byte map in the vendor's codec locates saved fields within the patch (`FixedByteMap` for fixed offsets), and `MappedState` reads and edits them by name.

A converter from one plugin to another is a function declared with `@converter(source, target, measured)` in `plugins/converters/`, keyed by the two map ids. It takes the source plugin's own patch and the selected program, and returns the target's; the two maps handle the formats on either side.

Work out a saved format by measuring it:

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
