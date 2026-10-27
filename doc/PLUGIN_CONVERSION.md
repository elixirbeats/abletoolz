# Converting plugins

A set made on another machine often holds plugins this one can't load: an Audio Unit from a Mac, an old VST2 you replaced with its VST3, a version you upgraded since. abletoolz moves those devices to what you have here, keeping their patches.

It works in three steps. abletoolz writes a plan, a YAML file listing every plugin in your sets and what each one could become. You edit the plan, then apply it:

```powershell
abletoolz --plugin-db                                     # once, and again after installing plugins
abletoolz plugins plan "D:/sets/Impact.als" -o plan.yaml  # reads the set, changes nothing
                                                          # edit plan.yaml
abletoolz plugins apply plan.yaml                         # rewrites the set, keeps a backup
```

The steps below go through it in order. Steps 7 to 9 do the same from Python, for when you want to pick by your own rules.

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

The plan lists the sets it read, then one entry per plugin. This is a real entry, for a VST2 ValhallaRoom used three times in one set:

```yaml
sets:
- path: D:\sets\Impact.als
  sha256: 12faaf688a164a516527eef339ae615f89af9c822e8ab272457d576dcb823cb8
plugins:
- plugin: ValhallaRoom
  format: vst
  name: ValhallaRoom
  stored: vst2 1383429485
  map_id: valhalla.room
  broken: false
  devices:
  - set: 0
    device: 2
    track: TRACK
  - set: 0
    device: 3
    track: TRACK
  - set: 0
    device: 16
    track: 37-Group
  targets:
  - id: ValhallaRoom (VST3)
    route: format
    plugin: ValhallaRoom
    format: vst3
    evidence: 'state: reframe (hosted 2026-10-22)'
    proved: false
    installed: true
    map_id: valhalla.room
    route_id: null
    path: null
    unique_id: null
    uid_fields: null
    refused: {}
  - id: ValhallaRoom (AU)
    route: format
    plugin: ValhallaRoom
    format: au
    evidence: 'state: reframe (hosted 2026-10-22)'
    proved: false
    installed: false
    map_id: valhalla.room
    route_id: null
    path: null
    unique_id: null
    uid_fields: null
    refused:
      the set is too old to hold a AU device; open and save it in Live first: 3
  - id: file C:\Program Files\VstPlugins\Valhalla\ValhallaRoom_x64.dll
    route: file
    plugin: ValhallaRoom_x64
    format: vst
    evidence: same plugin id in another file; patch kept as it is -- audition before trusting
    proved: false
    installed: true
    map_id: null
    route_id: null
    path: C:\Program Files\VstPlugins\Valhalla\ValhallaRoom_x64.dll
    unique_id: 1383429485
    uid_fields: null
    refused: {}
  pick: keep
  overrides: []
```

`broken: false` says this machine can load the plugin as it is. Each target is something its devices can become: here the plugin's VST3, its AU, or another installed file of the same VST2. Four fields decide whether you want one:

- `evidence`: what is known about moving the patch. `verbatim` and `vendor-compat` copy the patch unchanged, `reframe` rewraps it for the new format, and `re-encode` rewrites it. In brackets is how that was learned: `ear` is someone listening in Live, `declared` the plugin's maker saying so, `hosted` the target plugin loading the patch outside Live, `structural` the bytes looking right. `try` in front of an id marks an experiment: a plugin abletoolz hasn't mapped, matched by name.
- `proved`: true only for `ear` or `declared`. Audition anything else.
- `installed`: whether the target is on this machine.
- `refused`: how many of the devices can't take the target, and why.

The other fields identify the plugin for abletoolz; leave them as they are.

## 3. Pick what each plugin becomes

`pick` is what `apply` does with every device in the entry. To convert, copy a target's `id` into it:

```yaml
  pick: ValhallaRoom (VST3)
```

Only ids the plan offers work; a plugin name it doesn't offer doesn't create a conversion. `keep` leaves the devices alone.

To treat one device differently, add an override with its `set` and `device` from the entry's `devices`. This converts every Serum in the set except device 8:

```yaml
  pick: Serum (VST3)
  overrides:
  - set: 0
    device: 8
    pick: keep
```

`set: 0` is the first path under `sets`, and `device` is the plan's device number, not a track number.

## 4. Apply it

```powershell
abletoolz plugins apply plan.yaml
```

