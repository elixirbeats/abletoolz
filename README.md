![Abletoolz](https://github.com/elixirbeats/abletoolz/raw/master/doc/gradient.png)
# Abletoolz

Abletoolz is a Python command line tool to edit, fix and analyze Ableton Live sets. Primarily the purpose is to
automate things that aren't available in Live and make your life easier.
It can:
- Run on one set, or an entire directory of sets. So you can fix/analyze everything with one command.
- Color all your tracks/clips with random color gradients.
- Create a sample database of all your sample folders, which can then be used to automatically fix any broken samples in your ableton sets.
- Set all your Master/Cue outputs to a specific output, so if you buy a new audio interface you can fix all your master outs to point to 7/8 in one go.
- Validate all plugins in a set are installed.
- Analyze plugin state with plugin-specific parsers, and fix what they understand (Serato Sample's missing sample paths, for a start).
- Fold/Unfold all tracks, and/or set track height and widths.
- Prepend the set version name to the beginning of the file.
- Append the number of bars of the track, and the bpm to the end of the file.
- Dump the XML of the set, in case you want to dissect how they are structured or contribute to this project : )

It also:
- Moves your original set files to a backup folder before writing any changes, so you are never at risk of losing anything.
- Supports both Windows and MacOS created sets.
- Works on sets from Ableton 8.2 through Live 12, including current Live 12 betas (not every command works with the oldest versions).
- Preserves the original set modification time.

## What's new in 2.0

- **Live 12 support.** Live 12 renamed core elements (`MasterTrack` → `MainTrack`, and it finally fixed the
  historical `Sesstion` typo in track widths). 2.0 handles every version through the current 12 betas via a
  version-aware schema layer, and the test suite runs every feature against real-set fixtures from Live 9
  through the latest 12 beta.
- **Usable as a library.** The old monolith is now a small domain API: `AbletonSet` exposes `transport`,
  `tracks`, `samples` and `plugins` objects, so scripts can do what the CLI does programmatically.
- **Plugin parser framework** (experimental). A registry of plugin-specific parsers that can analyze, fix and
  upgrade plugin state inside sets: `--analyze-plugins`, `--fix-plugins`, `--upgrade-plugins`,
  `--dump-plugins` (for reverse engineering new formats), `--list-parsers`. The endgame is rescuing old
  projects: a set full of dead 32-bit plugins can be retargeted at the modern versions you actually have
  installed, and where a translator exists for the plugin's state format, your old settings come along instead
  of being lost. It can even translate one plugin into a different one once someone decodes both formats.
  Serato Sample is the first supported parser (finds and fixes its broken sample paths).
- **Safer saves.** Sets are serialized before anything on disk is touched — a failure can no longer leave you
  without your original file (which was always backed up, but still).
- **Honest batch results.** Directory runs process sets concurrently (`--jobs`), report `N ok, M failed`, and
  exit non-zero when something failed. Corrupt/truncated `.als` files are called out as such.
- **Bonus tools for DJs.** Warping a track collection by hand — dragging grid markers until every beat lines
  up — is hours of pure manual labor. `abletoolz-dj-crates` authors the grid for you from a TSV of per-track
  analysis (BPM, downbeat, drop cue) and builds a folder of pre-gridded `.alc` clips for your Live browser —
  drag one in and it's already beatmatched, with a second variant that starts right at the drop. Fair
  warning: the TSV schema currently comes from a companion cue-detection tool, so expect to massage your own
  analysis data into it. (`abletoolz.asd` has the `.asd` analysis-file format decoded too, warp grids
  included, but Live ignores externally written `.asd` files and regenerates them — so `.alc` clips are the
  delivery vehicle that actually works.)
- Python 3.12+ and a modern typed codebase.

## Installation:
Minimum python required 3.12

(https://www.python.org/downloads/)

Open a command line shell and make sure you installed Python 3.12+ correctly:
```
python -V  # Should give you a version
```
Once you verify you have python 3.12+, install with pip:
```
pip install abletoolz
```
This will install abletoolz as a command in your command line, you can now call `abletoolz` from anywhere if the
installation completed successfully. (Create an issue if you run into any errors please!)

## Usage:
`-h` Print argument usage.
`-v` Verbosity. For some commands, displays more information.

### Input - Parsing single or multiple sets.

`abletoolz setname.als` Process single set.

`abletoolz setname.als folder/with/sets` Process single set and all sets within a directory(recursive).

`"abletoolz D:\somefolder"` Finds all sets in directory and all subdirectories. If "backup", "Backup" or "abletoolz_backup" are in any
of the path hierarchy, those sets is skipped.

`--jobs N` Number of threads for processing sets concurrently (defaults to auto).

NOTE: On Windows, do NOT include the ending backslash when you have quotes! There is a bug with powershell
in how it handles backslashes and how python interprets backslashes as escape characters:

`abletoolz "D:\somefolder\"` # BAD

`abletoolz "D:\somefolder"` # GOOD

without quotes, backslashes are fine (but you'll need to use quotes if you have spaces in the directory path)
`abletoolz D:\somefolder\` # GOOD

### Analysis - checking samples/tracks/plugins

`--check-samples` Checks relative and absolute sample paths and verifies if the file exists. Ableton will load the
sample as long as one of the two are valid. If relative path doesn't exist(Not collected and saved) only absolute path
is checked. By default only missing samples are displayed to reduce clutter, use `-v` to show all found samples as well.

`--check-plugins` Checks plugin VST paths and verifies they exist. **Note**: When loading a set, if Ableton finds the
same plugin name in a different path it will automatically fix any broken paths the next time you save your project. This
command attempts to find missing VSTs and show an updated path if it finds one that Ableton will most likely load.
VST3s stored by display name alone resolve against the installed plugin files — on macOS through each bundle's
`Info.plist`, since bundle names often differ from display names — and, failing that, against Live's own plugin
database, the only place shell plugins like Waves can be found. Mac Audio Units/AU are not stored with paths,
just plugin names, and cannot be verified yet.
```
[MidiTrack: 1-Serum] Plugin: Serum_x64.dll, Path: C:\Program Files\VstPlugins\Xfer\Serum_x64.dll, Exists: True
[AudioTrack: 2-Audio] Plugin: Effectrix.dll, Path: C:\Program Files\VstPlugins\Effectrix.dll, Exists: True
[AudioTrack: 3-Audio] Plugin: DrumLeveler.dll, Path: None, Exists: False
Mac OS Audio Units are not saved with paths. Plugin FabFilter: FF Pro-Q 2 cannot be verified.
```

`--list-tracks` List track information.
```
Tracks:
Track type    MidiTrack, Name      1-LOW, Id   13, Group id   -1, Color  28, Width 120, Height  68, Unfolded: false
Track type   AudioTrack, Name    2-Drums, Id    8, Group id   -1, Color  28, Width 120, Height  68, Unfolded: false
Track type   AudioTrack, Name     3-Bass, Id   15, Group id   -1, Color  43, Width 120, Height  68, Unfolded: false
Track type  ReturnTrack, Name   A-Reverb, Id    2, Group id   -1, Color  60, Width 120, Height  68, Unfolded: true
```

### Plugin parsers (experimental)

Plugin state inside a set is an opaque buffer per plugin; parsers teach abletoolz specific formats.

`--list-parsers` List registered plugin parsers and their buffer formats.

`--analyze-plugins` Deep analysis using the registered parsers — reports issues like missing samples inside
Serato Sample instances.

`--fix-plugins` Fix what a parser knows how to fix (e.g. broken sample paths inside Serato Sample), using the
sample database. Use with `-s` to write changes.

`--upgrade-plugins` Upgrade plugin paths when a rule and an installed target exist. Rules live in a config file
so nothing is guessed.

`--dump-plugins` Dump plugin buffer hex + decoded preview — the starting point for writing a new parser.
Contributions welcome.

### Create sample database(used for automatic sample fixing)
`--db folder/with/samples` Build up a database of all samples that is used when
you run `--fix-samples-collect` or `--fix-samples-absolute`. This file gets stored in your user config directory. For best
results, run this on all folders that could have samples in them, including your set directories.
```
abletoolz --db "D:\samples" "D:\sets"
Creating database from scratch can take a while, please be patient. Updating an existing one is much faster!
Validating current db...: 100%|████████████████████| 152412/152412 [00:13<00:00, 11238.17it/s]
Progress: 100%|████████████████████████████████████| 7432/7432 [00:01<00:00, 3916.58it/s]
Updated database at C:\Users\you\AppData\Roaming\abletoolz\sample_db.json
```

### Edit
These will only edit sets in memory unless you use `-s/--save` explicitly to commit changes.

`--fix-samples-collect` Go through each sample reference in the ableton set, and if any are missing try to match them based on last modification date, file size and name from the database created with `--db`. Sample is copied into the set's
project folder, the same action as collect and save in ableton.

 `--fix-samples-absolute` The same thing as `--fix-samples-collect`, just doesn't
 copy the sample and instead puts the full path. Note: on MacOS 10/9 sets,
 this sometimes acts strange, so use `--fix-samples-collect` for those.
```
Set version: Ableton Live 10.0.1
Set name: shuffler, BPM: 172.0
Original missing sample count: 7, Samples fixed: 7, Couldn't fix: 0
Moving original file to backup directory:
D:\sets\shuffler.als --> D:\sets\abletoolz_backup\shuffler__1.als
Saved set to D:\sets\shuffler.als
```

`--gradient-tracks` Generate random gradients for tracks and clips. The results from this are limited, since
there are only 70 available colors in ableton, but sometimes you get some pretty good results!
![Abletoolz](https://github.com/elixirbeats/abletoolz/raw/master/doc/gradient_2.png)

`--unfold` or `--fold` unfolds/folds all tracks in set. Group tracks collapse/expand along with them.

`--set-track-heights`  Set arrangement track heights for all tracks, including groups and automation lanes. The values
will be different on different computers/OSes because it's based on your screen resolution, so first experiment
with this command and `--set-track-widths` on a set with different values and open it after to see how it looks. On my
setup the Min is 17, Default 68, Max 425 for track height.

`--set-track-widths` Set clip view track widths for all tracks. On my setup, Min 17, Default 24, Max 264.

`--master-out` number to set Master audio output channels to. 1 correlates to stereo out 1/2, 2 to stereo out 3/4 etc.

`--cue-out` set Cue audio output channels. Same numbering for stereo outputs as master out.

### Output - saving edited sets to disk
`-s`, `--save`
Saves modified set in the same location as the original file. This only applies if you use options that actually alter
the set, not just analyze plugins/samples/etc. When you use this option, as a safety precaution the original file is stored under the same
directory as the original set under `${CURRENT_SET_FOLDER}/abletoolz_backup/set_name__1.als`. If that file exists, it will automatically
create a new one `${CURRENT_SET_FOLDER}/abletoolz_backup/set_name__2.als` and keep increasing the number as files get created. That
way your previous versions are always still intact (be sure to clean this folder up if you run this a bunch of times).

***Disclaimer*** Before using `Edit` options with save, experiment on a set you don't care about first and then open them in ableton to be sure the changes are what you expect. Because I understand how many hours of hard work go into set files,
I've put in multiple safeguards to prevent you losing anything:
- The edited set is fully serialized in memory before anything on disk is touched — if something goes wrong,
your original file is exactly where it was.
- Original file is ALWAYS moved to the backup directory `${CURRENT_SET_FOLDER}/abletoolz_backup/` as described above,
so you can always re-open that file.
- The actual file write runs on a non daemon thread, which will not be forcibly killed if you Cntrl + C the script
during some long operation. Rather than rely on this, please just allow the script to finish processing to avoid any
issues, and make sure the options you're using do what you expect before executing a long running operation(hundreds
of sets can take a while).

All other arguments only modify the set in memory and will only write those changes to a new set when you include `-s`

`-x`, `--xml`  Dumps the uncompressed set XML in same directory as set_name.xml Useful to understand set structure for
development. You can edit this xml file, rename it from `.xml` to  `.als` and Ableton will load it! If you run with this
option multiple times, the previous xml file will be moved into the `abletoolz_backup`
folder with the same renaming behavior as `-s/--save`.

`--append-bars-bpm` Used with `-s/--save`, appends the longest clip or furthest arrangement bar length and bpm to the
set name. For example,
`myset.als` --> `myset_32bars_90bpm.als`. Running this multiple times overwrites this section only (so your filename
wont keep growing).

`--prepend-version` Puts the ableton version used to create set at beginning of file name.

## Examples
Check all samples in sets
```
abletoolz "D:\all_sets" --check-samples
```
```
Parsing: D:\all_sets\Drum N Bass\nyphty.als
Set version: Ableton Live 11.0.10
Set name: nyphty, BPM: 87.0
Longest clip or furthest arrangement position: 16 bars. Estimated length(Only valid for 4/4): 0:44
Sample BGE_170_Fake_Eyes_Drums.wav missing:
        Absolute[D:\Loopcloud\BGE_170_Fake_Eyes_Drums.wav], Relative [..\..\Loopcloud\BGE_170_Fake_Eyes_Drums.wav]
Missing sample references: 1
```

Set all master outs to stereo 1/2 and cue outs to 3/4
```
abletoolz "D:\all_sets" -s --master-out 1 --cue-out 2
```

Or a bunch of options
```
abletoolz "D:\all_sets\myset.als" -s -x --master-out 1 --cue-out 1  --unfold \
--set-track-heights 68 --set-track-widths 24
```
```
Parsing: D:\all_sets\myset.als
Set version: Ableton Live 12.2.6
Set name: myset, BPM: 174.0
Set MasterTrack to 1/2
Set PreHearTrack to 1/2
Unfolded all tracks.
Set track heights to 68.
Set track widths to 24.
Saved xml to D:\all_sets\myset.xml
Moving original file to backup directory:
D:\all_sets\myset.als --> D:\all_sets\abletoolz_backup\myset__1.als
Saved set to D:\all_sets\myset.als
Took 0:00:00.371096 to process 1 set(s): 1 ok, 0 failed
```

```
abletoolz "D:\all_sets\myset.als" -s --append-bars-bpm
```
```
Appending bars and bpm, new set name: myset_400bars_124.00bpm.als
Saved set to D:\all_sets\myset_400bars_124.00bpm.als
Restored creation and modification times: 05/17/2020 05:13:08, 05/17/2020 05:13:12
```

## Library use

Everything the CLI does is available programmatically:

```python
from abletoolz.live_set import AbletonSet

live_set = AbletonSet(pathlib.Path("myset.als"))
live_set.parse()
print(live_set.version_tuple, live_set.transport.bpm())
for track in live_set.tracks.load():
    print(track.type, track.name, track.color)
missing = live_set.samples.check()
```

## Future plans:
- Export sets as an older Live version (Live's compatibility is one-directional; most of the schema differences
are mechanical).
- More plugin parsers — the upgrade framework is in place, formats need decoding one plugin at a time.
- Figure out way to verify AU plugins on MacOs.
- Analyze audio clips and color them based on a Serato like gradient(red for bass, turqoise for hi end etc...)
- Figure out how ableton calculates CRC's for samples and use it to make perfect sample fixing. The current algorithm has a very low probability of being wrong, but this would guarantee each result is correct.
- Attempt to detect key based on non drum track midi notes.
