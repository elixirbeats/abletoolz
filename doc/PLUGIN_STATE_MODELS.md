# Plugin state models

Saved plugin settings belong to the state axis under `abletoolz.plugins.state`.
They can be inspected and edited without a plugin installation or an Ableton
document. Container translation, plugin identity and source-to-target conversion
remain separate operations.

## Storage, controls and transforms

`model.StateMap` names the saved coordinates of one serialized layout. Each
`StateField` has a stable field ID, name, storage type and byte offset. Vendor
element/parameter IDs are optional metadata. A field ID is independent of a
public VST parameter index. These maps describe serialized storage, not a live
C++ object or an inferred physical control range.

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

`controls.ControlMap` records sampled public-knob bindings, including coupled
fields, source/evidence hashes, measurement scope and unresolved status. Sample
lookup refuses interpolation. `ControlBinding` holds an executable curve only
where that curve is implemented. The sampled map and executable bindings are
different types so an observation cannot silently become an arithmetic rule.
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

A transform consumes a source model and produces a target model. It owns
correspondence, defaults, unsupported features and intentional losses. Only a
completed transform belongs in the existing `register_custom_state` registry.
A field map or successful byte round trip does not establish sound equivalence.
Registration needs an implemented, guarded source-to-target conversion with its
supported scope, validation and losses stated explicitly. A storage codec alone
does not meet that contract.

## The Ableton boundary

`PluginData` owns the hex-encoded XML Buffer. Its `decode_state` takes an explicit
decoder; `set_state_model` finishes encoding before changing the XML. Neither
method guesses the plugin format, changes its identity or rewrites automation.
A VST2 chunk and a VST3 processor/controller pair require their respective
codecs and container adapters.

For example, with `plugin` parsed from the intended VST2 info element:

```python
from abletoolz.plugins.state.izotope_trash import Bank

bank = plugin.decode_state(Bank.parse)
program = plugin.program_number
if program is None:
    raise ValueError("Select the saved bank program explicitly")

settings = bank.program(program).settings
frequency_hz = settings.value("PreFilter: Node 1 Freq")
edited = bank.with_values(program, {"PreFilter: Node 1 Freq": 823.25})
plugin.set_state_model(edited)
```

The caller retains the ALS `ProgramNumber` explicitly. It is zero-based, as is
`bank.program(index)`, which validates the bank's bounds. If absent, the caller
must supply a chosen index instead of the example's error; the codec never
defaults to program 0. A complete bank does not identify its active program.
Field edits change only the saved state. Reconciling
configured host parameters, automation and program selection is a separate
caller responsibility.

## Promoting research

Keep native captures, reverse-engineering output and rejected predictions as evidence. Promote
stable field definitions and measurement scope into package data, then build the
vendor codec on the shared models. Retain unknown regions and refuse ambiguous
versions. Add regression fixtures from independent native saves and real saved
buffers where available; a parser encoding its own synthetic output is not
native acceptance evidence. Package data and tests must run without the research
sandbox, a plugin DLL, a DAW or a remote machine.

The Trash 1 bank model packages typed fields and sampled controls. The Trash 2
model's sixteen executable core curves remain restricted to their measured VST2
template. Algorithm/routing correspondence from Trash 1 to Trash 2 is still
research, so no conversion between them is registered.
