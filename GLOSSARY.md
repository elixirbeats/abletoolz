# Abletoolz

Abletoolz reads, repairs and writes Ableton Live sets: the set itself (tracks, clips, devices, samples) and the plugin devices inside it.

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
The collected knowledge about one plugin: its identity and saved state in each plugin format it ships in, how its patch crosses between those formats, and its byte maps, parameter maps, evidence and limits of coverage. One plugin is what one set of stored identities names, so a new major version (Pro-Q 3 to Pro-Q 4) is another plugin with its own map. A plugin map can be incomplete.
_Avoid_: converter, recovery route, binary layout (each means something narrower or different)

**Framing**:
How one plugin format wraps a plugin's own saved state: a header, a trailer, a bank envelope or a property-list key. Crossing between two formats of one plugin is lifting out of one framing and lowering into the other.
_Avoid_: reframe policy, envelope (when the whole rule is meant)

**Plugin catalog**:
Every plugin map abletoolz knows, found by the identity a plugin device stores, and the converters between them. Vendor modules register their plugins into it.
_Avoid_: seed table, translation table

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

**Converter**:
A worked-out conversion of one plugin's own patch into another plugin's, keyed by the two plugin maps, with defined defaults, approximations, unsupported features and its own evidence. The two maps handle the formats on either side.
_Avoid_: translation (that is a plugin-format rewrite), custom state, plugin map, codec

**Plan**:
A file listing every plugin in some sets, each with the targets it can become on this machine, the evidence for each and its devices' refusals, and the pick its devices take; `apply` carries it out. Edited by hand between the two commands.
_Avoid_: mapping table, repair

**Target**:
One thing a plugin's devices can become in a plan: the same plugin in another format, another installed file of it, a recovery route, another plugin through a converter, or an experiment for an unmapped plugin: its patch copied to an installed plugin with a similar name, never picked automatically.
_Avoid_: mapping, replacement

**Recovery route**:
A measured path from an unavailable or broken plugin reference to a replacement verified to accept its saved state, bounded by the evidence for that path.
_Avoid_: migration, repair rule

**Controls in use**:
The facts about a plugin device's parameters that a format change could orphan: configured parameter ids, MIDI mappings, and automation or modulation targets referenced elsewhere in the set.
_Avoid_: parameters, bindings, automation (each is only one of them)

**Edit**:
A change to a plugin device that keeps its plugin format: the fields that change are written into its existing info element, and everything else Live wrote stays as it was.
_Avoid_: patch, update

**Rebuild**:
A plugin device written as another plugin format: a new info element built from its identity, saved state and, where it carries one, Live's list of its parameters, put where the old one was.
_Avoid_: translation, reframe

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
