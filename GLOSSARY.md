# Abletoolz

Abletoolz reads, repairs and writes Ableton Live sets: the set itself (tracks, clips, devices, samples) and the plugin devices inside it. This glossary names the concepts the code and its documents share; where two words exist for one thing, it picks one.

## Plugin devices

**Plugin device**:
One instance of a third-party plugin in a Live set, with its identity, reference, saved state and controls in use. Several plugin devices can use the same plugin with different settings.
_Avoid_: plugin (the software on disk), device (a native Live device), VST element, info

**Info element**:
The part of a plugin device's saved description that carries its identity, reference and saved state for a particular plugin format.
_Avoid_: plugin info, VstPluginInfo (when any format is meant)

**Container adapter**:
The bridge between Live's description of a plugin device and its identity, reference and saved state for a particular plugin format.
_Avoid_: reader, writer, translator, format handler

**Plugin format**:
The hosting interface through which a DAW uses a plugin, such as VST2, VST3 or Audio Unit.
_Avoid_: state format, binary format

## Plugin maps and saved state

**Plugin map**:
The collected knowledge about one plugin and version, including its byte maps, parameter maps, behavior, evidence and limits of coverage. A plugin map may cover several state formats and can be incomplete.
_Avoid_: converter, recovery route, binary layout (each means something narrower or different)

**Saved state**:
The information a plugin device saves to restore its configuration, which may include several programs and separate processor and controller state.
_Avoid_: preset, patch, buffer (when the complete saved configuration is meant)

**Preset**:
A selectable sound or configuration for a plugin.
_Avoid_: saved state (when all saved information is meant)

**State format**:
The rules a plugin uses to serialize its saved state, including any revision-specific structure and encodings. A plugin version may accept or produce more than one state format.
_Avoid_: plugin format, plugin version

**Byte map**:
The description of how saved fields are represented and located within a state format, including known framing and unknown regions. Locations may be fixed offsets, structural addresses within records or documents, or other format-specific addresses.
_Avoid_: plugin map, parameter map, state map

**Saved field**:
An identified value in a plugin's saved state, whether or not it has an exposed control.
_Avoid_: parameter (when only a stored value is meant)

**Parameter map**:
The relationships between a plugin's exposed parameters and its saved fields, including identifiers, units, value curves, coupling and the scope of measured behavior.
_Avoid_: control map, byte map, converter

**State codec**:
The reader and writer for a supported plugin state format, with explicit limits on the information it can interpret and edit.
_Avoid_: converter, parameter map

## Conversions and migrations

**State converter**:
A directional translation of settings from a supported source state model into a target state model, with defined defaults, approximations and unsupported features.
_Avoid_: plugin map, codec, repair

**Conversion rule**:
A description of an allowed plugin-device replacement, identifying the source, destination and how saved state and parameter relationships are handled.
_Avoid_: recipe, mapping

**Conversion plan**:
The proposed changes to specific plugin devices in a set, with their applicability checks, unresolved requirements and expected losses.
_Avoid_: conversion rule, plugin map

**Set migration**:
The application of planned plugin-device conversions to a Live set, including the surrounding references, program selection and automation they affect.
_Avoid_: mapping, state conversion (when the whole set operation is meant)

**Recovery route**:
A measured path from an unavailable or broken plugin reference to a replacement verified to accept its saved state, bounded by the evidence for that path.
_Avoid_: migration, repair rule

**Controls in use**:
The facts about a plugin device's parameters that a format change could orphan: configured parameter ids, MIDI mappings, and automation or modulation targets referenced elsewhere in the set.
_Avoid_: parameters, bindings, automation (each is only one of them)

**State policy**:
The choice of how a device's saved state reaches its destination: retained, reframed, converted or replaced with prepared state.
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
