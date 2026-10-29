# Converting plugins

A set made on another machine often holds plugins this one can't load: an Audio Unit from a Mac, an old VST2 you replaced with its VST3, a version you upgraded since. abletoolz moves those devices to what you have here, keeping their patches.

It works in three steps. abletoolz writes a plan, a short file listing every plugin in your sets with what each one could become. You write a name next to the plugins you want converted, then apply it:

```powershell
abletoolz --plugin-db                                     # once, and again after installing plugins
abletoolz plugins plan "D:/sets/Impact.als" -o plan.yaml  # reads the set, changes nothing
                                                          # write convert_to in plan.yaml
abletoolz plugins apply plan.yaml                         # rewrites the set, keeps a backup
```

The steps below go through it in order. Steps 7 to 9 do the same from Python, for when you want more control. [What converting costs](#what-converting-costs) says what a converted device can lose, and why. [What abletoolz can convert today](#what-abletoolz-can-convert-today) lists the plugins it knows.

## 1. Find the plugins on this computer

```powershell
abletoolz --plugin-db
```

This reads every plugin installed here. Run it again after installing or removing plugins. Extra plugin folders go in your `config.yaml`:

```yaml
plugin_database:
  paths:
    - "D:/VstPlugins"
```

## 2. Make a plan

```powershell
abletoolz plugins plan "D:/sets/Impact.als" -o plan.yaml
```

This reads the set and writes `plan.yaml`; the set itself isn't touched. `plugins plan` also takes a folder, and finds sets in it recursively, skipping backup folders.

The plan lists every plugin in the set, working or not, with three fields each. Above each one, a comment lists the names it can become. This is part of a real plan:

```yaml
# abletoolz plugin plan. Set convert_to to one of the names listed above a plugin,
# then run: abletoolz plugins apply <this file>. null leaves a plugin as it is.
abletoolz_plan: 2
sets:
- D:\sets\Impact.als
plugins:

# Auto-Tune Pro AU: 1 device(s), doesn't load here
#   nothing it can become here
- plugin: Auto-Tune Pro
  format: AU
  convert_to: null

# Serum VST2: 14 device(s)
#   serum_vst3
#       8 of 14 device(s): its automation and clip envelopes removed: the converted plugin numbers its controls differently
#       8 of 14 device(s): the list of its controls arranged in Live was replaced
- plugin: Serum
  format: VST2
  convert_to: null

# ValhallaRoom VST2: 3 device(s)
#   valhalla_room_vst3
#   valhalla_room_au
#       not installed here
#       3 of 3 device(s): its list of controls in Live was left empty: it can only be built for a VST3
- plugin: ValhallaRoom
  format: VST2
  convert_to: null
```

Under each name is what taking it costs, and how many of that plugin's devices it costs: see [what converting costs](#what-converting-costs). "Not installed here" is only information. You can convert to a plugin this computer doesn't have, for example an Audio Unit for a set going to a Mac.

## 3. Choose what each plugin becomes

Copy one of the names above a plugin into its `convert_to`:

```yaml
- plugin: Serum
  format: VST2
  convert_to: serum_vst3
```

The name has to be one of those listed, written exactly; anything else is refused when you apply, with the names it could have been. `null` leaves the plugin as it is, and every plugin starts that way, so nothing converts unless you wrote it.

`convert_to` covers every device of that plugin in the sets. To treat one device differently, see the [Python steps](#7-the-same-steps-in-python).

## 4. Apply it

```powershell
abletoolz plugins apply plan.yaml
```

```text
D:\sets\Impact.als: 17 device(s) rewritten, 0 left as they were
  [2] ValhallaRoom on TRACK -> valhalla_room_vst3
  [3] ValhallaRoom on TRACK -> valhalla_room_vst3
  [7] Serum on 30-Serum -> serum_vst3
  [8] Serum on Main Bass -> serum_vst3
      warning: its automation and clip envelopes removed: the converted plugin numbers its controls differently
      warning: the list of its controls arranged in Live was replaced
  [11] Serum on 34-Serum -> serum_vst3
  ...
```

Apply reads every set again, so it works on the sets as they are now, not as they were when you made the plan. Each set is saved in place, with the original copied to `abletoolz_backup\Impact__1.als` (then `__2` and so on). A device that can't be converted at all stays as it was, with the reason, while the rest of the set converts. Add `--json` for structured output.

Open the set in Live and listen before you delete the backup.

## 5. Do it for a whole library

Plan a folder instead of one set:

```powershell
abletoolz plugins plan "D:/sets" -o library-plan.yaml
```

Each plugin appears once, with the number of devices it has across every set, and one `convert_to` covers them all. A plan file that already exists isn't overwritten unless you pass `--force`.

## 6. Common cases

**Upgrade to a newer plugin.** Where abletoolz can convert one plugin into another, its name is listed like any format: `pro_q_3_vst3` under a Pro-Q 2, for example.

**Move a set between Mac and Windows.** Write the Audio Unit or VST3 name the other machine needs. The plugin doesn't need to be installed here. abletoolz writes VST3 and AU devices but not VST2 ones.

**A set too old for the target.** A set saved before Live supported VST3 can't hold a VST3 device, and Live would refuse the whole set, so those devices stay as they are and say so. Open and save the set in a newer Live first; converting a plugin doesn't upgrade the set.

**Recover an old wrapped plugin.** `abletoolz plugins routes` lists the measured recovery routes for old wrapped VST2 plugins and the binaries found for them, and a plan lists a route's name under the plugin it recovers. `--plugin-dir "D:/VstPlugins"` searches only that folder. abletoolz checks each binary's hash but never runs the plugin.

**Fix samples and convert.** Relink and save first, then plan. Converting carries the patch across as it is and doesn't relink anything.

## 7. The same steps in Python

Each command has a function. In Python a plan holds more than the file: every device, and every target with its evidence and what it costs.

```python
from pathlib import Path

from abletoolz.plugins.plan import apply_in_place, plan_sets, read_plan, write_plan

write_plan(plan_sets([Path("D:/sets/Impact.als")]), "plan.yaml")  # abletoolz plugins plan
# set convert_to in plan.yaml, then:
plan = read_plan("plan.yaml")  # reads the sets again, with your convert_to as each plugin's pick
for outcome in apply_in_place(plan):  # abletoolz plugins apply plan.yaml
    for device in outcome.devices:
        print(outcome.path.name, device.plugin, "->", device.pick if device.done else device.reason, device.warnings)
```

`entry.pick` is the Python side of `convert_to`, with `"keep"` for `null`. To treat one device differently, add an override with its set and device number from `entry.devices`:

```python
from abletoolz.plugins.plan import Override

serum = next(entry for entry in plan.plugins if entry.plugin == "Serum")
serum.pick = "serum_vst3"
serum.overrides.append(Override(set=0, device=8, pick="keep"))  # every Serum but device 8
```

## 8. Pick by your own rule

With code you can pick from what each target says about itself. This moves every plugin to its own VST3, where that costs nothing:

```python
from abletoolz.plugins.identity.ids import PluginKind
from abletoolz.plugins.plan import KEEP, Route

plan = plan_sets(["D:/sets"])
for entry in plan.plugins:
    free = [
        target.id
        for target in entry.targets
        if target.route is Route.FORMAT  # the same plugin in another format, not a different plugin
        and target.format is PluginKind.VST3
        and not target.refused
        and not target.warnings  # no device loses anything
    ]
    entry.pick = free[0] if free else KEEP
apply_in_place(plan)
```

Add `and target.proved` to take only conversions someone has listened to. `entry.broken` tells you whether this machine can load the plugin as it is. `file_targets` and `experiments` in `abletoolz.plugins.plan` add targets a plan file never offers: another installed file of the same VST2, and an installed plugin named like one abletoolz hasn't mapped.

## 9. Write converted copies somewhere else

`apply_in_place` saves over your sets, keeping backups. To leave them alone, `apply_plan` hands you each converted set instead, and you decide where it goes. This writes the copies into another folder:

```python
from abletoolz.formats import live_document
from abletoolz.plugins.plan import apply_plan, this_machine

out = Path("D:/converted")
out.mkdir(exist_ok=True)


def save_copy(path, root):  # called once for each set that changed, with the converted set
    live_document.write_new(out / path.name, live_document.encode(root))  # never replaces a file


outcomes = apply_plan(plan, save=save_copy, machine=this_machine())
```

`this_machine()` is this computer as the plan sees it: its plugins and its recovery files. Pass the same one to `plan_sets(..., machine=)` and `apply_plan` when you make and apply in one go.

A plan only offers what abletoolz already knows how to do: another format of a plugin, a recovery route, or a plugin it has a converter into. Anything else, like changing a plugin's settings, is code you write for that plugin: [Change a plugin's own settings](LIBRARY.md#change-a-plugins-own-settings) and [Rebuild a plugin as Live's own devices](LIBRARY.md#rebuild-a-plugin-as-lives-own-devices) show how.

## What abletoolz can convert today

This grows as plugins get mapped, so take it as a rough guide. `abletoolz --plugin-maps` prints the current list, with how each one was measured.

**Between VST2, VST3 and AU, keeping the patch:**

- FabFilter Pro-C 2, Pro-G, Pro-L 2, Pro-MB, Pro-Q 3, Saturn 2 and Timeless 3
- Valhalla Delay, FreqEcho, Plate, Room, Shimmer and Supermassive, Audio Units included
- 36 Kilohearts kHs effects
- Arturia Prophet V3, Devious Machines Texture, Goodhertz Tupe 3, Serato Sample, Xfer Serum, and Surge XT (VST3 and AU)

Massive, Battery 4, Trash 2, Ozone 9 Exciter and Volcano 3 can't be converted to VST3 yet, so their VST2 devices stay VST2.

**Into a newer plugin:**

- FabFilter Pro-Q and Pro-Q 2 to Pro-Q 3
- Surge to Surge XT
- Native Instruments Pro-53 to Arturia Prophet-5 V. A converted device lists what Prophet-5 V has no match for, like the high-pass filter, the delay and analog drift, and how each setting was matched.

iZotope Trash to Trash 2 is worked out too, but a plan can't offer it until abletoolz can write Trash 2's VST3.

**Files inside a plugin's patch**, relinked by the sample flags: Serato Sample, xfadelooper, Battery 4, Kontakt 5, Maschine 2, Texture, CMI V and Reaktor 6. iZotope Iris and Akai MPC Essentials files are listed so you can fix them by hand.

## What converting costs

A converted device keeps its patch, the settings that shape its sound. What it can lose is what sits around the patch. The plan shows each cost under the name you'd pick, and apply repeats it under every device it hit, so nothing goes missing without a word. These are limits of what abletoolz knows today, and each one is something we hope to carry over in time.

**Automation and clip envelopes are removed.** Live points automation at a plugin's controls by number, and a VST2, its VST3, its AU and another plugin all number their controls differently. A lane left in place would drive whichever control now has that number, so it is removed. Moving a lane needs a map from one plugin's control numbers to the other's, and a conversion of every point on it, and nobody has written those maps yet.

**MIDI and macro mappings are removed**, for the same reason: they point at control numbers too.

**A list of controls you arranged in Live is replaced.** For a plugin with few controls, Live lists them under the device, and Configure lets you choose and reorder them. A converted device gets the list Live itself would make for the new plugin. abletoolz reads that list from the installed VST3. For an AU or VST2 target, or a VST3 that isn't installed here, the list is left empty.

**Settings the target has no place for are left out.** A converter carries every setting it has a measured match for, and names each one it couldn't, with what it did instead: Pro-Q 2's Auto Gain is turned off, a Trash 1 module Trash 2 can't play is left out, and so on.

**Some devices can't be converted at all**, and stay as they were with the reason: a VST3 into a set saved before Live supported VST3, which would make Live refuse the whole set; a device whose set never says which plugin it is; and a patch abletoolz can't read, which leaves nothing to carry.

**Some formats are read but not written yet.** `--plugin-maps` marks them `(read)`, and a plan offers no target in them.

## Why some plugins convert and others don't

Live stores a plugin's settings as bytes, and VST2, VST3 and AU only say how the plugin is hosted, not what those bytes mean. Each operation needs to know a different amount:

| Operation | What abletoolz has to know |
| --- | --- |
| Point a device at another installed file of the same plugin | The plugin's identity and file; the patch stays untouched |
| Move the same plugin between formats | Its identity in both formats, how each wraps the patch, and that the patch inside is the same |
| Relink a sample inside a plugin | Where the path lives in the patch and how to rewrite it |
| Change a sound setting | Which saved fields it changes, their encoding, units and curves |
| Turn one plugin into another | Both patch formats and a worked-out match between their controls |

Plugins from the same maker often wrap their patches the same way, so one discovery can cover a family. Turning one plugin into another is a sound translation every time and needs its own converter. That is why support is per plugin, per format and per operation. `abletoolz --plugin-maps` shows what abletoolz knows, and [Mapping a new plugin](DEVELOPMENT.md#mapping-a-new-plugin) covers adding one.
