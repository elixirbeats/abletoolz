# The plugin transform model

Every plugin conversion a set can need decomposes into three independent moves.
Each module in this package serves exactly one of them; the config YAML is the
policy that picks a strategy per axis. Living document: when a measurement
lands, it goes here. If it isn't measured or implemented, it isn't stated as
fact.

## The three axes

### 1. Container — what XML shape does Ableton write?

Format-pair knowledge, plugin-independent, finite. A device is a `PluginDesc`
wrapping one info element — `VstPluginInfo`, `Vst3PluginInfo`, `AuPluginInfo` —
and the `PluginDevice` wrapper around it is byte-identical whichever format is
inside (measured on Live 12.4.5). So a container change is entirely local to
the info element: rename, drop the source format's fields, add the target's,
in the order that version of Live writes them. The keep-list rebuild in
`format_translation.py` makes one rule cover every Live version 9–12.

Implemented pairs live in `_TRANSLATIONS`: today `(VST, VST3)`. AU is one more
entry plus a reader, not a redesign. The degenerate pair (same format both
sides) is no container change at all — that is what `upgrade_rules.py` does.

#### The container has a floor

A set's XML is schema-versioned by its root element, and Live reads a tag as a
class only if the schema that version declared knows the name. A container
change is therefore not free: writing a `Vst3PluginInfo` into a set saved by
Live 9 does not produce a set with a VST3 device in it, it produces a file Live
will not open at all —

    ... is corrupt and cannot be loaded.
    (Unknown class 'Vst3PluginInfo' encountered (at line 19371, column 25))

measured 2026-08-13 on Live 12.4.5b, opening a Live 9.0.1 set
(`MinorVersion 9.0_305`) whose two Pro-Q 1 devices had been translated to
Pro-Q 3. Identity and state were both right; the document could not hold the
element at any quality, and the failure takes the whole file rather than the one
device.

The floor comes from the library, by document schema. `MinorVersion` 9.0_305
(9 sets), 9.5_326 (10), 9.5_327 (22) and 10.0_370 (61) carry no
`Vst3PluginInfo` between them; 10.0_377 — the schema Live 10.1 writes — carries
129 across 23 sets. By `Creator` the line falls between 10.0.6 and 10.1, so
`set_supports` puts the VST3 floor at 10.1.0. Repair reports a device below it
as `set_too_old_for_target` and writes nothing, since the fix is to open the set
in Live and save it, which upgrades the schema.

#### One shape does not translate

Measured over 811 sets: 54 devices in 11 of them carry a stub `VstPluginInfo` —
`Dir`, `FileName`, `PlugName`, `UniqueId`, and a `Preset` in fewer than half —
with no `Category` and none of the rest. There is no `Category`, so the set
never says whether the device is an instrument, and `DeviceType` cannot be
written. `is_translatable` says so and repair reports it as `incomplete_device`
rather than guessing.

Every one of those 11 sets was written by a third-party set generator, not by
Live. That correction is the point: an earlier version of this file credited the
shape to Live 9.7.7, on the strength of 22 sets whose `Creator` says 9.7.7 and
whose XML Live never produced. Re-measured 2026-08-13 with provenance held
apart, four fingerprints agreeing on the same 23 files out of 811:

* **Indentation.** All 788 Live-written sets indent with tabs. 22 use four
  spaces; 1 has no indentation at all.
* **Mixed indentation.** No Live-written set mixes styles. All 22 of the
  four-space sets contain tab-indented fragments — Live's own markup pasted
  into the generator's output.
* **`Creator` against content.** `<RelativePathElement Id="…">` appears in 142
  Live 10 sets and in none of the 19 authentic Live 9 sets, yet all 22 sets
  declaring Live 9.7.7 carry between 81 and 180 of them.
* **Name.** All 23 are named `jukeblocks`/`jukebox`. The name alone
  over-selects — 14 more sets carry it and were written by Live, which re-saved
  them — so the structural fingerprint is the test and the name is corroboration.

Held apart that way, four shapes this library once called Live behaviour turn
out to be one tool's output and nothing else:

| Shape | Generated sets | Live-written sets |
| --- | --- | --- |
| stub `VstPluginInfo` | 54 devices, 11 sets | 0 of 12,123 VST2 devices |
| stub `Dir` holding a bare file name | 30 devices, 10 sets | 0 |
| group/return track with no `TrackGroupId`, lane height, session width or fold state | 44 return + 44 group tracks, 22 sets | 0 |
| `SampleRef` with no `LastModDate` | 98 refs, 14 sets | 0 of 76,906 refs |

No version of Live was ever observed to write any of them. Reading them anyway
is still in scope — abletoolz generates sets itself, and a set another tool
wrote is a real file on a real disk — so the handling stays and only the
attribution changes. The fixture is `test/version_fixtures/generated/`, kept out
of the version matrix because it can testify to a generator's output and not to
any version of Live.

#### A transplanted device drops its remote bindings

A device copied into another set must lose the `KeyMidi` elements its
parameters carry, or Live 12 dies on load — access violation during document
exchange, after every plugin has already restored. Measured 2026-08-13 by
bisecting an authored set on 12.4.5b11 down to one VST2 device: its subtree was
byte-identical to the donor's (1,894 paths, no differences) and only removing
the two bindings made the same file open. The donor set carries eight identical
blocks and opens fine, so a binding is not malformed — it is fatal transplanted,
and why is not known.

