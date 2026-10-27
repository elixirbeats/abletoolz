![Abletoolz](https://github.com/elixirbeats/abletoolz/raw/master/doc/gradient.png)
# Abletoolz

Abletoolz is a Python command line tool to edit, fix and analyze Ableton Live sets. Primarily the purpose is to automate things that aren't available in Live and make your life easier.

It can:

- Run on one set, or an entire directory of sets. So you can fix/analyze everything with one command.
- Color all your tracks/clips with random color gradients.
- Create a sample database of all your sample folders, which can then be used to automatically fix any broken samples in your Ableton sets, including samples that were renamed.
- Set all your Master/Cue outputs to a specific output, so if you buy a new audio interface you can fix all your master outs to point to 7/8 in one go.
- Validate all plugins in a set are installed.
- Convert plugins to the ones you have, for example a Mac set's Audio Units to the same plugins' VST3s on Windows, keeping their sounds.
- Fix the samples plugins keep inside their own settings (a sampler's sample, a drum kit, a Kontakt instrument), which Live never reports.
- Fold/Unfold all tracks, and/or set track height and widths.
- Dump the XML of the set, in case you want to dissect how they are structured or contribute to this project : )

It also:

- Copies your original set files to a backup folder before writing any changes, so you are never at risk of losing anything.
- Supports both Windows and macOS created sets.
- Works on sets from Ableton 8.2 through Live 12 (not every command works with older versions though).
- Preserves the original set modification time.
- Works as a [Python library](doc/LIBRARY.md), if you want to script your own fixes.

## Installation

Minimum Python required is 3.12 (https://www.python.org/downloads/).

Open a command line shell and make sure you installed Python 3.12+ correctly:

```
python -V  # Should give you a version
```

Once you verify you have Python 3.12+, install with pip:

```
pip install abletoolz
```

This will install abletoolz as a command in your command line, you can now call `abletoolz` from anywhere if the installation completed successfully. (Create an issue if you run into any errors please!)

## Usage

`-h` Print argument usage.

`-v` Verbosity. For some commands, displays more information.

### Input - Parsing single or multiple sets

`abletoolz setname.als` Process single set.

`abletoolz setname.als folder/with/sets` Process single set and all sets within a directory (recursive).

`abletoolz "D:\somefolder"` Finds all sets in directory and all subdirectories. Sets inside a "Backup" or "abletoolz_backup" folder are skipped.

NOTE: On Windows, do NOT include the ending backslash when you have quotes! Python reads the backslash as an escape for the closing quote:

`abletoolz "D:\somefolder\"` # BAD

`abletoolz "D:\somefolder"` # GOOD

Without quotes, backslashes are fine (but you'll need quotes if you have spaces in the directory path):

`abletoolz D:\somefolder\` # GOOD

`-j N` Number of sets to work on at the same time. By default abletoolz uses all your CPU cores; `-j 1` does one set at a time.

### Analysis - checking samples/tracks/plugins

`--check-samples` Checks relative and absolute sample paths and verifies if the file exists. Ableton will load the sample as long as one of the two are valid. If the relative path doesn't exist (not collected and saved) only the absolute path is checked. By default only missing samples are displayed to reduce clutter, use `-v` to show all found samples as well. It also checks the samples and kits plugins keep inside their own settings, which Live never warns you about: the plugin loads and simply plays nothing.

`--check-plugins` Checks whether each plugin in the set is installed, the way Live would find it on this computer. **Note**: When loading a set, if Ableton finds the same plugin name in a different path it will automatically fix any broken paths the next time you save your project. When a plugin isn't found, this shows the closest installed one.

```
[1-Serum] Plugin: Serum_x64.dll, Path: C:\Program Files\VstPlugins\Xfer\Serum_x64.dll, Exists: True
[2-Audio] Plugin: Effectrix.dll, Path: C:\Program Files\VstPlugins\Effectrix.dll, Exists: True
[3-Audio] Plugin: DrumLeveler.dll, Path: None, Exists: False
[4-Audio] Plugin: Apple: AUBandpass, Path: None, Exists: True
[5-MIDI] Plugin: Arturia: Analog Lab V, Path: /Library/Audio/Plug-Ins/Components/Analog Lab V.component, Exists: True
```

`--list-tracks` List track information.

```
Tracks:
Track type    MidiTrack, Name      1-LOW, Id   13, Group id   -1, Color  28, Width 120, Height  68, Unfolded: false
Track type   AudioTrack, Name    2-Drums, Id    8, Group id   -1, Color  28, Width 120, Height  68, Unfolded: false
Track type   AudioTrack, Name     3-Bass, Id   15, Group id   -1, Color  43, Width 120, Height  68, Unfolded: false
Track type  ReturnTrack, Name   A-Reverb, Id    2, Group id   -1, Color  60, Width 120, Height  68, Unfolded: true
```

### Create sample database (used for automatic sample fixing)

`--db folder/with/samples` Build up a database of all samples that is used when you run `--fix-samples-collect` or `--fix-samples-absolute`. For best results, run this on all folders that could have samples in them, including your set directories and your plugin libraries.

```
abletoolz --db "D:/samples" "D:/sets" "C:/Users/Public/Documents"
```

Running it again only re-reads what changed, so keep it up to date whenever you add samples. Folders you don't pass stay in the database. If you always scan the same folders, list them in your `config.yaml` (`%APPDATA%\abletoolz` on Windows, `~/Library/Application Support/abletoolz` on macOS, `~/.config/abletoolz` on Linux) and run `--db` with no folders:

```yaml
sample_database:
  paths:
    - "D:/samples"
    - "D:/sets"
```

`--search-samples "kick 174"` Search the database by file and folder names.

`--db-prune` Remove every sample from the database that no longer exists. A sample that's gone is never used to fix a set, but when fixing runs into some it tells you to run this. Plug in your sample drives first, since files on a drive that isn't connected count as gone too.

### Edit

These will only edit sets in memory unless you use `-s/--save` explicitly to commit changes.

`--fix-samples-collect` Go through each sample reference in the set, and if any are missing find them in the database created with `--db`. The sample is copied into the set's project folder, the same action as collect and save in Ableton. A sample is matched by its name, size, date and the checksum Live stores, so a renamed sample can be found too. Files only a plugin loads, like a Kontakt instrument, are pointed at where they are in your library and never copied.

`--fix-samples-absolute` The same thing as `--fix-samples-collect`, just doesn't copy the sample and instead puts the full path.

`--gradient-tracks` Generate random gradients for tracks and clips. The results from this are limited, since there are only 70 available colors in Ableton, but sometimes you get some pretty good results!
![Abletoolz](https://github.com/elixirbeats/abletoolz/raw/master/doc/gradient_2.png)

`--unfold` or `--fold` unfolds/folds all tracks in set.

`--set-track-heights` Set arrangement track heights for all tracks, including groups and automation lanes. The values will be different on different computers/OSes because it's based on your screen resolution, so first experiment with this command and `--set-track-widths` on a set with different values and open it after to see how it looks. On my setup the Min is 17, Default 68, Max 425 for track height.

`--set-track-widths` Set clip view track widths for all tracks. On my setup, Min 17, Default 24, Max 264.

`--master-out` Number to set Master audio output channels to. 1 correlates to stereo out 1/2, 2 to stereo out 3/4 etc.

`--cue-out` Set Cue audio output channels. Same numbering for stereo outputs as master out.

### Output - saving edited sets to disk

`-s`, `--save` Saves the modified set in the same location as the original file. This only applies if you use options that actually alter the set, not just analyze plugins/samples/etc. When you use this option, as a safety precaution the original file is copied to `abletoolz_backup/set_name__1.als` beside the set before the new one replaces it. If that file exists, it will create `abletoolz_backup/set_name__2.als` and keep increasing the number, so your previous versions are always still intact (be sure to clean this folder up if you run this a bunch of times).

`--output DIRECTORY` Used with `-s`, writes the changed sets into another folder instead, and leaves the originals alone:

```
abletoolz "D:/sets" --fix-samples-absolute -s --output "D:/repaired_sets"
```

***Disclaimer*** Before using `Edit` options with save, experiment on a set you don't care about first and then open it in Ableton to be sure the changes are what you expect. Because I understand how many hours of hard work go into set files, I've put in multiple safeguards to prevent you losing anything:

- The original file is ALWAYS copied to the backup folder as described above, so you can always re-open that file if for some reason the newly created set breaks.
- The new set is written to a temporary file and swapped in at the very end in one step, so if you Ctrl + C the script or your computer loses power mid-save, the original set is still where it was. Rather than rely on this, please just allow the script to finish processing, and make sure the options you're using do what you expect before running on hundreds of sets.

`-x`, `--xml` Dumps the uncompressed set XML in the same directory as set_name.xml. Useful to understand set structure for development. You can edit this xml file, rename it from `.xml` to `.als` and Ableton will load it! If you run with this option multiple times, the previous xml file is copied into the `abletoolz_backup` folder with the same renaming behavior as `-s/--save`. With `--output`, the xml goes into the output folder beside the copy instead.

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

Fix all broken samples, copying them into each project

```
abletoolz --db "D:\samples" "D:\all_sets"
abletoolz "D:\all_sets" --fix-samples-collect -s
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

## Converting plugins

A set made on another machine often has plugins this one can't load: an Audio Unit from a Mac, an old VST2 you replaced with its VST3. abletoolz can switch those devices to what you have, keeping their sounds. It works in two steps with a file in between, so nothing changes until you've looked at it:

```
abletoolz --plugin-db                                   # once: lists the plugins installed here
abletoolz plugins plan "D:/sets" -o plan.yaml           # writes what each plugin can become
abletoolz plugins apply plan.yaml                       # after you've read and edited the plan
```

[Converting plugins](doc/PLUGIN_CONVERSION.md) walks through a real plan.

`--list-plugins` Lists every plugin abletoolz knows how to convert or fix, and what it can do with each.

`--plugin-db` Records the plugins installed on this computer. Run it again after installing plugins. Extra plugin folders go in your `config.yaml`:

```yaml
plugin_database:
  paths:
    - "D:/VstPlugins"
```

## What a run leaves behind

A run that checks or fixes samples or plugins leaves a small `<set name>.meta.yaml` beside each set (`<clip name>.alc.meta.yaml` for a clip file), with what it found. The `status` and `notes` fields are yours to fill in, abletoolz never touches them:

```yaml
status: needs work
notes: reverb tail runs long in the breakdown
scan:
  live_version: Ableton Live 11.3.42
  bars: 128
  bpm: 174.0
  plugins_missing:
    Serum_x64: 2
  samples_missing: 3
```

(The real file has a few more fields abletoolz uses to tell whether the set changed.) Running `--check-plugins` again on an unchanged set reads this file instead of opening the set, so a second pass over a big library is much faster. Sample checks always look at your drives. `--no-meta` skips these files and checks everything fresh.

Each run also writes `abletoolz_report_<timestamp>.json` into the folder it processed, with everything it found, changed and couldn't do, in case you want to process the results with your own scripts.

## Using it from Python

Everything the command line does, you can call from Python:

```python
from pathlib import Path

from abletoolz.live_set import AbletonSet

song = AbletonSet(Path("D:/sets/my song.als"))
song.parse()
print(song.version, song.transport.bpm(), "bpm")
for track in song.tracks.load():
    print(track.name)
```

[Using abletoolz as a library](doc/LIBRARY.md) starts there and works up to fixing samples and converting plugins.

## Experimental: reading and editing a set as JSON

Two commands let a program, such as a script or a language model, read a set and write changes back. Both speak JSON and may still change.

`--describe structure|patterns|full` prints the set as JSON: its tempo and version, every track with its devices, and with `patterns` or `full`, the clips and their notes.

```
abletoolz "tune.als" --describe patterns
```

`--apply-ops` takes a JSON file of changes and writes the result to a new set given with `--output`. The input set is never touched.

```
abletoolz "tune.als" --apply-ops edits.json --output "tune variation.als"
```

```json
{"ops": [
  {"op": "copy_section", "src_start": 128.0, "src_end": 192.0, "dest_start": 384.0, "tracks": ["Drums"]},
  {"op": "set_notes", "track": {"name": "Bass"}, "clip": {"start": 384.0}, "notes": [[36, 0.0, 1.0]]}
]}
```

If any change in the file is invalid, nothing is changed. Every kind of change is listed in [the library guide](doc/LIBRARY.md#experimental-json-for-programs).

## Future plans

- Make sending a set to an older Live reliable. `save_as` does it already, but it's experimental and some copies still crash Live.
- More plugin maps. The framework is in place; each plugin's format is decoded one at a time.
- Support time signatures other than 4/4, and maybe signature changes within a set.
- Figure out why Live rejects `.asd` files created by hand, and fix it.
- More AI agentic tool interaction interfacing.

## Development

The plugin data and test fixtures are stored with Git LFS, so install `git lfs` before cloning. [Developing abletoolz](doc/DEVELOPMENT.md) explains how the code is laid out and how to add support for a new plugin.

`-x` and `--dump-plugins` are the tools for looking inside sets and plugin settings. `abletoolz-dj-crates` builds pre-warped `.alc` clips from a TSV of track analysis; run `abletoolz-dj-crates --help` for its options.
