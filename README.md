![Check plugins](/doc/new.png)
# Abletoolz

So what is Abletoolz? It's a Python command line tool to do operations & analysis on single files and directories of Ableton
Live sets. Currently you can change all your Master/Cue out channels (for example make all your sets use
stereo out 7/8 for master),
find out all your missing
samples and plugins and change your set filenames to append the length of the longest clip/furthest bar and bpm. The
analysis options do not write anything to files, but if you use the save set options,
it moves the original file
under the directory `abletoolz_backup` before creating the edited set file as a precaution(more info below).

Supports both Windows and MacOS created sets.

## Updates
- Ableton 8, 9, 10 and 11/11.1b sets are now supported.
- Abletoolz now preserves your set file creation/modification times by storing them before writing changes and then
modifying the file after.
- Added `--append-bars-bpm` argument and used non daemon thread for set write to help safeguard against process being
killed.

## Installation:
Minimum python required 3.8

(https://www.python.org/downloads/)

Open a command line shell and make sure you installed Python 3.8+ correctly:
```
python -V  # Should give you a version
```
Once you verify you have python 3.8+, clone this repository using git clone, or download this repo's zip file and
extract it to a folder. Navigate to that folder on the command line, then run:
```
python -m pip install .
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

NOTE: On windows, do NOT include the ending backslash! There is a bug with powershell
in how it handles backslashes and how python interprets backslashes as escape characters:
`abletoolz "D:\somefolder\"` # BAD

`abletoolz "D:\somefolder"` # GOOD

without quotes, backslashes are fine (but you'll need to use quotes if you have spaces in the directory path)
`abletoolz D:\somefolder\` # GOOD


### Analysis - checking samples/tracks/plugins
![Analyze set](/doc/new.png)

`--check-samples` Checks relative and absolute sample paths and verifies if the file exists. Ableton will load the
sample as long as one of the two are valid. If relative path doesn't exist(Not collected and saved) only absolute path
is checked. By default only missing samples are displayed to reduce clutter, use `-v` to show all found samples as well.

`--check-plugins` Checks plugin VST paths and verifies they exist. **Note**: When loading a set, if Ableton finds the
same plugin name in a different path it will automatically fix any broken paths the next time you save your project. This
command attempts to find missing VSTs and show an updated path if it finds one that Ableton will most likely load.
Mac Audio Units/AU are not stored with paths, just plugin names. Mac OS is not supported for this yet.
`/Library/Audio/Plug-Ins/Components`.

`--list-tracks` List track information.

### Edit
These will only edit sets in memory unless you use `-s/--save` explicitly to commit changes.

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
directory as the original set under `set_dir/abletoolz_backup/set_name__1.als`. If that file exists, it will automatically
create a new one `set_dir/abletoolz_backup/set_name__2.als` and keep increasing the number as files get created. That
way your previous versions are always still intact (be sure to clean this folder up if you run this a bunch of times).

***Disclaimer*** Before using `Edit` options with save, experiment on simple sets you don't care about,
then open them in ableton to make sure they load correctly and the changes are what you expect. Once you are confident
of the options you use then edit . Because I understand how many hours of hard work go into set files,
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


## Examples
Check all samples in sets
```
abletoolz "D:\all_sets" --check-samples
```
![Check samples](/doc/check_samples.png)


```
abletoolz "D:\all_sets" --check-plugins
```
![Check plugins](/doc/check_plugins.png)

```
abletoolz "D:\all_sets\some_set.als" --list-tracks
```
![List tracks](/doc/track_list.png)

Set all master outs to stereo 1/2 and cue outs to 3/4
```
abletoolz "D:\all_sets" -s --master-out 1 --cue-out 2
```

Or a bunch of options
```
abletoolz "D:\all_sets\myset.als" -s -x --master-out 1 --cue-out 1  --unfold \
--set-track-heights 68 --set-track-widths 24
```
![Check plugins](/doc/everything.png)

```
abletoolz "D:\all_sets\myset.als" -s --append-bars-bpm
```
![Append bars bpm](/doc/append_bars_bpm.png)

## Future plans
- Add color functions to color tracks/clips with gradients or other fun stuff.
- Collect sample path errors when both absolute and relative paths are broken into a report file.
- Missing sample fix feature:
    - Add crc verify for samples, since ableton does store a crc number in the set.
    - Currently I've added a database feature that will build up a json file to track all samples. Implementing
    the missing sample fix is next, but getting it to work across older ableton versions will take some work since
    mac/windows use different types of binary encoded hex.
- Figure out way to verify AU plugins on MacOs.
- Attempt to detect key based on non drum track midi notes.
- Add support for different time signatures besides 4/4.
- Add more track and clip specific analysing/editing functions.
- Figure out how to create package with setup tools and put this on PyPy.
