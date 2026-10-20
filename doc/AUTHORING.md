# The set authoring model

`plugins/` turns Ableton's plugin bytes into something a program can
reason about. This package does the same for the set itself: tracks, clips,
notes, device chains and arrangement sections, read out as typed objects and
written back as the XML the running Live version expects.

Two things use it. A person, through `cli.py`. And a program -- an LLM being
the case it was built for -- through exactly two doors: `describe()` reads a
set out as JSON, `apply_ops()` writes a batch of changes back. Everything
else in this package is the domain those two doors stand in front of.

## The four layers

Every module sits at exactly one depth, and depends only downward.

```
  apply_ops.py   describe.py        <- the doors: JSON in, JSON out
        |             |
      clips  devices  tracks  samples  plugins  transport
        |             |                              <- the domain: typed objects
      sections.py   sample_ref.py
        |                                            <- the mechanics: elements and ids
      xml_edit.py   persistence.py   schema/
```

`document.py` is the hub the layers meet at: `AbletonSet` owns the parsed
tree and hands out one accessor per domain (`set.clips`, `set.devices`,
`set.tracks`, `set.samples`, `set.plugins`, `set.transport`, and the callable
`set.describe`). Accessors are the only way in; nothing reaches across to
another domain's internals.

## Where each module sits

| Module | Layer | Job |
| --- | --- | --- |
| `describe.py` | door | a set as a tiered JSON document -- STRUCTURE, PATTERNS, FULL -- with clip content deduplicated into a pattern table and placements referencing it |
| `apply_ops.py` | door | a JSON ops document validated whole, then run; one typed result per op, never prose |
| `clips.py` | domain | the MIDI note model, note reading and writing per schema generation, audio clip discovery, and the arrangement placement decisions |
| `devices.py` | domain | device chain inventory per track, grafting a chain from a donor set into a target, and `walk()` over every device of a kind (racks included) to read or write its settings |
| `tracks.py` | domain | listing, view state, fold state, colors, routing |
| `samples.py` / `sample_ref.py` | domain | sample references, missing-file checks, relinking; `sample_ref` holds the per-version path shapes |
| `plugins.py` | domain | VST2/VST3/AU references and whether this machine can load them |
| `transport.py` | domain | tempo and timeline length |
| `sections.py` | mechanics | where clips live on a timeline: the `Events` lists, session slots, spans, straddle rules, automation envelope segments, and the corpus measurements behind all of it |
| `xml_edit.py` | mechanics | the set-global Pointee id space, indentation, number formatting, remote bindings |
| `persistence.py` | mechanics | loading and saving through `formats.live_document`, backup naming, file times |
| `schema/` (package root) | mechanics | Live's names as StrEnums: `Tag` and `Attr` for document structure, the generated `Device` enum with one settings enum per native device, and the table of every tag Live renamed |

## The rules the package is built on

**Version differences live in two places and nowhere else.** A tag that Live
renamed goes in `schema`'s rename table. Logic that genuinely differs by version
gets a `@versioned` override, the way `clips.py` carries one
`_note_attributes` per schema generation. Feature code never hardcodes a
spelling and never branches on a version number inline.

**Readers and writers see the same clips.** Anything that finds arrangement
clips goes through `sections.arrangement_events()`. A reader that reached
further than the writers -- into a take lane, into a rack -- would report
clips to a caller that no op could then touch.

**Ids are set-global and Live checks them.** `Pointee`, every
`*AutomationTarget`/`*ModulationTarget` and `ControllerTargets.<N>` share one
counter at `LiveSet/NextPointeeId`. Any subtree grafted or cloned into a set
must be renumbered above it and the counter advanced past;
`xml_edit.renumber_pointee_ids` is the only thing that does this. Clip-local
ids -- `KeyTrack/@Id`, `MidiNoteEvent/@NoteId` -- are a different space and
must not be confused with it.

**A write path is not done until Live has loaded it.** The version fixtures
under `test/version_fixtures/skeletons/` are hermetic XML oracles and are
*not* Live-loadable; opening one crashes Live 12. Verification artifacts get
built on a real set.

**Sections are never trimmed.** A clip crossing the edge of a section is
reported and left alone, never silently truncated -- which is why the section
reports name what they kept as well as what they moved.

**Nothing returns English to a program.** Domain writers answer with typed
reports; `apply_ops` answers with one typed result per op. Prose is
`run/console.py`'s job, rendered from the same fields.

## Reading a set out

```python
from abletoolz.live_set import AbletonSet
from abletoolz.live_set.describe import DescribeLevel, describe_json

live_set = AbletonSet(path)
live_set.parse()
print(describe_json(live_set, DescribeLevel.PATTERNS))
```

Notes ship as positional arrays -- `[pitch, start, duration]`, with velocity
and off-velocity appended once either leaves default and a trailing dict for
probability/deviation/disabled at FULL. `encode_note`/`decode_note_array` are
the only pair that speaks that shape.

## Writing a set back

```json
{"ops": [
  {"op": "copy_section", "src_start": 128.0, "src_end": 192.0,
   "dest_start": 384.0, "tracks": ["Drums"]},
  {"op": "set_notes", "track": {"name": "Bass"}, "clip": {"start": 384.0},
   "notes": [[36, 0.0, 1.0], [43, 2.0, 0.5]]}
]}
```

Every selector in the document resolves before the first XML edit, so a
document with one bad op changes nothing. Track selectors resolve by
identity: two tracks can share a name, so `{"name": ...}` raises on ambiguity
rather than guessing, and `{"id": ...}` is the unambiguous handle.

## What this package does not read yet

Worth knowing before assuming a fact is available: **locators** (the named
arrangement markers -- "Intro", "Drop" -- that are the closest thing a set
has to a section map), **mixer state** (volume, pan, sends, mute, solo),
**scenes**, and **time signature**, which is why `transport.length()` still
assumes 4/4. Automation envelopes are read and copied by `sections.py` but
are not surfaced by `describe()`.
