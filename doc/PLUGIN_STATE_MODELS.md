# Plugin maps and state models

Saved plugin settings belong to the state axis under `abletoolz.plugins.state`.
They can be inspected and edited without a plugin installation or an Ableton
document. The [glossary](../GLOSSARY.md#plugin-maps-and-saved-state) defines plugin
maps, saved state and the vocabulary used here.

`plugin_map.PluginMap` collects a vendor's codecs, maps and measurement scope
for one plugin/version. Each `StateFormat` supplies a decoder and the byte maps,
sampled parameter maps and executable curves associated with that format.
`ByteMap` is an open storage-description interface; `FixedByteMap` implements
fixed offsets, and `ParameterMap` holds sampled parameter-to-field relationships.
Container translation, plugin identity and source-to-target conversion remain
separate operations.

## Using a plugin map

Choose the state format explicitly. The map retains typed vendor models and
does not identify a plugin or infer a format from buffer size or a display name.
A format can describe known storage with `decoder=None`; decoding that format
then raises before any state can be written. Product releases that have not
been established use `version=None`, with binary fingerprints and measured
format coverage recorded separately.

```python
from abletoolz.plugins.state.izotope_trash import STATE_FORMAT, plugin_map

knowledge = plugin_map()
format = knowledge.format(STATE_FORMAT)
bank = device.decode_state(format.decode)
selected = device.saved_state.program_number
if selected is None:
    raise ValueError("Choose the saved program explicitly")
edited = bank.with_values(selected, {"PreFilter: Node 1 Freq": 823.25})
device.set_state_model(edited)
```

`PluginMap[Bank]` returns `Bank`; another vendor can return its own encodable
model. Its `formats` tuple can contain any number of format descriptors and
decoder implementations. `StateFormat.byte_maps` may describe nested records:
the Trash bank codec handles sixteen program frames, while its byte map addresses
revision-21 program contents. Revision-22 records remain opaque and reject field
edits. The codec model enforces that distinction before a byte-map edit.

Parameter knowledge must reference the actual byte-map object owned by its state
format. Sharing an ID with a different layout is insufficient. Sampled
`ParameterMap` observations and executable `ParameterCurves` stay separate;
`ParameterCurves.values` refuses conflicting writes to a shared saved field.
Custom map providers must keep IDs, fields and addresses stable while a format
references them. Frozen descriptors protect their own metadata; they do not
freeze arbitrary providers or callback internals. Binary hashes and scope record
the evidence. Callers must check binary compatibility before native validation;
the map does not inspect an installed plugin or enforce scope text at runtime.
Maps carry no converter or registration side effects. Vendor factories expose
their maps explicitly, without adding entries to the state-converter registry.

## State formats and layers

A saved buffer is bytes at the host boundary. Its contents may be text, binary,
compressed data, or a combination. Keep three questions separate:

1. How does Live carry the state? The container adapter extracts the buffer or
   processor/controller pair and retains host metadata such as program selection.
2. How does the plugin serialize it? The state codec handles headers, record
   boundaries, lengths, names, compression and state-format revisions.
3. What do its values mean? The parameter map relates exposed parameters to saved
   fields, with units, curves, coupling and measured context. Other saved fields
   can describe routing or settings with no exposed parameter.

The byte map describes storage within the state format. An address can be a
fixed offset, a document path, a field in an identified record, or another
format-specific location. The `ByteMap` interface exposes format identity and
saved-field lookup. Concrete maps describe their own locations and encodings;
codecs interpret them. `FixedByteMap` provides the current offset-based editor.

`SavedField` exposes a field's ID and name. IDs are local to one state format;
matching numbers across formats imply no correspondence. Concrete maps validate
their field identities and locations, while codecs enforce supported revisions,
framing and editing context. Addresses and encodings remain properties of each
concrete format, available to its codec without widening the shared interface.

The following are examples, rather than an exhaustive set of format types.

| State representation | Storage knowledge the plugin map needs |
| --- | --- |
| JSON or XML | Keys/paths, types, nesting, revision rules and any surrounding framing. A readable name still needs measured units and behavior. |
| Fixed binary | Offsets, widths, endianness, encodings and the exact layout/revision those offsets describe. |
| Structured binary | Record IDs, lengths, counts, nesting and field encodings. Variable-sized records require parsing before locating a field. |
| Compressed or archived state | The outer compression/archive and the inner state format, including metadata needed to rebuild it. |
| Mixed or partly understood state | The known regions and boundaries, preserved opaque data and explicit limits on permitted edits. |

Formats can combine these mechanisms or use others. A bank can contain several
framed programs,
and an archive can contain both text and binary records. The family detector in
`state.families` recognizes observed outer signatures; detection alone establishes
neither a complete parse nor editable semantics. Existing examples include JSON
in `parsers.serato`, fixed fields in `state.izotope_trash`, framed parameter arrays
in `state.fabfilter`, and length-prefixed archive metadata in `state.arturia`.

The detector's categories are reporting labels, not an admission list for plugin
maps or codecs. A new format supplies a byte map with stable saved-field identities
and its own decoder/encodable state model. Parameter maps look up those identities
without requiring offsets or a central format enum. A converter is supplied
separately for each supported source-to-target relationship.

A plugin release, a plugin format and a state-format revision are separate
identities. One plugin release can accept several revisions, and VST2 and VST3
builds can carry different envelopes around related settings. Maps must state
which revisions, binaries and contexts their evidence covers. Serialized fields
also need their own addresses: an in-memory object layout or pointer is evidence
for investigation, rather than a saved-buffer offset.

Exact byte preservation, native save/reload acceptance and sound equivalence are
separate checks. Fixed-field edits can retain untouched bytes. A text or compressed
codec may regenerate whitespace, ordering or compression bytes; each codec must
state its preservation contract and retain unknown content or refuse the edit.
Record lengths and any integrity fields must be rebuilt according to the measured
format. Readable JSON alone supplies none of those behavioral guarantees.

## Byte maps, parameter maps and converters

`model.FixedByteMap` names the saved coordinates of one fixed serialized layout. Each
`StateField` has a stable field ID, name, storage type and byte offset. Vendor
element/parameter IDs are optional metadata. A field ID is independent of a
public VST parameter index. These maps describe serialized storage, not a live
C++ object or an inferred physical control range.
`FieldEncoding` lists the primitive types this fixed-layout editor implements;
other codecs can define their own scalar or composite encodings.

`MappedState` retains the complete bytes and provides typed reads and atomic
`with_values` edits: all requested values must validate before any edits are
applied to a new model. The original stays immutable. Reads and edits accept
either an integer field ID or an exact, unique field name. Inspect
`settings.map.fields` to discover them.

Edits copy the original and patch only the requested fields. Parsing and encoding
without edits preserves every byte, including unknown regions and floating-point
bit patterns. Explicit float writes round ordinary numbers to float32 and refuse
nonfinite or overflowing inputs; they do not promise preservation of the old bit
pattern. Other writes require unsigned 32-bit words or explicit Booleans. A codec
must add any known physical-range or enum validation.

Vendor models own record boundaries, names, program banks and revision checks.
They expose `parse(bytes)` and `encode()` as the other existing state codecs do.
An unsupported revision may remain an opaque record when its framing is known;
it cannot borrow the field coordinates of an older revision. For example, a Trash
bank program exposes its `revision`, raw state and name even when opaque. Its
`settings` property raises for an unmapped revision; the complete bank can still
be re-encoded losslessly. Editable programs compose a `MappedState` with the
vendor's record and name framing.

`controls.ParameterMap` records sampled public-parameter bindings, including coupled
fields, source/evidence hashes, measurement scope and unresolved status. Sample
lookup refuses interpolation. `ParameterBinding` holds an executable curve only
where that curve is implemented. The sampled map and executable bindings are
different types so an observation cannot silently become an arithmetic rule.
`ParameterCurve` is an open callable interface; a newly measured curve supplies
its own implementation without extending a central list of curve types.
Native-check status is bounded to the inputs and context in its evidence.
Sample lookup returns field IDs and observed stored values. Executable bindings
calculate stored values; the owning vendor model applies them to `MappedState`
after checking its format and context.

A partial fixed-layout codec can supply an `opaque_sha256` guard. The digest
covers the full state with mapped bytes masked out. It rejects other templates,
including same-sized states whose unmodelled settings differ. This deliberately
limits coverage until those settings and layouts have been mapped.
The guard is checked when constructing or decoding a `MappedState`, and again
when constructing an edited copy. A mismatch raises `StateTransformError` before
a writable model is returned.

A state converter consumes a source model and produces a target model. It owns
correspondence, defaults, unsupported features and intentional losses. Only a
completed converter belongs in the existing `register_custom_state` registry.
The source and target are decoded codec models; the converter defines their
correspondence explicitly. Registry callbacks accept and return bytes, using the
appropriate source decoder and target encoder around that model conversion.
A field map or successful byte round trip does not establish sound equivalence.
Registration needs an implemented, guarded source-to-target conversion with its
supported scope, validation and losses stated explicitly. A storage codec alone
does not meet that contract.

## The Ableton boundary

`PluginDevice.saved_state` describes the complete host-carried state and its
metadata through the existing format adapters. `PluginDevice.decode_state`
takes an explicit decoder; `set_state_model` finishes encoding
before changing the XML. Neither method guesses the plugin format, changes its
identity or rewrites automation. VST3 controller bytes are kept alongside the
processor state and are preserved by a processor-only model edit. The older
`PluginData` API delegates model reads and writes to this same device boundary.
A plugin's VST2 and VST3 payloads can share a codec where that equivalence has
been measured; container adapters handle their different host records.

For example, with `device` selected from `PluginDevices(document_root)`:

```python
from abletoolz.plugins.state.izotope_trash import Bank

bank = device.decode_state(Bank.parse)
program = device.saved_state.program_number
if program is None:
    raise ValueError("Select the saved bank program explicitly")

settings = bank.program(program).settings
frequency_hz = settings.value("PreFilter: Node 1 Freq")
edited = bank.with_values(program, {"PreFilter: Node 1 Freq": 823.25})
device.set_state_model(edited)
```

The caller retains the ALS `ProgramNumber` explicitly. It is zero-based, as is
`bank.program(index)`, which validates the bank's bounds. If absent, the caller
must supply a chosen index instead of the example's error; the codec never
defaults to program 0. A complete bank does not identify its active program.
Field edits change only the saved state. Reconciling
configured host parameters, automation and program selection is a separate
caller responsibility.

## Device conversions

`container.format_translation.ConversionRule` describes the destination identity
and the state/controller policies for a device conversion. Resolving a named rule
supplies the destination identity; applying it performs the supported container
rewrite. Rules consume codecs and converters through explicit state policies.
The existing repair/recovery planners guard controls in use; a rule does not
establish an automation correspondence by itself.

A conversion plan concerns specific device instances, preconditions and expected
changes. Applying the plan to a set is a set migration. Recovery is the narrower
purpose of restoring an unavailable or broken device. Inspection, state editing,
same-plugin upgrades and cross-plugin conversion can all use plugin maps.

The former `StateMap`, `ControlMap`, `ControlBinding` and `Recipe` names remain
compatibility aliases. Fixed layouts use `FixedByteMap`; parameter relationships
use `ParameterMap` and `ParameterBinding`, and device rules use `ConversionRule`.
Vendor accessors use `byte_map()` and `parameter_map()`.
`PluginDevice.preset` and `set_preset_record` remain aliases for
`saved_state` and `set_saved_state_record`. Native XML tag names and existing config
keys retain their host/schema spelling.

## Promoting research

Keep native captures, reverse-engineering output and rejected predictions as evidence. Promote
stable field definitions and measurement scope into package data, then build the
vendor codec on the shared models. Retain unknown regions and refuse ambiguous
versions. Add regression fixtures from independent native saves and real saved
buffers where available; a parser encoding its own synthetic output is not
native acceptance evidence. Package data and tests must run without the research
sandbox, a plugin DLL, a DAW or a remote machine.

Keep unfinished codecs, converters and research inventories on development
branches. The release package contains completed operations within their stated
scope. Unsupported settings must be refused or reported as an explicit accepted
approximation, rather than silently treated as mapped. A completed map can cover
part of a format without claiming that all controls or conversions are available.
Previously configured names for withdrawn transforms remain readable, but
resolving those transforms raises before a device can be rewritten.

The Trash 1 bank model packages typed fields, version-local algorithm domains and
sampled controls. Trash 2 packages a native default template, bounded saved-field
edits, linear curve polarity edits and sixteen executable core parameter curves.

`plugins.converters.izotope_trash.convert_single_band_distortion(bank, program=index)`
consumes these models and returns a `TrashConversion` with a target state and
notices. It supports an explicitly selected single-band distortion program,
refuses active unmapped modules and reports known tonal differences. The
converter's correspondence data is owned separately from both plugin maps.
Callers retain its scope and tonal-difference notices when presenting the result.
No byte-only conversion is registered, because a bank alone does not identify
the selected program. Set identity, configured controls and automation require
separate handling.