### 2. Identity — what string or id makes the host load the right plugin?

Per-plugin data, nothing more:

* VST2: file name/path, plus a 4-byte UniqueId integer.
* VST3: display name plus a 16-byte class id. Live stores it as four
  big-endian signed int32 `Uid` fields; the same id appears dashed inside
  Live's database `dev_identifier` and undashed in `moduleinfo.json`. Binaries
  hand it back from `GetPluginFactory` in COM GUID byte order — convert before
  comparing.
* AU: a FourCC triple (type, subtype, manufacturer) stored as uint32s.

Identity sources, ranked by authority (all four agree on every overlap
measured so far — a disagreement is always surfaced, never averaged away,
because a wrong class id makes Live silently load a different plugin):

1. Live's own plugin database (ids for both formats, plus vendor).
2. Sets that already use the target plugin.
3. The plugin binaries themselves.
4. `moduleinfo.json` (few vendors ship it; Waves shells never do).

`plugin_db.py` snapshots this machine's inventory per source;
`uid_sources.py` resolves a name to an id through the ranking; `mapping.py`
does name matching for suggestions. Name matching is identity confidence
only — it says nothing about state.

### 3. State — do the preset bytes carry over?

The only hard axis. Four rungs, cheapest first:

1. **verbatim** — same bytes both sides. Measured by ear for Serum, Ghz Tupe 3,
   Prophet V3, Serato Sample, FabFilter (same container generation both
   formats).
2. **reframe** — same payload, new envelope. First found on Kilohearts: every
   Kilohearts VST3 wraps the VST2 zip payload in an 8-byte header, two
   little-endian uint32 `(1, payload length)`. Measured; ear-validated on two
   plugins. The 2026-08-15 readback survey found the same shape across
   vendors, none implemented yet: soothe2 and Rift's VST3 state is the entire
   VST2 chunk plus a 60-byte `JUCEPrivateData` trailer; Valhalla, Eventide and
   Soundtoys carry the VST2 chunk unchanged at byte 176 of a `VstW`+FXB
   envelope; Diva's VST3 adds a 4-byte length prefix to the same preset text.
   Each is one primitive operation — add or strip a trailer, wrap or unwrap,
   prefix a length — not a per-vendor codec.
3. **vendor-compat** — pass the old bytes and the target plugin migrates them
   itself. Declared in some `moduleinfo.json` Compatibility blocks (Serum's
   VST3 declares it accepts VST2 state); otherwise discoverable only by
   experiment. Predictable when declared, an experiment when not.

   A version migration — Pro-C 1 → Pro-C 2, Volcano 2 → Volcano 3 — is this
   rung by definition, and it is where the host rig was pointed first.
   Measured 2026-08-13, every one of them refused: Volcano 2 → Volcano 3,
   Pro-Q 1 → Pro-Q 3, Pro-Q 2 → Pro-Q 3, Timeless 2 → Timeless 3,
   Saturn 1 → Saturn 2, Pro-C 1 → Pro-C 2, Ozone 4 → Ozone 9 and
   Ozone 8 Elements → Ozone 9. No vendor on this machine migrates a patch for
   you. The rung is empty of FabFilter and iZotope; those pairs are rung 4.
4. **re-encode** — nobody migrates for you; a real parser for the source
   format and a serializer for the target (the Analog Lab 1 → 4 class of
   problem). The `state` package keeps a registry, `register_custom_state("name", fn)`
   fills it, and a config entry reaches it with `state: custom:name`. A
   `custom:` naming nothing registered refuses to load the config, because the
   alternative is passing the old bytes through and calling it verbatim.

   One is written — `custom:fabfilter-q1-to-q3`, in `fabfilter.py`. See "The
   re-encode" below.

The `state` package holds all three: which policy an entry may name, what each
does to the bytes, and `MEASURED_STATE` — the table below, in code. Vendor-compat is
not a policy there; the bytes still pass verbatim and the declaration is
evidence, which is the other axis of the same module.

#### The ControllerState beside it

A VST3 preset holds two blobs, and Live writes exactly one `ControllerState`
per `ProcessorState` — 917 pairs out of 917 in a 100-set sample. Whether the
second holds anything is a fact about the plugin: over 150 sets it is populated
704 times and empty 377. soothe2, Oszillos Mega Scope, Rift, Phase Plant, SPAN
Plus and Smack Attack write none; Diva writes ~6.7 KB, Arturia Mini V3 ~15.9 KB,
Serum 2 ~2.4 KB, Chorus JUN-6 ~2.1 KB.

That corrects a statement this library carried until 2026-08-15 — that plugins
rebuild the controller state from the processor state, so an empty one is
enough. The listening behind it was real; every plugin it was heard on happens
to write no controller state anyway. Headless readback says the general claim is
false: iZotope Trash 2 and u-he Diva both revert to their defaults when handed a
processor state with no controller state beside it. So a `TranslationTarget`
declares one, in one of three shapes — nothing, a constant blob, or FabFilter's
editor form.