```text
D:\sets\Impact.als: 9 device(s) rewritten, 7 left as they were
  [2] ValhallaRoom on TRACK -> ValhallaRoom (VST3)
  [3] ValhallaRoom on TRACK -> ValhallaRoom (VST3)
  [7] Serum on 30-Serum -> Serum (VST3)
  [9] Serum on Main Bass kept, because it has automation or mapped controls, which need a parameter map
  [10] Serum on 33-Serum screamer kept, because it has automation or mapped controls, which need a parameter map
  [11] Serum on 34-Serum -> Serum (VST3)
  [12] Serum on 35-Serum -> Serum (VST3)
  [13] Serum on Main Bass kept, because it has automation or mapped controls, which need a parameter map
  [16] ValhallaRoom on 37-Group -> ValhallaRoom (VST3)
  [17] Serum on 38-Serum screamer kept, because it has automation or mapped controls, which need a parameter map
  [18] Serum on 38-Serum screamer kept, because it has automation or mapped controls, which need a parameter map
  [19] Serum on 38-Serum screamer kept, because it has automation or mapped controls, which need a parameter map
  [20] Serum on Basses 3 kept, because it has automation or mapped controls, which need a parameter map
  [21] Serum on 42-Serum -> Serum (VST3)
  [23] Serum on 43-Serum -> Serum (VST3)
  [24] Serum on 44-Serum -> Serum (VST3)
```

Device 8 isn't listed because the override kept it. The set is saved in place, with the original copied to `abletoolz_backup\Impact__1.als` (then `__2` and so on). Apply checks each device again: a device that can't take its target stays as it was while the rest of the set converts, and a set that changed since the plan was made is skipped. A successful exit doesn't mean every device converted, so read the list, or add `--json` for structured output.

Open the set in Live and listen before you delete the backup.

## 5. Do it for a whole library

Plan a folder instead of one set, and `--pick` decides what the plan picks before you edit it:

```powershell
abletoolz plugins plan "D:/sets" --pick best -o library-plan.yaml
```

| Option | Starting picks |
| --- | --- |
| `--pick broken` (default) | A target for every plugin this machine can't load |
| `--pick best` | A target for every plugin, working ones too |
| `--pick keep` | Nothing picked |

An automatic pick prefers the same plugin over a different one, then stronger evidence, then your format order, `--formats` (default `vst3,au,vst`, where `vst` is VST2). An experiment is never picked for you.

Once you've edited one plan, `--from` carries its picks into a new one, so you only decide once per plugin:

```powershell
abletoolz plugins plan "D:/sets" --from plan.yaml -o library-plan.yaml
```

A pick carries over when the new plan still offers its target, and an override only for an unchanged set. Plugins the old plan didn't cover start from `--pick`, so review the new plan before applying it. An existing plan file isn't overwritten unless you pass `--force`.

The `installed` flags describe the machine the plan was made on, so after installing plugins or moving machines, run `--plugin-db` again and make a fresh plan.

## 6. Common cases

**Upgrade to a newer plugin.** Plan with `--pick keep`, set the old plugin's `pick` to the newer one's offered target (for example `Pro-Q 3 (VST3)` for a Pro-Q 2), and apply. `--pick broken` leaves a working plugin alone, so an upgrade is always your choice. Upgrades are offered into VST3 or AU, never VST2.

**Move a set between Mac and Windows.** Plan on the machine the set is going to. An AU set planned on Windows offers each mapped plugin's VST3; the AU doesn't need to run there. abletoolz writes VST3 and AU devices but not VST2 ones.

**A set too old for the target.** A set saved before Live supported VST3 or AU refuses those targets, as the AU target in step 2 does. Open and save it in a newer Live first; converting a plugin doesn't upgrade the set.

**Recover an old wrapped plugin.** `abletoolz plugins routes` lists the measured recovery routes for old wrapped VST2 plugins and the binaries found for them. `--plugin-dir "D:/VstPlugins"` searches only that folder. abletoolz checks each binary's hash but never runs the plugin.

**Fix samples and convert.** Relink and save first, then plan the saved set: a plan made before the save holds the old set's hash and is skipped. Converting carries the patch across as it is and doesn't relink anything.

## 7. The same steps in Python

Each command has a function, and a plan in Python has the same entries and fields as the YAML file:

```python
from pathlib import Path

from abletoolz.plugins.plan import Pick, apply_in_place, plan_sets, read_plan, write_plan

plan = plan_sets([Path("D:/sets/Impact.als")], pick=Pick.KEEP)  # abletoolz plugins plan --pick keep
for entry in plan.plugins:  # one entry per plugin, like the YAML
    if entry.plugin == "Serum" and entry.format == "vst":
        entry.pick = "Serum (VST3)"  # an id from entry.targets
write_plan(plan, "plan.yaml")  # -o plan.yaml, so you can read it first

for outcome in apply_in_place(read_plan("plan.yaml")):  # abletoolz plugins apply plan.yaml
    for device in outcome.devices:
        print(outcome.path.name, device.plugin, "->", device.pick if device.done else device.reason)
```

