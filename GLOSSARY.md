# Abletoolz

Abletoolz reads, repairs and writes Ableton Live sets: the set itself (tracks, clips, devices, samples) and the plugin devices inside it. 

## Plugin devices

**Plugin device**:
One third-party plugin as a Live set stores it: its identity, the file it points at, its saved preset, the controls in use, and the device and track that own it. The same plugin device can be stored in any plugin format.
_Avoid_: plugin (the software on disk), device (a native Live device), VST element, info

**Info element**:
The one format-specific element inside a plugin device that carries its identity, reference and preset for a given format: VST2, VST3 or Audio Unit.
_Avoid_: plugin info, VstPluginInfo (when any format is meant)

**Container adapter**:
What reads one format's info element into a plugin device and writes a plugin device back out as that format's info element, in the shape the set's Live version writes.
_Avoid_: reader, writer, translator, format handler

**Recipe**:
What one plugin device becomes: the source format and name it applies to, the target format and identity, the state policy for its preset bytes, and the controller policy for a VST3 target. A recipe is data; the seed table, a config entry and a recovery route each produce one.
_Avoid_: mapping, target, translation, upgrade rule

**Recovery route**:
A measured path from one broken plugin reference to a native plugin that has been verified to load its saved banks, with the evidence behind it. A recovery route yields a recipe.
_Avoid_: migration, repair rule

**Controls in use**:
The facts about a plugin device's parameters that a format change could orphan: configured parameter ids, MIDI mappings, and automation or modulation targets referenced elsewhere in the set.
_Avoid_: parameters, bindings, automation (each is only one of them)

**State policy**:
What happens to a preset's bytes on the way to the target: carried verbatim, reframed, re-encoded by a registered transform, or replaced by a prepared state.
_Avoid_: transform (one kind of policy), conversion

## Live versions

**Schema**:
The document format a Live release writes, named by the two version numbers at the top of every set: `MinorVersion` and `SchemaChangeCount`. Several releases can share one schema, and one Live line holds many. Live opens a set whose schema is its own or older.
_Avoid_: version (a release), format (a plugin format)

**Save target**:
The schema a set is written in. Named by a Live version; it resolves to the newest schema seen at or below that version, and to nothing if the corpus holds no set from it.
_Avoid_: downgrade target, export version

**Grammar**:
For each element of a set, the child elements and attributes Live writes under it, with the first and last schema each was seen in.
_Avoid_: census (the measuring that produces it), allow-list

**Stand-in rack**:
An empty rack of a device's kind (audio effect, instrument or MIDI effect), named after a device the save target does not have and put where that device was, its info text saying what was there.
_Avoid_: placeholder, downgrade, replacement device