**FabFilter's two.** Across 200 sets, Pro-C 2 (203 devices), Pro-L 2 (100),
Pro-MB (18) and Pro-R (36) write one constant twelve byte value and nothing
else: `FFed`, a zero word, a float32 1.0. Not one of the 357 varies. Pro-Q 3
(189), Saturn 2 (29) and Timeless 3 (17) write a longer one ending in those same
twelve bytes, with the editor state in front of them:

    the product's 4CC (`FQ3p`, `FS2a`, `F3Ts`), u32 version 3, u32 length + the
    preset name in ASCII, i32 instance index, u32, u32 length + a label, u32,
    `CuSV`, u32, u32 count, and that many (name, value) pairs of
    length-prefixed strings — a Timeless 3 device's XY pads, LFOs and envelope
    followers are in there by name.

Which is where **the preset name lives** — the second statement corrected.
`fabfilter.py` used to say Pro-Q 3 keeps its name in an editor state an `.als`
does not carry. The `.als` carries it, and this element is it.

Three fields there are carried rather than understood. The instance index reads
-1 in half the devices measured and 0 to 4 in the rest, each non-negative value
at most once per product in a set; what it indexes is unknown, so a device
written here says -1. The three words around `CuSV` read 1 everywhere seen. The
label is a display string Live hands the plugin — a track name in most devices,
the whole device chain in one — and a translated device carries none, which is a
length the corpus also holds.

**A VST2 chunk already holds this element.** `state: fabfilter` cuts an FFBS
chunk at the editor magic and gives the first half the `FFpr` trailer; the half
it cuts off is this, less the `FFed` trailer. Read off a Pro-Q 3 and a
Timeless 3 VST2 buffer, same layout both sides. So a same-product translation
carries that half across whole rather than building one, and the name, the label
and every named controller cross unchanged.

#### What a buffer is — the container families

Measured 2026-08-14/15 over every state buffer in 811 sets — 23,786 device
instances, 10,966 distinct buffers — classified by leading bytes and structure,
weighted by device count (rigs and raw data:
`unfinished_sandbox/format_translation/state_survey/`):

| Family | Devices | Detection |
| --- | ---: | --- |
| opaque binary | 37.4% | none of the below |
| FabFilter framed | 20.3% | `FFBS`/`FFed`/`FabF`; the VST2 banks open with their preset name, so they read as an FxBk bank, not by tag |
| mostly-text framed | 10.9% | ≥75% printable behind a short binary header |
| plain text | 10.4% | fully printable |
| empty | 7.6% | zero bytes — a default patch stores nothing |
| JSON | 3.3% | `{`, or `XferJson` |
| zlib/deflate | 3.1% | `78 01/9c/da`, `1f 8b` |
| `VstW` wrapper | 2.3% | `VstW` at 0; FXB inside; chunk at 176 |
| zero-prefixed | 2.3% | zero words, then a vendor 4CC |
| XML | 2.2% | `<?xml` or `<` |
| zip archive | 0.1% | `PK\x03\x04` (Kilohearts) |

Half the library reads without the plugin: the text-like families plus
FabFilter's decoded ones. Coverage is concentrated — Pro-Q 3 alone is 2,488
devices, and SPAN's 475 devices share 24 distinct buffers — so one product's
parser can cover a large share of real devices.

Three comparison rules, each learned from a false difference: a compressed
body is never compared as bytes (Serum re-deflates differently and inflates to
the identical 172,736); XML is parsed, not hashed (a JUCE host inserts the
prolog and reformats floats on readback); a zip differs only in its DOS
timestamp when its content is identical (Kilohearts).

#### The bytes cross hosts

The claim behind the whole state axis — the DAW stores the plugin's own
serialization and only wraps it — is measured, not assumed. Buffers harvested
from real sets were pushed into the same plugins under foreign hosts
(DawDreamer for VST2, pedalboard for VST3), 25 targets and 48 pushes spanning
the families (`state_survey/readback_results.json`): a third came back
byte-identical, including Pro-Q 3, Pro-C 2, Mini V3 and Trash 2, and in no
case did a host contribute bytes to a VST3 processor state. Where the readback
differs, the cause is almost always the installed plugin being a newer build
than the one that wrote the set — Blackhole restamps two bytes of version,
soothe2 rewrites `2.1` to `3.0`, Serato adds seven schema keys and keeps all
84 it was given. On the VST2 side an `.als` stores the bare chunk — the FXB
program payload with the floats byte-flipped — so the FXB framing a host wants
is added at the boundary, which is Ableton's omission and not a host addition.

Three hazards the same experiment surfaced:

* **A Waves state is not sample-rate portable.** An F6 buffer written at
  48 kHz, read back at 44.1 kHz, had a stored value rewritten by exactly
  48000/44100. Round-tripping a Waves device at the wrong rate silently
  detunes it, and a rig comparing across rates reads a false difference.
* **SPAN silently refuses its older bank layout.** The single most widespread
  SPAN buffer in the corpus — 337 devices across 335 sets, channels named
  "1"/"2" — loads as a no-op: the installed SPAN reverts to default and
  reports nothing. The later layout ("Mid"/"Side") loads byte-exact.
