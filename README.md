![Abletoolz](https://github.com/elixirbeats/abletoolz/raw/master/doc/gradient.png)
# Abletoolz

Abletoolz is a Python command line tool to edit, fix and analyze Ableton sets. Primarily the purpose is to automate
things that aren't available and make your life easier.
It can:
- Run on one set, or an entire directory of sets. So you can fix/analyze etc everything with one command.
- Color all your tracks/clips with random color gradients.
- Create a sample database of all your sample folders, which can then be used to automatically fix any broken samples in your ableton sets.
- Set all your Master/Cue outputs to a specific output, so if you buy a new audio interface you can fix all your master outs to point to 7/8 in one go.
- Validate all plugins in a set are installed. MacOS VST3s currently do not work for this.
- Fold/Unfold all tracks, and/or set track height and widths.
- Prepend the set version name to the beginning of the file.
- Append the number of bars of the track, and the bpm to the end of the file.
- Dump the XML of the set, in case you want to disect how they are structured or contribute to this project : )


It also:
- Moves your original set files to a backup folder before writing any changes, so you are never at risk of losing anything.
- Supports both Windows and MacOS created sets.
- Works on Ableton 8.2+ sets(not every command works with older versions though).
- Preserves the original set modification time.

Future plans:
- Figure out way to verify AU plugins on MacOs.
- Analyze audio clips and color them based on a Serato like gradient(red for bass, turqoise for hi end etc...)
- Build plugin parsers, that can read in the plugin saved buffer and attempt fixes or other things. For instance, a sampler that has a broken filepath could automatically be fixed.
- Figure out how ableton calculates CRC's for samples and use it to make perfect sample fixing. The current algorithm has a very low probability of being wrong, but this would guarantee each result is correct.
- Attempt to detect key based on non drum track midi notes.


## Installation:
Minimum python required 3.10

(https://www.python.org/downloads/)

Open a command line shell and make sure you installed Python 3.10+ correctly:
```
python -V  # Should give you a version
```
Once you verify you have python 3.10+, install with pip:
```
pip install abletoolz
```
This will install abletoolz as a command in your command line, you can now call `abletoolz` from anywhere if the
installation completed successfully. (Create an issue if you run into any errors please!)


## Usage:
`-h` Print argument usage.
`-v` Verbosity. For some commands, displays more information.

### Input - Parsing single or multiple sets.
Only one input option can be used at a time.

`abletoolz setname.als` Process single set.

`abletoolz setname.als folder/with/sets` Process single set and all sets within a directory(recursive).

`"abletoolz D:\somefolder"` Finds all sets in directory and all subdirectories. If "backup", "Backup" or "abletoolz_backup" are in any
of the path hierarchy, those sets is skipped.

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
Mac Audio Units/AU are not stored with paths, just plugin names. Mac OS is not supported for this yet.
![Analyze plugins](https://github.com/elixirbeats/abletoolz/raw/master/doc/plugins_check.png)

`--list-tracks` List track information.
![List tracks](https://github.com/elixirbeats/abletoolz/raw/master/doc/list_tracks.png)

### Create sample database(used for automatic sample fixing)
`--db folder/with/samples` Build up a database of all samples that is used when
you run `--fix-samples-collect` or `--fix-samples-absolute`. This file gets stored in your home directory. For best 
results, run this on all folders that could have samples in them, including your set directories.
![Database](https://github.com/elixirbeats/abletoolz/raw/master/doc/db_example.png)    

### Edit
These will only edit sets in memory unless you use `-s/--save` explicitly to commit changes.

`--fix-samples-collect` Go through each sample reference in the ableton set, and if any are missing try to match them based on last modification date, file size and name from the database created with `--db`. Sample is copied into the set's
project folder, the same action as collect and save in ableton.

 `--fix-samples-absolute` The same thing as `--fix-samples-collect`, just doesn't
 copy the sample and instead puts the full path. Note: on MacOS 10/9 sets,
 this sometimes acts strange, so use `--fix-samples-collect` for those.
![Fixing sample references](https://github.com/elixirbeats/abletoolz/raw/master/doc/sample_fix.png)

`--gradient-tracks` Generate random gradients for tracks and clips. The results from this are limited, since
there are only 70 available colors in ableton, but sometimes you get some pretty good results!
![Abletoolz](https://github.com/elixirbeats/abletoolz/raw/master/doc/gradient_2.png)

`--unfold` or `--fold` unfolds/folds all tracks in set.

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
- Original file is ALWAYS moved to the backup directory `${CURRENT_SET_FOLDER}/abletoolz_backup/` as described above,
so you can always re-open that
file if for some reason the newly created set breaks(so far I have not been able to break one, on Windows and MacOs).
- I use a non daemon thread to do the actual file write, which will not be forcibly killed if you Cntrl + C the script
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
![Check samples](https://github.com/elixirbeats/abletoolz/raw/master/doc/check_samples.png)


```
abletoolz "D:\all_sets" --check-plugins
```
![Check plugins](https://github.com/elixirbeats/abletoolz/raw/master/doc/plugins_check.png)

```
abletoolz "D:\all_sets\some_set.als" --list-tracks
```
![List tracks](https://github.com/elixirbeats/abletoolz/raw/master/doc/list_tracks.png)

Set all master outs to stereo 1/2 and cue outs to 3/4
```
abletoolz "D:\all_sets" -s --master-out 1 --cue-out 2
```

Or a bunch of options
```
abletoolz "D:\all_sets\myset.als" -s -x --master-out 1 --cue-out 1  --unfold \
--set-track-heights 68 --set-track-widths 24
```
![Check plugins](https://github.com/elixirbeats/abletoolz/raw/master/doc/everything.png)

```
abletoolz "D:\all_sets\myset.als" -s --append-bars-bpm
```
![Append bars bpm](https://github.com/elixirbeats/abletoolz/raw/master/doc/append_bars_bpm.png)