`plan_sets` takes the command's other options too: `formats=` and `picks_from=` an older edited plan, like `--from`.

## 8. Pick by your own rule

With code you can pick from what each target says about itself instead of by name. This moves every plugin with an installed VST3 of itself that no device refuses, across a whole folder:

```python
from abletoolz.plugins.identity.ids import PluginKind
from abletoolz.plugins.plan import KEEP, Route

plan = plan_sets(["D:/sets"], pick=Pick.KEEP)
for entry in plan.plugins:
    vst3 = [
        target.id
        for target in entry.targets
        if target.route is Route.FORMAT  # the same plugin in another format, not a different plugin
        and target.format is PluginKind.VST3
        and target.installed
        and not target.refused  # every device can take it
    ]
    entry.pick = vst3[0] if vst3 else KEEP
apply_in_place(plan)
```

Add `and target.proved` to take only conversions someone has listened to. `entry.broken` tells you whether this machine can load the plugin as it is.

## 9. Write converted copies somewhere else

`apply_in_place` saves over your sets, keeping backups. To leave them alone, `apply_plan` hands you each converted set instead, and you decide where it goes. This writes the copies into another folder:

```python
from abletoolz.formats import live_document
from abletoolz.plugins.plan import apply_plan, this_machine

plan = plan_sets(["D:/sets"], pick=Pick.BEST)
out = Path("D:/converted")
out.mkdir(exist_ok=True)


def save_copy(path, root):  # called once for each set that changed, with the converted set
    live_document.write_new(out / path.name, live_document.encode(root))  # never replaces a file


outcomes = apply_plan(plan, save=save_copy, machine=this_machine())
```

`this_machine()` is this computer as the plan sees it: its plugins and its recovery files. Pass the same one to `plan_sets(..., machine=)` and `apply_plan` when you make and apply in one go, so a device is rewritten against what was checked.

A plan only offers what abletoolz already knows how to do: another format or file of a plugin, or an upgrade it has a converter for. Anything else, like turning a plugin into a different one or changing its settings, is code you write for that plugin: [Change a plugin's own settings](LIBRARY.md#change-a-plugins-own-settings) and [Rebuild a plugin as Live's own devices](LIBRARY.md#rebuild-a-plugin-as-lives-own-devices) show how.

## What doesn't convert

A converted device keeps its patch, every setting that shapes the sound. These are the things around it.

**Automation, macros and MIDI mappings refuse a device.** Live points them at a plugin's controls by number, and VST2 and VST3 number them differently. Moving a lane would mean renumbering it and converting every point, so the device is left as it is and the plan says why.

**Live's own list of a plugin's controls is rebuilt.** For a plugin with few controls, Live lists them under the device. A converted device gets the list Live would make for the new VST3, read from the installed plugin. A list you arranged yourself with Configure counts as controls in use and refuses the device.

**A patch from an older build can refuse.** When an earlier version saved fewer parameters, its patches are refused rather than shifted onto the wrong controls.

**A setting with no place in the target refuses the patch**, or the converter says what it loses.

**Some formats are read but not written yet.** `--list-plugins` marks them `(read)`, and a plan offers no target in them.

## Why some plugins convert and others don't

Live stores a plugin's settings as bytes, and VST2, VST3 and AU only say how the plugin is hosted, not what those bytes mean. Each operation needs to know a different amount:

| Operation | What abletoolz has to know |
| --- | --- |
| Point a device at another installed file of the same plugin | The plugin's identity and file; the patch stays untouched |
| Move the same plugin between formats | Its identity in both formats, how each wraps the patch, and that the patch inside is the same |
| Relink a sample inside a plugin | Where the path lives in the patch and how to rewrite it |
| Change a sound setting | Which saved fields it changes, their encoding, units and curves |
| Turn one plugin into another | Both patch formats and a worked-out match between their controls |

Plugins from the same maker often wrap their patches the same way, so one discovery can cover a family. Turning one plugin into another is a sound translation every time and needs its own converter. That is why support is per plugin, per format and per operation. `abletoolz --list-plugins` shows what abletoolz knows, and [Mapping a new plugin](DEVELOPMENT.md#mapping-a-new-plugin) covers adding one.