* **A vendor can change container mid-life.** The corpus holds Kilohearts
  states from before the zip container as flat binary; the installed plugin
  accepts one and returns the zip form. Detection has to ask the bytes, not
  the plugin name.

## The predictability rule

A conversion is predictable exactly when all three axes are known:

* the container pair is implemented,
* the identity comes from an authoritative source,
* the state rung is measured for that plugin (by ear or vendor declaration).

Anything less is an experiment. Experiments run on disposable copies, and
their results land in the table below.

Every suggestion and every repaired device is labelled with which of the two it
is: `state: verbatim (ear 2026-08-10)`, `state: reframe (hosted 2026-08-13)`, or
`state: unknown — experiment, audition before trusting`. A repair run counts
what it fixed in two piles, measured and experimental, so the second number is
how much listening the user still owes. Evidence is `ear`, `declared` (a
vendor's `moduleinfo.json`), `hosted` (the host rig below), or `structural` —
and only the first two make a conversion predictable, because a plugin that
takes a patch can still sound wrong, and bytes that look right are only where a
listen starts.

## The host rig

The state axis was the one axis nothing could be read off a set. Whether Volcano
3 understands a Volcano 2 patch is not in the bytes, in Live's database or in
`moduleinfo.json` — it is in the plugin, and the way to find out is to ask the
plugin. The rig does that without Live and without a human: pedalboard loads the
installed VST3, a patch harvested out of the user's own sets goes in, and three
independent readings say whether anything happened.

**The envelope.** pedalboard is a JUCE host, so its `raw_state` is not the
plugin's state — it is `AudioProcessor::copyXmlToBinary` around JUCE's VST3
document. Measured layout: magic `0x21324356` (`VC2!`) little-endian, a uint32
UTF-8 length, the XML, then a NUL the length does not count. The XML is
`<VST3PluginState><IComponent>…</IComponent><IEditController>…</IEditController>`
and each body is `MemoryBlock::toBase64Encoding` — a decimal byte count, a `.`,
then six-bit groups read least-significant-bit-first through the alphabet
`.A-Za-z0-9+`. The bytes inside `IComponent` are the bytes an `.als` holds as
`ProcessorState`; that equality is measured, not assumed, by injecting a
set-harvested `ProcessorState` and getting the same bytes back.

**The three readings**, because any one of them lies on its own: an exact
readback, every parameter's value, and two fixed seeded renders (noise and a log
sweep). Sound is the only one that speaks about sound, and it is worth exactly
as much as the plugin is repeatable — so the rig renders the untouched default
twice first and treats that difference as the plugin's noise floor. Timeless 3's
floor is 0.23 of full scale, a free-running LFO; a rig that had not measured it
would have read the LFO as a patch being applied.

**What acceptance is.** Not "something changed". Measured 2026-08-13: hand a
FabFilter VST3 a blob with a correct header and a random body and it renders
differently and reports new parameters — it reads the body as a positional
parameter array without checking who wrote it. Pro-C 2 answers a Pro-C 1 patch,
a Pro-Q 1 patch, a Saturn 1 patch and a random body with the *same* render, to
six decimal places. Only an exact readback of the injected bytes is acceptance;
a render delta on its own is the plugin resetting somewhere that is not its
default. Random bytes alone would never have caught this, because random bytes
fail the header check and never reach that code.

**Sandboxing.** Every load is its own process with a hard timeout, like the
class-id prober, and for the same reason: garbage hangs three Soundtoys plugins
outright. A hang costs one trial.

Two targets are out of reach rather than measured. Trash 2 refuses its own
`ProcessorState` harvested from a set that plays it, so nothing can be said
about Trash 1 → Trash 2 from here. Stutter Edit 2 takes its state and echoes it
back but exposes two parameters and passes audio through untouched until MIDI
triggers it, so the rig has no channel to read. Both are UNTESTABLE, which is
not REJECTED.

## The re-encode

Pro-Q 1 → Pro-Q 3 is the fourth rung's first implementation, and it exists
because the third rung is empty: the rig asked Pro-Q 3 whether it understands a
Pro-Q 1 patch and it does not. The two formats have nothing in common. Pro-Q 1
exposes no chunk, so what an `.als` stores for it is Live's own
stored-parameter bank — a 28 byte NUL-padded preset name, then one float32 per
parameter normalized 0 to 1, in the host's units. Pro-Q 3 stores `FFBS`, a
version, a count and that many float32 in its *own* units, log2 Hz and dB. 177
parameters against 358, seven fields per band against thirteen.

`fabfilter.py` reads the one and writes the other. Slot 0 of the bank is the
used-band count over 24; each band it covers is decoded — frequency logarithmic
over 10 Hz to 30 kHz, gain linear and centred on 0.5 for ±30 dB, Q identical on
both sides — and written into Pro-Q 3's thirteen. The two enums are the edge
that would otherwise pass unnoticed: Pro-Q 3 kept Pro-Q 1's five band shapes in
order and added four, so shapes map straight across, while it offers nine cut
slopes to Pro-Q 1's four, so Pro-Q 1's 24 dB/oct is Pro-Q 3's slope 3 and not
its slope 2. Both maps are built by looking a name up in the other version's
list, so a reordering fails at import instead of quietly turning every low cut
into a notch.

