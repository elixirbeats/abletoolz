# Abletoolz

Abletoolz reads, repairs and writes Ableton Live sets: the set itself (tracks, clips, devices, samples) and the plugin devices inside it. This glossary names the concepts the code and its documents share; where two words exist for one thing, it picks one.

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
