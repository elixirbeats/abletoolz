# Using abletoolz as a library

Everything the command line does, you can call from Python. This guide starts with opening one set and works up a step at a time, each step adding one new idea. Follow the numbered steps in order: later examples reuse imports and objects from earlier steps. Change the paths to your own, and start with a copy of a set while you get familiar with the library. You can do everything here from Python; the command line equivalents are only there for reference.

For installation and the command line, see the [README](../README.md). For adding support for a plugin, see [Mapping a new plugin](DEVELOPMENT.md#mapping-a-new-plugin).

For old sets that hold a binary document instead of XML, see the experimental [legacy set conversion](LEGACY_SETS.md).

1. [Open a set and look around](#1-open-a-set-and-look-around)
2. [Change something and save](#2-change-something-and-save)
3. [Find missing samples](#3-find-missing-samples)
4. [Fix them from your sample database](#4-fix-them-from-your-sample-database)
5. [Do it for a whole folder](#5-do-it-for-a-whole-folder)
6. [Change any setting by name](#6-change-any-setting-by-name)
7. [Write MIDI notes](#7-write-midi-notes)
8. [Check and convert plugins](#8-check-and-convert-plugins)
9. [Going further](#9-going-further)

## 1. Open a set and look around

```python
from pathlib import Path

from abletoolz.live_set import AbletonSet

song = AbletonSet(Path("D:/sets/my song.als"))
if not song.parse():  # reads the file and logs why if it cannot
    raise ValueError("Couldn't read the set; see the log for why")

print(song.version)  # Ableton Live 11.3.42
print(song.transport.bpm())  # 174.0
print(song.transport.length())  # 4:24, from the furthest clip (assumes 4/4)
for track in song.tracks.load():
    print(track.type, track.name)  # AudioTrack Drums, MidiTrack Bass, ...
```

`song` holds the whole set. Each part of it has its own attribute: `song.tracks`, `song.clips`, `song.devices`, `song.samples`, `song.plugins` and `song.files`. The steps below use them one at a time.

## 2. Change something and save

These are the command line's edit flags:

```python
song.tracks.set_audio_output(1, element_string="MasterTrack")  # --master-out 1
song.tracks.set_audio_output(2, element_string="PreHearTrack")  # --cue-out 2 (Live calls the cue track PreHearTrack)
song.tracks.unfold()  # --unfold
song.tracks.set_heights(68)  # --set-track-heights 68
song.tracks.gradient()  # --gradient-tracks

song.save_set()  # -s: the original is copied to abletoolz_backup first
```

The track names are the names Live uses in the set file, and you don't have to remember them: `Tag` from `abletoolz.schema` names every one, so your editor lists them as you type `Tag.`. Live 12 renamed `MasterTrack` to `MainTrack`, and either name finds the main track whatever version saved the set:

```python
from abletoolz.schema import Tag

song.tracks.set_audio_output(1, element_string=Tag.MAIN_TRACK)  # works on a Live 9 set too
song.tracks.set_audio_output(2, element_string=Tag.PRE_HEAR_TRACK)
```

A name that isn't the main or cue track is refused with an error.

Nothing is written until `save_set()`. To leave the original alone, `song.save_set(Path("D:/repaired"))` writes the changed set into that folder instead, like `--output`.

## 3. Find missing samples

```python
missing = song.files.check()
for ref in missing.samples:
    print("missing sample:", ref.name, ref.absolute)
for found in missing.plugin_files:  # samples and kits saved inside a plugin's own settings
    print("missing in", found.device, ":", found.name)
```

`song.files` is every file the set points at: Live's own samples, and the files plugins keep inside their settings, like a sampler's sample or a Kontakt instrument.

## 4. Fix them from your sample database

First build the database, the same one `abletoolz --db` builds. It lives in abletoolz's config folder. Run this again when you add samples; unchanged files keep their checksums:

```python
from abletoolz.samples import database

database.create_or_update_db(
    [
        "D:/Samples",
        "D:/sets",
        "D:/Kontakt Libraries",
        "D:/Maschine Content",
        "D:/Battery Kits",
        "D:/Reaktor Ensembles",
    ]
)
```

Use your own content-folder paths. This one call populates both databases: audio goes into the sample catalog, while file types declared by plugin maps (such as instruments, kits and ensembles) go into `plugin_files.sqlite3` beside it. Include every relevant content folder, or a parent containing them; these paths are unrelated to the installed-plugin inventory built by `--plugin-db`.

The SQLite catalog stores the same checksum Live saves with a sample reference, along with its name, size and date. Indexing reads checksums in parallel; pass `workers=4` to `create_or_update_db` if you want to limit the readers. Loading the catalog opens indexed lookups rather than reading the entire database into memory.

Then relink the set from it:

```python
with database.load_db() as db, database.load_plugin_files_db() as plugin_files_db:  # read-only catalogs
    relinked = song.files.relink(db, plugin_files_db, collect=False)  # --fix-samples-absolute

for fix in relinked.samples:
    print(fix.old, "->", fix.new)
for fix in relinked.plugin_files:
    print("plugin file:", fix.old, "->", fix.new)
for skipped in relinked.refused:  # found, but not sure enough to relink
    print("left alone:", skipped.what, "--", skipped.why)
song.save_set()
```

The lookup uses the information saved with each reference to check candidates. A conflicting size or checksum rules a candidate out. When it cannot resolve an ambiguous match, the reference stays as it was and appears in `refused`. Plugin-held paths often have less information than Live's own sample references, so the available evidence varies.

`collect=True` is `--fix-samples-collect`: found audio is copied into the project folder. In Python, you explicitly run the planned copies with `relinked.collector.run()` before saving the rewritten set. Calling `relink` alone does neither. Once you call the collector, it copies files even if you decide not to save afterwards:

```python
with database.load_db() as db, database.load_plugin_files_db() as plugin_files_db:
    # The second database holds the files only plugins load (Kontakt instruments, Battery kits, ...).
    relinked = song.files.relink(db, plugin_files_db, collect=True)
if relinked.collector is not None:  # None when nothing needed copying
    relinked.collector.run()
song.save_set()
```

Plugin instruments and kits stay in their library locations; collecting only copies audio.

`db.search("kick 174")` searches the database by file and folder names, like `--search-samples`.

When a relink runs into database entries for files that no longer exist, it skips them and warns once per set. `database.prune_db()` removes them, like `--db-prune`. Connect your sample drives first, since a disconnected file counts as missing.

## 5. Do it for a whole folder

`find_sets` finds sets the way the command line does, skipping backup folders:

```python
from abletoolz.live_set import find_sets

with database.load_db() as db, database.load_plugin_files_db() as plugin_files_db:
    for path in find_sets(["E:/Old Projects"]):
        song = AbletonSet(path)
        if not song.parse():
            continue  # not a set abletoolz can read; the log says why
        if song.files.relink(db, plugin_files_db, collect=False).relinks:  # only save sets that changed
            song.save_set()
```

This loop processes one set at a time so you can see each step. The command line's batch runner uses worker processes; parallel processing is not implied by `find_sets`.

## 6. Change any setting by name

Mapped settings of Live's own devices have names in `abletoolz.schema.devices`, and tracks, mixers, clips and the transport have theirs in `abletoolz.schema.objects`. Your editor lists them as you type: `Sampler.` then tab. The library resolves names against the set's Live schema, including settings Live renamed. Availability still depends on the device and version; a setting introduced in a newer Live is not added to an older set by naming it:

```python
from abletoolz.schema.devices import Device, EqEight, Sampler
from abletoolz.schema.objects import Clip, Mixer, Track

song = AbletonSet(Path("D:/sets/my song.als"))
song.parse()

# walk() finds every Sampler in the set, including ones inside racks.
for sampler in song.devices.walk(Device.SAMPLER):
    volume = sampler.setting(Sampler.VOLUME)
    print(sampler.display_name, volume.value)
    volume.set(-6.0)  # a value of the wrong kind (text for a number) is refused

# A setting that lives in a list, like an EQ band, takes its position.
for eq in song.devices.walk(Device.EQ_EIGHT, track=song.tracks.named("Bass")):
    eq.setting(EqEight.PARAMETER_A_GAIN, index=0).set(-3.0)  # first band's gain, in dB

for track in song.tracks.load():
    track.setting(Track.COLOR).set(14)
    track.setting(Mixer.VOLUME).set(0.5)
song.tracks.main().setting(Mixer.TEMPO).set(174)  # the tempo lives on the main track's mixer
for clip in song.clips.midi():
    clip.setting(Clip.LOOP_ON).set(True)
song.save_set()
```

## 7. Write MIDI notes

A note is a pitch, a start and a length in beats; everything else gets what Live gives a freshly drawn note:

```python
from abletoolz.live_set.clips import MidiNote

clip = song.clips.midi()[0]  # the first MIDI clip in the set
kick, hat, snare = 36, 42, 38
beat = [MidiNote(pitch, start=float(step), duration=0.5) for step, pitch in enumerate((kick, hat, snare, hat))]
song.clips.set_notes(clip, beat)  # replaces the clip's notes
song.save_set()
```

## 8. Check and convert plugins

Checking plugins needs to know about this computer: where Live keeps its own list of installed plugins, and where your plugin folders are. `Locations` holds those places:

```python
from abletoolz.machine import Locations
from abletoolz.plugins.loadable import oracle

here = Locations.this_computer()  # where Live, your plugins and abletoolz's config are on this computer
can_load = oracle(here)  # answers "would Live load this device here?" from Live's own plugin list

for plugin in song.plugins.scan(can_load):  # --check-plugins
    if not plugin.exists:
        print("missing:", plugin.name, "-- closest installed:", plugin.alternative)
```

### Make a plan and choose a conversion

Converting works like the command line: make a plan, choose what each plugin becomes, apply it. Only mapped format swaps, upgrades and recovery routes are offered. [The README](../README.md#converting-plugins) explains the plan file and the current conversion limits. In Python, a plugin's `picks` is its `convert_to`: names from its `targets`, best first, each device taking the first it can, and an empty list keeps the plugin as it is. A fresh plan fills it in for every plugin that doesn't load here, as `suggest` ranks what is installed. This example converts only Serum, choosing its VST3 for Serum VST2 devices when that target is offered, so it first clears the suggestions for every other plugin. Before applying, it prints the target's warnings and any refusal:

```python
from abletoolz.plugins.plan import apply_in_place, plan_sets, this_machine

machine = this_machine()
plan = plan_sets(["D:/Ableton Projects"], machine=machine)  # abletoolz plugins plan
for entry in plan.plugins:  # one entry per plugin, like the plan file
    entry.picks = []  # the plan suggested targets for every plugin that doesn't load; keep only Serum's
    if entry.plugin == "Serum" and entry.format == "vst":
        for target in entry.targets:
            if target.id == "serum_vst3":
                print(entry.plugin, target.id, target.warnings, target.refused)
                if not target.refused:
                    entry.picks = [target.id]
```

Nothing has been saved yet. Inspect the warnings before applying: conversions currently remove automation and MIDI/macro mappings from converted devices, and some upgrades approximate or omit settings. [What converting costs](../README.md#what-converting-costs) explains the limits.

The next examples show ways to refine or replace those choices. Choose how to save only after the plan has the choices you want.

### Keep one device unchanged

A plugin choice normally applies to every instance of that plugin and format. To treat one device differently, add an override with its set and device number from `entry.devices`:

```python
from abletoolz.plugins.plan import Override

serum = next(entry for entry in plan.plugins if entry.plugin == "Serum" and entry.format == "vst")
serum.picks = ["serum_vst3"]
device = serum.devices[0]  # choose the intended instance from this plan
serum.overrides.append(Override(set=device.set, device=device.device, picks=[]))  # no picks: keep it
```

### Pick by your own rule

Instead of choosing Serum by name, you can pick from what each target says about itself. This starts a fresh plan and selects same-plugin VST3 conversions with no reported warnings or refusals:

```python
from abletoolz.plugins.identity.ids import PluginKind
from abletoolz.plugins.plan import Route

plan = plan_sets(["D:/sets"], machine=machine)  # a fresh plan; previous picks are not reused
for entry in plan.plugins:
    free = [
        target.id
        for target in entry.targets
        if target.route is Route.FORMAT  # the same plugin in another format, not a different plugin
        and target.format is PluginKind.VST3
        and not target.refused
        and not target.warnings  # no known losses reported for these devices
    ]
    entry.picks = free[:1]
```

Add `and target.proved` to take only conversions someone has listened to. `entry.broken` tells you whether this machine can load the plugin as it is. `file_targets` and `experiments` in `abletoolz.plugins.plan` add targets a plan file never offers: another installed file of the same VST2, and an installed plugin named like one abletoolz hasn't mapped.

### Apply your choices

Use this block to save in place, or the next example to write copies elsewhere. They are alternative save steps; don't apply the same plan twice.

```python
for outcome in apply_in_place(plan, machine=machine):  # originals are copied to abletoolz_backup
    for device in outcome.devices:
        print(outcome.path.name, device.plugin, "done" if device.done else device.reason, device.warnings)
```

`device.warnings` says what each converted device lost or approximated. If you need to make more changes after saving, make a fresh plan from the saved sets.

If you want an editable plan file too, import `write_plan` and `read_plan` from `abletoolz.plugins.plan`. `write_plan(plan, "plan.yaml")` writes it, and `read_plan("plan.yaml")` reads the sets again with your saved choices. You can also restrict recovery-file searches when constructing the machine: `this_machine(["D:/VstPlugins"])`.

### Write converted copies somewhere else

`apply_in_place` saves over your sets, keeping backups. To leave them alone, `apply_plan` hands you each converted set instead, and you decide where it goes. After making your choices, this writes the copies into another folder:

```python
from abletoolz.formats import live_document
from abletoolz.plugins.plan import apply_plan

out = Path("D:/converted")
out.mkdir(exist_ok=True)


def save_copy(path, root):  # called once for each set that changed, with the converted set
    live_document.write_new(out / path.name, live_document.encode(root))  # refuses an existing file


outcomes = apply_plan(plan, save=save_copy, machine=machine)
```

If sets in different source folders share a filename, choose distinct output paths in `save_copy`; this simple example refuses a collision.

The examples pass the same `machine` to planning and applying, so both use the same view of installed plugins and recovery files.

A plan only offers what abletoolz already knows how to do: another format of a plugin, a recovery route, or a plugin it has a converter into. Anything else, like changing a plugin's settings, is code you write for that plugin: [Change a plugin's own settings](#change-a-plugins-own-settings) and [Rebuild a plugin as Live's own devices](#rebuild-a-plugin-as-lives-own-devices) show how.

## 9. Going further

These go deeper into the set and into plugins' own settings. Open the result in Live before trusting it.

### Copy a section of the arrangement

Clips and track automation inside the window travel together. A clip crossing either edge is reported and left alone, never cut:

```python
from abletoolz.live_set.sections import SectionMode

# Copy beats 64-128 (bars 17-32 in 4/4) to beat 448.
report = song.clips.copy_section(64.0, 128.0, 448.0, mode=SectionMode.REFUSE)
for placed in report.copied:
    print(placed.track_name, placed.clip_name, placed.start)
song.save_set()
```

### Copy a device chain from another set

Both sets have to be from the same major Live version:

```python
donor = AbletonSet(Path("D:/sets/donor.als"))
donor.parse()
# inventory() lists every track's device chain.
chain = next(track for track in donor.devices.inventory() if track.track_name == "Kick")
for device in song.devices.graft_chain(chain, song.tracks.named("Drums"), mode="append"):
    print("added", device.display_name)
song.save_set()
```

### Read what a scan found

A run that checks samples or plugins leaves a `.meta.yaml` beside each set. It reads back as an object, your own `status` and `notes` included, which is enough to sort through a whole library without opening a set:

```python
from abletoolz.run import meta

sidecar = meta.read(Path("D:/sets/my song.als"))
if sidecar is not None and sidecar.scan is not None:
    print(sidecar.scan.live_version, sidecar.scan.bars, sidecar.scan.bpm)
    print("missing plugins:", sidecar.scan.plugins_missing)
    print("missing samples:", sidecar.scan.samples_missing)
    print("yours:", sidecar.status, "--", sidecar.notes)
```

### Send a set to someone on an older Live

Once you've saved a friend's set in Live 12, their Live 11 refuses it. `save_as` writes a copy in the older release's format and reports what it had to remove. This is very experimental: expect Live to crash on some copies for now. If a copy won't open or crashes Live, please [open an issue](https://github.com/elixirbeats/abletoolz/issues) with the error you got, and attach the original set and the copy if you can, so we can fix it.

```python
from abletoolz.live_set.save_as import resolve_target

written, report = song.save_as(resolve_target("11"), output_dir=Path("D:/sets/For Live 11"))
print("wrote", written)
for change in report.removed + report.replaced:
    print(" ", change)
```

### Look up samples yourself

```python
from abletoolz.samples import relink
from abletoolz.samples.catalog import SampleCatalog

with database.load_db() as db:
    print(db.search("break 174", limit=25))  # file and folder words
    print(db.candidates("Kick 01.wav"))  # every file with that name: path -> name, size, checksum, date

    # Relink only to files you've picked: a catalog of just those, built in memory.
    approved = SampleCatalog.from_records(db.candidates("Kick 01.wav"))
song.files.relink(approved, collect=False)

# One lookup outside a set: the same rules a relink uses.
with database.load_db() as db:
    lookup = relink.find(db, relink.Evidence("Kick 01.wav", Path("C:/Old/Kick 01.wav"), size=42642, crc=28820))
print(lookup.found, lookup.candidates)
```

`crc.file_crc(path)` from `abletoolz.samples` gives the checksum Live stores for a sample.

### List the plugins on this computer

The plugin database is what `abletoolz --plugin-db` writes: every installed plugin with its format, maker and file.

```python
from abletoolz.plugins.config import load_config
from abletoolz.plugins.identity import plugin_db
from abletoolz.plugins.identity.ids import PluginKind

config = load_config(here.config_file)  # your config.yaml, for the extra plugin folders
plugins = plugin_db.load_plugin_db(config, here)  # the saved database, or a fresh read if there is none
for entry in plugins.installed(PluginKind.VST3):
    print(entry.name, entry.vendor, entry.module_path)
```

### Change a plugin's own settings

For plugins abletoolz has mapped, the settings saved inside the plugin can be changed by name. This sets the pre-filter frequency of every iZotope Trash 1 in the set:

```python
from abletoolz.plugins.container.device import PluginDevices
from abletoolz.plugins.identity.ids import PluginKind, stored_id
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.izotope_trash import Bank

for device in PluginDevices(song.root):  # every plugin device in the set
    # The plugin's id, as the set stores it, finds what abletoolz knows about it (its "map").
    stored = stored_id(device.identity)
    plugin_map = CATALOG.find(stored) if stored is not None else None
    if plugin_map is None or plugin_map.id != "izotope.trash1" or device.kind is not PluginKind.VST:
        continue
    selected = device.saved_state.program_number  # which of the plugin's programs is playing
    if device.controls.in_use or selected is None:
        continue  # automated or MIDI-mapped controls, or no program selected: leave it alone
    bank = device.decode_state(Bank.parse)  # read the plugin's settings
    device.set_state_model(bank.with_values(selected, {"PreFilter: Node 1 Freq": 823.25}))  # Hz
song.save_set()
```

A plugin's settings that abletoolz can't read raise `StateTransformError` instead of being guessed at.

### Rebuild a plugin as Live's own devices

Plans offer mapped format swaps and plugin-to-plugin conversions. Rebuilding a plugin as a native Live device is a separate recipe you write for that pair; this example shows how to use the saved settings as its starting point.

The same reading works across plugins. This reads the bands of every FabFilter Pro-Q 3 in a set and rebuilds them on an EQ Eight at the end of the same track. The EQ Eight comes from a set of yours where a track holds only an EQ Eight, and the Pro-Q 3 stays, so you can compare them in Live:

```python
from abletoolz.plugins.container.device import PluginDevices
from abletoolz.plugins.identity.ids import stored_id
from abletoolz.plugins.state.all_plugins import CATALOG
from abletoolz.plugins.state.fabfilter import PRO_Q3_BAND_SLOTS, FfbsState, Q3BandField

# Pro-Q 3 shape -> EQ Eight mode: bell, low shelf, low cut, high shelf, high cut, notch.
EQ_EIGHT_MODE = {0: 3, 1: 2, 2: 1, 3: 5, 4: 6, 5: 4}

donor = AbletonSet(Path("D:/sets/EQ Eight.als"))
donor.parse()
eq_eight = next(chain for chain in donor.devices.inventory() if [d.tag for d in chain.devices] == ["Eq8"])

for device in list(PluginDevices(song.root)):
    stored = stored_id(device.identity)
    plugin_map = CATALOG.find(stored) if stored is not None else None
    if plugin_map is None or plugin_map.id != "fabfilter.pro-q-3":
        continue
    # Pro-Q 3 saves 24 bands as one flat list of numbers, PRO_Q3_BAND_SLOTS per band.
    values = FfbsState.parse(device.saved_state.state).parameters
    bands = [values[start : start + PRO_Q3_BAND_SLOTS] for start in range(0, 24 * PRO_Q3_BAND_SLOTS, PRO_Q3_BAND_SLOTS)]
    used = [band for band in bands if band[Q3BandField.USED] and int(band[Q3BandField.SHAPE]) in EQ_EIGHT_MODE]

    target = song.tracks.named(song.plugins.track_name(device.info), main=True)
    (eq,) = song.devices.graft_chain(eq_eight, target)
    for index in range(8):  # EQ Eight has eight bands
        band = used[index] if index < len(used) else None
        eq.setting(EqEight.PARAMETER_A_IS_ON, index=index).set(band is not None and bool(band[Q3BandField.ENABLED]))
        if band is not None:
            eq.setting(EqEight.PARAMETER_A_MODE, index=index).set(EQ_EIGHT_MODE[int(band[Q3BandField.SHAPE])])
            eq.setting(EqEight.PARAMETER_A_FREQ, index=index).set(2 ** band[Q3BandField.FREQUENCY])  # saved as log2 Hz
            eq.setting(EqEight.PARAMETER_A_GAIN, index=index).set(band[Q3BandField.GAIN])  # dB
            # Q is saved as 0..1 over Pro-Q's range of 0.025 to 40.
            eq.setting(EqEight.PARAMETER_A_Q, index=index).set(0.025 * 1600 ** band[Q3BandField.Q])
song.save_set()
```

This is an approximation for inspection, not an equivalent replacement: it copies up to eight mapped band shapes and does not transfer dynamic behavior, automation or all Pro-Q settings. The original Pro-Q 3 remains active, so bypass one device when comparing them in Live.

## Experimental: JSON for programs

`describe()` reads a set out as JSON, and `apply_ops()` writes a batch of changes back. They were made for programs, such as a language model, to edit sets, and may still change. The command line has them as `--describe` and `--apply-ops`.

```python
from abletoolz.live_set.describe import DescribeLevel, describe_json

print(describe_json(song, DescribeLevel.PATTERNS))
```

`STRUCTURE` is the tempo, version and every track with its devices. `PATTERNS` adds the clips: a pattern placed sixteen times is written once and referred to by id. Notes are `[pitch, start, duration]`, with velocity and release velocity added when they aren't the default; `FULL` adds probability, deviation and whether the note is disabled.

A changes document lists changes in order:

```json
{"ops": [
  {"op": "copy_section", "src_start": 128.0, "src_end": 192.0, "dest_start": 384.0, "tracks": ["Drums"]},
  {"op": "set_notes", "track": {"name": "Bass"}, "clip": {"start": 384.0}, "notes": [[36, 0.0, 1.0], [43, 2.0, 0.5]]},
  {"op": "set_device_setting", "device": "SAMPLER", "setting": "VOLUME", "value": -6},
  {"op": "set_track_setting", "setting": "Mixer.VOLUME", "value": 0.5},
  {"op": "set_clip_setting", "track": {"name": "Bass"}, "clip": {"slot": 0}, "setting": "LOOP_ON", "value": true},
  {"op": "set_transport_setting", "setting": "LOOP_ON", "value": true}
]}
```

The changes are `set_notes`, `clone_clip`, `place_clip`, `copy_section`, `remove_section_clips`, `graft_chain`, `set_device_setting`, `set_track_setting`, `set_clip_setting` and `set_transport_setting`; their fields are the models in `abletoolz.live_set.apply_ops`. A setting change applies to every match in the set unless given a `"track"`, and takes an `"index"` for a setting in a list.

Every change is checked before the first one is made, and a change that fails while running puts back what the earlier ones did, so a document with one bad change changes nothing. Two tracks can share a name, so `{"name": ...}` is refused when it's ambiguous; `{"id": ...}`, the id `describe()` prints, always picks one, and `{"id": -1}` is the main track.