Everything Pro-Q 1 has no counterpart for keeps Pro-Q 3's own defaults, read
off the installed VST3 rather than out of anybody's set. Two things do not
survive, and they are limits rather than bugs: per-band placement, because
Pro-Q 1's non-stereo encoding is unresolved and every band therefore lands on
Stereo; and automation, because the two versions do not number their parameters
alike. The preset name was a third until 2026-08-15, on the strength of the
statement corrected above — Pro-Q 3 keeps it in the editor state, and the `.als`
does carry that, as the `ControllerState`. It crosses now.

Three readings agree. A converted set opened in Live 12 on 2026-08-09 with its
bands where the Pro-Q 1 had them. Pro-Q 1's default band and Pro-Q 3's default
band both decode to 1 kHz through formulas that share no arithmetic. And on
2026-08-13 the rig converted every distinct Pro-Q 1 patch in the collection —
138 of them, out of 70 sets in 812, both spellings, 0 to 9 bands each — and
injected every one into the installed Pro-Q 3. All 138 came back byte for byte,
with the plugin's own parameter readings agreeing band for band with what the
remap computed. Every one of the 138 banks holds exactly 177 parameters, which
is the layout assumption the whole remap rests on.

The first pass was 135 of 138, and the three that missed are worth the sentence.
Each had a band at the very top of the range, where the two versions disagree in
the last two bits of one float: Pro-Q 3 stores 29999.981 Hz for its own maximum
and log2 30000 rounds to 30000.001. `fabfilter.py` clamps to the plugin's
number. The difference is 0.02 Hz at 30 kHz — inaudible, and above the audible
band besides — but writing the arithmetically correct value instead costs an
exact readback, which is the only acceptance signal the rig has. The plugin's
own range readings confirm the rest: 10 Hz to 30 kHz, ±30 dB, the same as
Pro-Q 1, and both gain ends and the frequency floor round-trip untouched.

### The second one, off a corpus rather than a plugin

Pro-C 1 → Pro-C 2 is the same rung reached a different way. The target half was
already measured — Pro-C 2's derived table says what each of its 46 parameters
stores and how a normalized value gets there. The source half could not be
asked at all: Pro-C 1's VST2 is a 32-bit build no 64-bit host will open, so
neither the derivation rig's stamp-and-save proof nor a parameter list was
available from the binary.

Live had written it down. Every `PluginDevice` carries a `ParameterList` beside
the `Buffer` — one record per plugin parameter, with the plugin's own index, the
plugin's own name for it, and the value Live last saw. Swept over the whole
collection on 2026-08-15: 847 sets, 608 Pro-C 1 devices, 566 of them still
binding names to indices, **all 31 names recovered and not one index
contested**. The 42 that bind nothing are devices Live re-saved after the plugin
went missing, and 4 more carry `Parameter #1`…`#31` placeholders, which is Live
saying it has no name rather than disagreeing about one.

That record is also a second reading of the bank itself. Of 17,453 parameter
readings across the 563 devices that hold a patch, 17,410 agree with the float
at the same index in the `Buffer`. The 43 that do not are all Live's `Manual`
reading 0 where the bank holds a value, on 8 indices, and never the scattered
disagreement a shifted layout would give — so the bank's float order *is* the
parameter index order, which is the fact the whole re-encode rests on, proved
out of Live's record instead of out of DawDreamer.

The join is by name against Pro-C 2's table: 9 of the 31 pair on the name
alone, 13 are written down by hand with a reason each, and 9 are dropped. The 22
Pro-C 2 parameters nothing claims keep the plugin's own defaults. The dropped ones are
listed in `fabfilter.py` with why — Pro-C 1's `Knee Shape` is a switch where
Pro-C 2's `Knee` is a continuous 0–72 dB width, its per-channel side-chain mix
has no counterpart, and its separate `Auto Release Speed` would have to
overwrite the Release knob. Two Pro-C 2 flags have no source parameter at all:
Pro-C 1's side-chain filters are always in circuit and are opened by running
them to the end of their range, so `Side Chain Low/High Enabled` is set from
whether the filter was doing anything.

**What is assumed, and said out loud.** A normalized 0.5 means whatever range
the plugin declares, and Pro-C 1 cannot be asked what its ranges were. Where the
corpus can check it, it checks out: 0.5 in Pro-C 1's four pans, its side-chain
level and its input and output levels all decode through Pro-C 2's own curves to
the exact defaults Pro-C 2 ships, and `Dry Level`'s 0 decodes to Pro-C 2's −1.
Threshold, ratio, attack, release and the two side-chain filter frequencies have
no such check and pass through assumed — Pro-C 2 defaults its ratio to 0.6
normalized where the most repeated Pro-C 1 value is 0.5, which is either two
products defaulting to different ratios or two different curves, and no bank
says which. Two enums are widened rather than passed through, because Live
stores an enum as its index over its top index and 0.5 of three settings is 1
while 0.5 of eight is 3.5: `Characteristic` takes exactly three values in 188
patches and `Meter Scale` exactly four, and both are rewritten against Pro-C 2's
counts on the assumption that the newer product kept the older one's settings in
order — the way Pro-Q 3 kept Pro-Q 1's five band shapes. A setting the
collection never used would make a count too small and land the middle settings
one place early.

