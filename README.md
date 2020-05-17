![Check plugins](/doc/top.png)
# Abletoolz

Python command line tool for batch editing and analyzing Ableton Live Sets. 

Supports both Windows and MacOS created sets.
Currently only Ableton 10 sets are supported. Some versions of Ableton 8 and 9 will be supported in the future.

## Installation:
I developed this using Python 3.8, but most likely it will work with other Python 3.x versions. I recommend 3.6+

(https://www.python.org/downloads/)

Use git clone or download the zip file and extract to a directory.
```
python -m pip install -r requirements.txt
```

## Usage:
`-h` Print argument usage.

### Input
Only one input option can be used at a time.

`-f` Process single set.

`-d` Finds all sets in directory and all subdirectories. If "backup", "Backup" or "abletoolz_backup" are in any 
of the path hierarchy, the set is skipped.

### Output
`-s`, `--save` 
Save changes to set. When you use this option, as a safety precaution the original file is stored under the same 
directory as the original set under `set_dir/abletoolz_backup/project_name.als`. If that file exists, it will automatically 
create a new one `set_dir/abletoolz_backup/project_name_1.als` and keep increasing the number as files get created. That 
way your previous version are always still intact.

***Disclaimer*** Use the save argument your own risk! Because this is the initial version of the project, it is possible 
there are unknown bugs that could potentially break your sets! Use a copy of your sets in another folder, commit changes
 to them, then open them in ableton to be sure they load correctly. All other arguments modify the set in memory and 
 only if this argument is specified does it save the changes to a file, so they are safe to run.

`-x`, `--xml`  Dumps the uncompressed set XML in same directory as set_name.xml Useful to understand set structure for 
development. If you run with this option multiple times, the previous xml file will be moved into the abletoolz_backup 
folder with the same renaming behavior as --save.

### Analysis
`--list-tracks` List track information.

`--check-samples` Checks relative and absolute sample paths and verifies if the file exists. Ableton will load the 
sample as long as one of the two are valid. If relative path doesn't exist(Not collected and saved) only absolute path 
is checked.

`--check-plugins` Checks plugin VST paths and verifies they exist. **Note**: When loading a set, if Ableton finds the 
same plugin name in a different path it will automatically fix any broken paths the next time you save your project. 
AU are not stored as paths but component names. Ableton stores the AU name differently than what is stored under
`/Library/Audio/Plug-Ins/Components`.

### Edit
These will only edit sets in memory unless you use --save explicitly to commit changes.

`--unfold` or `--fold` unfolds/folds all tracks in set.

`--set-track-heights`  Set arrangement track heights for all tracks, including groups and automation lanes. Min 17, 
Default 68, Max 425

`--set-track-widths` Set clip view track widths for all tracks, including groups. Min 17, Default 24, Max 264. 

`--master-out` number to set Master audio output channels to. 1 correlates to stereo out 1/2, 2 to stereo out 3/4 etc.

`--cue-out` set Cue audio output channels. Same numbering for stereo outputs as master out.

## Examples
Check all samples in sets
```
python main.py -d "D:\all_sets" --check-samples
```
![Check samples](/doc/check_samples.png)


```
python main.py -d "D:\all_sets" --check-plugins
```
![Check plugins](/doc/check_plugins.png)

```
python main.py -f "D:\all_sets\some_set.als" --list-tracks
```
![List tracks](/doc/track_list.png)

Set all master outs to stereo 1/2 and cue outs to 3/4
```
python main.py -d "D:\all_sets" -s --master-out 1 --cue-out 2
```

Or a bunch of options
```
python main.py -f "D:\all_sets\myset.als" -s -x --master-out 1 --cue-out 1  --unfold \
--set-track-heights 68 --set-track-widths 24 
```
![Check plugins](/doc/everything.png)

## Future plans
- Continue to add support for pre Ableton 10 versions. Some of this is done already.
- Add color functions to color tracks/clips with gradients or other fun stuff.
- Collect samplepath errors when both absolute and relative paths are broken into a report file.
- Add crc verify for samples.
- Fix broken sample paths. For windows this should be easy, for Mac ... a challenge. Will need to figure out how to 
correctly create byte data that ableton loads happily.
- Add parsing for older non gzipped ableton sets.
- Figure out way to verify AU plugins.
- Attempt to detect key based on non drum track midi notes.
- Add support for different time signatures besides 4/4.
- Add more track and clip specific analying/editing functions.
- Create a GUI.
- Figure out how to create package with setup tools