Acceptance is the same bar: every distinct Pro-C 1 patch in the collection — 189
of them, out of 608 devices — converted through the registered transform and
injected into the installed Pro-C 2 VST3. **189 of 189 came back byte for byte.**
That says the plugin reads what was written; it does not say the ranges were
right, and this one needs a listen more than any conversion so far.

## The derivation rig

Pro-Q 1 → Pro-Q 3 was cracked by hand, and that does not scale to a product
line. The FabF-generation products — Pro-C 2, Pro-L 2, Pro-MB, Pro-DS, Pro-G,
Micro — have the same shape of problem: an `.als` stores their VST2 as a bank of
normalized floats and their VST3 as a `FabF` block of floats in the plugin's own
units, and nobody outside FabFilter has written down the curve between the two.
The rig derives it instead of guessing it, by asking both binaries.

**Source side**, DawDreamer, which loads the VST2: the ordered parameter names,
and a proof that the bank's float order is the parameter index order. Every
parameter is stamped with a value no other parameter has, the bank is saved, and
each float has to sit nearer its own stamp than any other's. Pro-C 2: 46 of 46,
worst error 3e-08. The `.als` Buffer is that bank's program payload verbatim,
with only the float byte order flipped — FXB is big-endian by the VST2 spec and
Ableton writes little-endian.

**Target side**, pedalboard, which loads the VST3: move one parameter, diff the
`FabF` float array, and the slot that moved is that parameter's. Then sweep it
across 65 positions and fit the curve — never propose one. A line covers dB,
log2 Hz, ratios and identity; a lookup covers enums and switches; anything
neither reproduces gets the measured points themselves, which is the honest
answer rather than a fallback. The two coefficients of a line are what the
plugin wrote at 0 and at 1 rather than a regression, because least squares puts
2e-16 where the plugin puts a clean zero and the state stops being byte-identical
to one the plugin would have written. One rule had to be measured rather than
assumed: the step index is round-half-up, so a two-way switch at exactly 0.5
reads as on, and Python's banker's rounding sent 22 of Pro-C 2's parameters
through the fallback before that was noticed.

**The join is by parameter name**, which is the one thing the two formats agree
on. Same-product it is exact; for a version migration it goes through fuzzy
matching and whatever fails to pair is reported rather than guessed. That half
is written and untested against reality, because the legacy 64-bit VST2s are not
installed.

Pro-C 2 is the proof. 46 parameters, every state slot owned by exactly one of
them, none unattributed and none contested, 24 lines and 22 lookups and no
fallbacks, worst residual 7e-07. Nine patches — eight real Pro-C 2 devices out
of the user's sets and the corpus fragment — converted and injected: all nine
echoed back byte for byte, and all 414 parameter readings agree with what the
VST2 says the same bank means. Pro-L 2 came out the same way, 256 of 256.

`custom:fabfilter-pro-c-2` is that table, in `abletoolz/data/fabfilter/`. It
earns its own line of evidence rather than inheriting the rig's: the library
evaluates the table to the same bytes the rig verified, on all nine, and every
distinct Pro-C 2 patch in the collection — 28 of them, out of the same 812 sets
— converts and comes back out of the installed VST3 unchanged. The preset name
survives this one inside the processor state, where the `FabF` container has a
field for it; Pro-Q 3's rides in the `ControllerState` instead.

Three words in the container are copied rather than understood — a zero before
the parameter count and two trailing ones — so a built state differs from one
the plugin wrote only where a parameter actually differs. They were constant
across every product and every patch seen, and constant in every sample is not
the same as understood.

What the rig cannot reach is a generational boundary rather than a gap. The
FFBS products — Pro-Q 3, Pro-Q 4, Pro-R 2, Saturn 2, Timeless 3, Twin 3,
Volcano 3 — save an opaque `FBCh` chunk on the VST2 side, so there is no
indexable float array for a per-parameter transfer to bridge. Those are the ones
the reframe in the `state` package already handles. Two smaller findings worth keeping:
Pro-G really does have two parameters on one slot, "Style" and "Ex Style" being
two host-facing views of one internal enum, so whichever writes last decides it
and the rig says so; and three real Pro-L 2 devices hold 31 floats where the
installed build declares 32, which is skipped and reported rather than padded,
because padding would shift every value after the insertion into the wrong knob.

## Where each module sits

| Module | Axis | Job |
| --- | --- | --- |
| `format_translation.py` | 1 | container rewrites; `TranslationTarget` bundles one policy choice per axis |
| `plugin_db.py` | 2 | machine inventory, per-source records, disagreements kept visible |
| `uid_sources.py` | 2 | name → id resolution through the source ranking |
| `mapping.py` | 2 | name matching + suggestion formatting (shared by repair and the suggester) |
| `upgrade_rules.py` | 2 | same-container file-name swaps |
| `state/__init__.py` | 3 | the seam: which policy an entry may name, what it does to the bytes, what goes in the `ControllerState`, and what is measured about each plugin |
| `state/families.py` | 3 | what a buffer is, read off its own bytes, and the reframes those families share |
| `state/fxbk.py` | 3 | the standard VST2 bank a host writes for a plugin that exposes no chunk |
| `state/derived.py` | 3 | a transfer table the derivation rig measured, and the `FabF` container it writes |
| `state/fabfilter.py` | 3 | what really is FabFilter's own: its `FFBS` chunk, its editor state, and the re-encodes the seam dispatches to |
| `state/serato.py` | 3 | Serato Sample's JSON -- the one per-plugin parser written before this model |
| `state/xfadelooper.py` | 3 | a fixed-width struct: one sample path in a 256 byte field, read and rewritten in place |
| `state/maschine.py` | 3 | Maschine 2's nested NI chunks: kit and sample references found by their length prefix, reported rather than rewritten |
| `base.py` / parsers | 3 | per-plugin buffer analysis |
| `data/fabfilter/` | 3 | derived tables: what the rig measured off a product's two binaries |
| `repair.py` | policy | broken (Live's database says so) ∩ mapped (config says so) → translate |

Two deliberate splits: the repair oracle reads Live's database live, because
loadability flips with a preferences toggle while identity does not; and
nothing converts without an explicit config entry — suggestions are emitted
commented out so enabling one is a conscious act.

## Measured state rungs

| Plugin | Rung | Evidence |
| --- | --- | --- |
| Serum | verbatim | ear, 2026-08-10; moduleinfo declares vst2 compat |
| Ghz Tupe 3 | verbatim | ear, 2026-08-10 |
| Prophet V3 | verbatim | ear, 2026-08-10 |
| Serato Sample | verbatim | ear, 2026-08-10 |
| FabFilter Volcano 3 | reframe | hosted, 2026-08-13 |
| FabFilter Pro-Q 3 | reframe | hosted, 2026-08-13 |
| FabFilter Saturn 2 | reframe | hosted, 2026-08-13 |
| FabFilter Timeless 3 | reframe | ear and hosted, 2026-08-13 |
| FabFilter Pro-C 2 | re-encode | hosted, 2026-08-13 |
| FabFilter Pro-L 2 | re-encode | hosted, 2026-08-13 |
| FabFilter Pro-R | re-encode | hosted, 2026-08-13 |
| FabFilter Volcano 2 | re-encode | hosted, 2026-08-13 |
| FabFilter Pro-Q | re-encode | ear and hosted, 2026-08-13 |
| FabFilter Pro-Q.64 | re-encode | ear and hosted, 2026-08-13 |
| FabFilter Pro-Q 2 x64 | re-encode | hosted, 2026-08-13 |
| FabFilter Timeless 2 | re-encode | hosted, 2026-08-13 |
| FabFilter Saturn | re-encode | hosted, 2026-08-13 |
| FabFilter Pro-C | re-encode | hosted, 2026-08-15 |
| FabFilter Pro-C.64 | re-encode | hosted, 2026-08-15 |
| iZotope Ozone 4 | re-encode | hosted, 2026-08-13 |
| Ozone 8 Elements | re-encode | hosted, 2026-08-13 |
| Ozone 9 Exciter | reframe | hosted, 2026-08-13 |
| kHs Distortion | reframe | ear, 2026-08-10 |
| kHs Filter | reframe | ear, 2026-08-10 |
| kHs Stereo | reframe | ear, 2026-08-15 |

This table is `MEASURED_STATE` in the `state` package, row for row: `test_state` reads
both and fails if they disagree about a plugin, a rung, its evidence or its
date. A row carries one date, the day its rung was last measured — Timeless 3
was heard on 2026-08-10 and re-measured in the rig on 2026-08-13, and the rig is
what changed its rung. The two Pro-Q 1 rows read the same way: the converted set
was heard on 2026-08-09 and the rig injected a converted patch on 2026-08-13,
and the two Pro-C 1 rows the same way again. They are two spellings of one
plugin, and both are listed because `.64` is a jBridged 32-bit build that Live
12 will not load at all — re-encoding it is the only way that patch comes back.
kHs Stereo's class id was probed from the binary on 2026-08-13, which is not
what its row records: the date on a row is the day its rung was measured, and
that was the listen on 2026-08-15. Roughly a hundred more plugins have
structurally valid translations awaiting a listen; they stay out of this table
until a human has heard them or the rig has asked the plugin.

Pro-Q 1 is also the only row whose rung is re-encode and whose evidence is
predictive. The rung says the bytes cannot cross on their own; the ear says the
transform that carries them across was heard to work.

Two rows changed rather than appeared, and both used to say verbatim. The
FabFilter reframe is why: a VST2 chunk is the processor's state followed by the
editor's, and a VST3 `ProcessorState` is the first half plus a twelve byte
`FFpr` trailer. Copying the chunk whole still reaches the DSP — the device
sounds right, which is exactly how it passed a listen — while the edit
controller never sees the patch and every parameter reads as its default.
`state: fabfilter` cuts at the editor magic and appends the trailer; the result
is byte for byte a corpus `ProcessorState` of the same patch. `state: izotope`
is the same job for iZotope, whose chunk is a length, the VST3 state, and the
preset name.

## Measured migration verdicts

Every pair the host rig asked about, 2026-08-13. Two to ten real patches per
pair, harvested out of the user's sets; a pair is only ACCEPTED when the target
handed the injected bytes back unchanged.

| Pair | Verdict | What the plugin did |
| --- | --- | --- |
| Volcano 2 → Volcano 3 | REJECTED | 6 patches, 0/901 parameters, render delta 0.000000 |
| Pro-Q 1 → Pro-Q 3 | REJECTED | 6 patches, 0/387 parameters, render delta 0.000000; re-encoded instead |
| Pro-Q 2 → Pro-Q 3 | REJECTED | 7 patches, 0/387 parameters, render delta 0.000000 |
| Timeless 2 → Timeless 3 | REJECTED | 6 patches, 0/1012 parameters, render delta 0.234 against a 0.232 LFO floor |
| Saturn 1 → Saturn 2 | REJECTED | 10 patches, 0/956 parameters, render delta at the 0.000006 floor |
| Pro-C 1 → Pro-C 2 | REJECTED | 10 patches read as a parameter array, never echoed; a Pro-Q 1 patch, a Saturn 1 patch and a random body give the identical 0.168140 render; re-encoded instead |
| Ozone 4 → Ozone 9 | REJECTED | 3 patches, every framing, 0/646 parameters, render delta 0.000000 |
| Ozone 8 Elements → Ozone 9 | REJECTED | 3 patches, every framing, 0/646 parameters, render delta 0.000000 |
| Trash 1 → Trash 2 | UNTESTABLE | Trash 2 refuses its own `ProcessorState`; the rig cannot reach it |
| Stutter Edit 1 → Stutter Edit 2 | UNTESTABLE | Stutter Edit 2 echoes state back but exposes 2 parameters and passes audio through untriggered |

The same-product controls that make those verdicts worth reading: 22 pairs where
both sides are one plugin. Sixteen were accepted, and the six that were not are
each a fact about the rig rather than the mapping — SPAN and Ozone 9 Exciter
because an analyser changes no audio and shows almost no parameters, Trash 2 and
Stutter Edit 2 because the rig cannot reach them, and one Pro-Q 3 and one
ValhallaShimmer donor because the patch a set stored *was* the plugin's default.
The Pro-Q 3 default donor echoed its bytes back exactly, which is how a default
patch is told apart from a refusal.

## Open

* AU container pair: unimplemented (identity reading already works).
* Re-encode rung: twelve plugins sit on it and three transforms are written,
  for Pro-Q 1, Pro-C 2 and Pro-C 1. The rig has derived tables for four more —
  Pro-L 2, Pro-MB, Pro-DS, Pro-G, Micro — and Pro-L 2 is verified end to end;
  promoting them is copying a JSON file and registering a name. Pro-G needs its
  one contested slot decided first.
* Pro-Q 2 → Pro-Q 3 and Saturn 1 → Saturn 2 cannot be built the Pro-C 1 way, and
  the corpus is why. Swept 2026-08-15: 183 Pro-Q 2 devices and 86 Saturn 1
  devices, and **not one of either binds a name to a parameter index** — every
  `ParameterList` record reads `ParameterId` -1. What survives is a scrap of
  loose names with no indices to hang them on: 21 distinct labels over 4 list
  slots for Pro-Q 2, 11 over 2 for Saturn 1, against the 191 and 899 parameters
  their banks hold. Both banks are uniform and readable — 792 bytes and 3,624
  bytes, every device — so the patches are all still there; what is missing is
  the map. Getting either would take the plugin itself: a 32-bit VST2 host that
  can enumerate parameters, which is a different rig from the one this project
  has.
* Nothing on the re-encode rung has been heard yet except Pro-Q 1. The rig says
  the plugin reads a converted patch back unchanged, which is not the same as
  the patch sounding like it did.
* `ControllerState`: a config entry names one now, as `controller:`, and the
  four measured shapes are registered under names. Pro-C 1 → Pro-C 2 is the
  first entry to use it — `controller: fabfilter-fabf`, the twelve constant
  `FFed` bytes every FabF-generation device in the corpus writes. No seed-table
  row declares one yet, so every other translated device still gets the empty
  element until its entry says otherwise.
* Vendor-compat declarations: only some vendors ship them; the undeclared
  majority needs per-vendor listening evidence before it can be called
  predictable.
* The measured family reframes — soothe2/Rift's JUCE trailer, the `VstW`
  envelope, Diva's length prefix — are written in `state/families.py` and no
  `state:` name reaches them yet, because enabling a conversion stays a
  conscious act. Two of them go one way only: the JUCE trailer and the `VstW`
  envelope are stripped, and writing either back needs bytes no measurement
  here holds.
* SPAN's older bank layout is silent data loss on load — 337 real devices.
  Nothing warns about it yet; detection of that layout would let repair or a
  checker say so.
* The rig judges by readback, parameters and render. A plugin that takes a patch
  and sounds wrong still passes it, which is why `hosted` does not make a
  conversion predictable.

## Aside: the other set-global id space

Not a plugin-transform fact, but adjacent -- `xml_edit.py` renumbers ids in
the same `.als` document this model translates plugins inside. The
set-global pointee-id owner space is `Pointee`, `*AutomationTarget`,
`*ModulationTarget`, `ControllerTargets.N` -- measured via a Live 12
"non-unique Pointee IDs" refusal of a set whose copies kept template ids;
renumbering the duplicates made Live accept it.
