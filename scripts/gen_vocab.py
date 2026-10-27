"""Measure Live's XML vocabulary across a corpus of sets; write the settings enums and what was measured.

Run from the abletoolz checkout::

    uv run python scripts/gen_vocab.py --corpus DIR [--skip DIR ...]        # writes the modules and data
    uv run python scripts/gen_vocab.py --corpus DIR --sample 20 --dry-run  # time a 20-set sample

Every .als under ``--corpus`` is scanned except folders named ``backup`` or
``abletoolz_backup`` and the ``--skip`` folders, plus the version skeletons in
test/version_fixtures.

Two kinds of object are measured. Devices: every element that is a direct child
of a ``Devices`` list, at any depth (racks nest chains), one settings enum per
native device tag. And the objects a set is built from, one settings enum each:
``Track`` (every track element, the main track included), ``Mixer`` (each
track's ``DeviceChain/Mixer``), ``Clip`` (every ``MidiClip`` and ``AudioClip``)
and ``Transport`` (``LiveSet/Transport``).

Inside an object the walk records:

* an automatable parameter: an element with a ``Manual`` child; its path is
  recorded relative to the object element and the walk does not descend into it;
* a plain setting: a leaf with a ``Value`` attribute outside any parameter.

Bookkeeping subtrees (pointee/automation ids, and for devices view state and
sample references) and nested content (devices inside a track, notes inside a
clip, clips inside a track) are not walked as settings.

Versions are measured per setting: the Live versions it was seen in, and every
spelling Live used for it. A setting Live renamed is one member whose value is
the newest spelling; the older spellings go to its ``older`` record with the
version each one stops at. Renames are found two ways: a tag rename declared in
``RENAMES`` below and checked against the corpus (the run stops if the corpus
contradicts it), and two paths that read the same word for word once Live's
separators are ignored (``SideChainEq/Gain`` became ``SideChainEq_Gain``).

The same pass measures each release's document structure for
``schema/data/structure.json``: the root attributes it wrote, and for every tag the
children and attributes it carries, with the first release each was seen in
and which ones every element of the tag carries per schema.

The raw census goes to ``--json``. The settings enums go to ``--devices-out`` and
``--objects-out`` as Python; everything measured about them, and the document
structure, goes to ``settings.json`` and ``structure.json`` in ``--data-dir``. Nothing
holds a set name or a path; structure.json holds the most common value of an
attribute some schema always writes.
"""

from __future__ import annotations

import argparse
import enum
import gzip
import importlib.util
import json
import multiprocessing
import random
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zlib
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent

type Version = tuple[int, int, int]
type Range = tuple[Version, Version]


def _load_schema_module(name: str) -> ModuleType:
    """``abletoolz.schema.<name>``, loaded from its file.

    versioning.py and live_schemas.py import nothing from the package, so loading
    them alone keeps the census independent of whatever else in the checkout is
    mid-change.
    """
    spec = importlib.util.spec_from_file_location(f"_census_{name}", REPO_ROOT / "abletoolz" / "schema" / f"{name}.py")
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load abletoolz/schema/{name}.py")
    module = importlib.util.module_from_spec(spec)
    # A dataclass looks its module up by name while it is built.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


parse_creator: Callable[[str], Version] = _load_schema_module("versioning").parse_creator
live_schemas = _load_schema_module("live_schemas").live_schemas

# Live's own backup folders and abletoolz's; the state survey skips the same.
SKIP_DIR_NAMES = {"abletoolz_backup", "backup"}
# Sets a third-party generator wrote or rewrote ("jukeblocks rearranged",
# "jukeblocks_downgraded"). Their Creator names a Live version their content does
# not match -- ColorIndex on Live 11.2 tracks, Color on Live 10.1 clips -- so they
# testify to the tool, not to Live, and a census of what Live writes leaves them out.
# The name over-selects slightly (a few were re-saved by Live); losing those costs nothing.
GENERATED_NAME = re.compile(r"jukeblocks|jukebox", re.IGNORECASE)
FIXTURE_GLOB = "test/version_fixtures/skeletons/*.als"
DEVICES_OUT = REPO_ROOT / "abletoolz" / "schema" / "devices.py"
OBJECTS_OUT = REPO_ROOT / "abletoolz" / "schema" / "objects.py"
DATA_DIR = REPO_ROOT / "abletoolz" / "schema" / "data"
PRE_82_MAGIC = b"\xab\x1e"
ZERO: Version = (0, 0, 0)

# Devices whose settings live in a hosted plugin's or Max patch's own state.
HOSTED_TAGS = frozenset(
    {"PluginDevice", "AuPluginDevice", "MxDeviceAudioEffect", "MxDeviceInstrument", "MxDeviceMidiEffect"}
)

# Tags Live renamed, by old spelling: the new spelling and the first version that
# writes it. The census checks each against the corpus and stops if a set
# disagrees. Renames that only change separators are found without a listing.
RENAMES: dict[str, tuple[str, Version]] = {
    # Live 11.0 renamed ColorIndex to Color (11.0.0 itself already writes Color).
    "ColorIndex": ("Color", (11, 0, 0)),
    # Live 12 fixed the historical 'Sesstion' typo.
    "ViewStateSesstionTrackWidth": ("ViewStateSessionTrackWidth", (12, 0, 0)),
    # Live 12 renamed master/slave terms: same values on both sides.
    "IsSongTempoMaster": ("IsSongTempoLeader", (12, 0, 0)),
    "ReWireSlaveMidiTargetId": ("ReWireDeviceMidiTargetId", (12, 0, 0)),
    # Live 11 added "InDocument" to the selection flags.
    "IsContentSelected": ("IsContentSelectedInDocument", (11, 0, 0)),
}

# Settings Live moved within an object, by old path: the new path and the first
# version that writes it. Checked against the corpus like RENAMES.
MOVES: dict[str, dict[str, tuple[str, Version]]] = {
    # Live 11 gathered a clip's follow action settings under FollowAction.
    "Clip": {
        old: (f"FollowAction/{old}", (11, 0, 0))
        for old in ("FollowActionA", "FollowActionB", "FollowChanceA", "FollowChanceB", "FollowTime")
    },
}

# The main track's element was MasterTrack until Live 12; both are one owner.
OWNER_SPELLINGS: dict[str, str] = {"MasterTrack": "MainTrack"}

# Schema class names of an owner tag the census counts under its newer spelling.
OWNER_CLASSES: dict[str, tuple[str, ...]] = {
    new: tuple(old for old, newer in OWNER_SPELLINGS.items() if newer == new) for new in set(OWNER_SPELLINGS.values())
}

# Settings classes whose element sits inside its owner rather than being it.
INSIDE: dict[str, str] = {"Mixer": "DeviceChain/Mixer"}

# Ids and automation plumbing: never a setting a user changes, in any object.
BOOKKEEPING_TAGS = frozenset(
    {
        "LomId",
        "LomIdView",
        "Pointee",
        "AutomationTarget",
        "ModulationTarget",
        "MidiControllerRange",
        "ArrangerAutomation",
        "ControllerTargets",
        "ViewData",
        "LockId",
        "LockSeal",
    }
)

# Devices: bookkeeping, view and editor state, and nested chains (which hold
# other devices, walked on their own as devices).
DEVICE_SKIP_TAGS = BOOKKEEPING_TAGS | frozenset(
    {
        "MpePitchBendUsesTuning",
        # view and editor state
        "LastSelectedTimeableIndex",
        "LastSelectedClipEnvelopeIndex",
        "IsExpanded",
        "BreakoutIsExpanded",
        "IsFolded",
        "ShouldShowPresetName",
        "SelectedBranch",
        "SelectedDocumentView",
        "ViewStateEditorCenter",
        # presets, names, scripts
        "LastPresetRef",
        "LockedScripts",
        "SourceContext",
        "UserName",
        "Annotation",
        "OverwriteProtectionNumber",
        # sample file references
        "SampleRef",
        "FileRef",
        # other devices
        "Devices",
        "DeviceChain",
        "Branches",
        "ReturnBranches",
        # editor and rack view state
        "ViewSettings",
        "SpectrumAnalyzer",
        "CurrentOverlay",
        "CurrentTab",
        "SelectedPage",
        "SelectedBand",
        "SelectedFinger",
        "SelectedSampleChannel",
        "SelectedModulationSourceVisualization",
        "SelectedOscillatorBreakout",
        "VisualizationMode",
        "VisibleDisplay",
        "ViewMode",
        "EditMode",
        "ActiveEditMode",
        "ShowLfo",
        "ShowFilter",
        "ShowBranchesInSessionMixer",
        "AmpEnvelopeDisplayMode",
        "Envelope2DisplayMode",
        "Envelope3DisplayMode",
        "BranchesSplitterProportion",
        "ViewsToRestoreWhenUnfolding",
        "IsAutoSelectEnabled",
        "GridIntervalPixel",
        "Selection",
        "MacroAnnotations",
        # routing: Target is the route, these two are Live's cached labels for it
        "LowerDisplayString",
        "UpperDisplayString",
        # counters Live maintains itself
        "ModulationSourceCount",
        "ExportCounter",
        "AutogeneratedNameIndex",
        # stored rack snapshots: macro values per variation, not the rack's settings
        "MacroVariations",
    }
)
# Device view state spelled by pattern: scroll offsets and panel visibility flags.
DEVICE_SKIP_PATTERN = re.compile(r"ScrollPos|Visible$")

# A track's settings stop where other objects start: its mixer, its devices, its
# clips and the automation it carries.
TRACK_SKIP_TAGS = BOOKKEEPING_TAGS | frozenset(
    {
        "Mixer",
        "Devices",
        "SignalModulations",
        "ClipSlotList",
        "ClipTimeable",
        "Sample",
        "FreezeSequencer",
        "MidiControllers",
        "TakeLanes",
        "AutomationEnvelopes",
        "ClipEnvelopeChooserViewState",
        "LastPresetRef",
    }
)
MIXER_SKIP_TAGS = BOOKKEEPING_TAGS | frozenset({"MidiControllers"})
# A clip's settings: everything but its content (notes, envelopes) and the file it plays.
CLIP_SKIP_TAGS = BOOKKEEPING_TAGS | frozenset({"Notes", "Envelopes", "SampleRef", "FileRef", "MidiControllers"})
TRANSPORT_SKIP_TAGS = BOOKKEEPING_TAGS

NUMBERED = re.compile(r"^(?P<stem>.+)\.(?P<index>\d+)$")
INT_RE = re.compile(r"^-?\d+$")
PLACEHOLDER_RE = re.compile(r"\{index\d*\}")
DISTINCT_CAP = 32

# Live's display names where the XML tag says something else. Tags not listed
# fall back to UPPER_SNAKE of the tag.
DISPLAY_NAMES: dict[str, str] = {
    "OriginalSimpler": "SIMPLER",
    "MultiSampler": "SAMPLER",
    "InstrumentGroupDevice": "INSTRUMENT_RACK",
    "AudioEffectGroupDevice": "AUDIO_EFFECT_RACK",
    "MidiEffectGroupDevice": "MIDI_EFFECT_RACK",
    "DrumGroupDevice": "DRUM_RACK",
    "Eq8": "EQ_EIGHT",
    "Compressor2": "COMPRESSOR",
    "StereoGain": "UTILITY",
    "FilterEQ3": "EQ_THREE",
    "InstrumentVector": "WAVETABLE",
    "UltraAnalog": "ANALOG",
    "LoungeLizard": "ELECTRIC",
    "StringStudio": "TENSION",
    "ProxyInstrumentDevice": "EXTERNAL_INSTRUMENT",
    "Hybrid": "HYBRID_REVERB",
    "Tube": "DYNAMIC_TUBE",
    "Vinyl": "VINYL_DISTORTION",
    "CrossDelay": "SIMPLE_DELAY",
    "Chorus2": "CHORUS_ENSEMBLE",
    "PhaserNew": "PHASER_FLANGER",
    "Spectral": "SPECTRAL_TIME",
    "Transmute": "SPECTRAL_RESONATOR",
    "MidiPitcher": "PITCH",
    "MidiVelocity": "VELOCITY",
    "MidiChord": "CHORD",
    "MidiRandom": "RANDOM",
    "MidiScale": "SCALE",
    "MidiArpeggiator": "ARPEGGIATOR",
    "MidiNoteLength": "NOTE_LENGTH",
    "PluginDevice": "PLUGIN",
    "AuPluginDevice": "AU_PLUGIN",
    "MxDeviceAudioEffect": "MAX_AUDIO_EFFECT",
    "MxDeviceInstrument": "MAX_INSTRUMENT",
    "MxDeviceMidiEffect": "MAX_MIDI_EFFECT",
}

# The set objects, in the order the module lists them.
OBJECT_DOCS: dict[str, str] = {
    "Track": "Settings of a track element, any type, the main track included; relative to the track element.",
    "Mixer": "Settings of a track's mixer; relative to ``DeviceChain/Mixer`` of the track.",
    "Clip": "Settings of a ``MidiClip`` or ``AudioClip``; relative to the clip element.",
    "Transport": "Settings of the set's transport; relative to ``LiveSet/Transport``.",
}


class ValueKind(enum.StrEnum):
    """What the observed ``Value`` strings of a setting parse as."""

    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    STR = "str"


def classify(value: str) -> ValueKind:
    """Return the narrowest kind a single value string fits."""
    if value in ("true", "false"):
        return ValueKind.BOOL
    if INT_RE.match(value):
        return ValueKind.INT
    try:
        float(value)
    except ValueError:
        return ValueKind.STR
    return ValueKind.FLOAT


def resolve_kind(kinds: set[ValueKind]) -> ValueKind:
    """Join every kind seen for one setting into the one kind that holds them all."""
    if kinds == {ValueKind.BOOL}:
        return ValueKind.BOOL
    if kinds == {ValueKind.INT}:
        return ValueKind.INT
    if kinds and kinds <= {ValueKind.INT, ValueKind.FLOAT}:
        return ValueKind.FLOAT
    return ValueKind.STR


def version_text(version: Version) -> str:
    return ".".join(str(part) for part in version)


def widen_range(mine: Range | None, theirs: Range | None) -> Range | None:
    if mine is None:
        return theirs
    if theirs is None:
        return mine
    return min(mine[0], theirs[0]), max(mine[1], theirs[1])


@dataclass(slots=True)
class SettingTally:
    """One setting path under one object.

    A few settings change shape across versions (a plain ``Value`` leaf in one,
    a ``Manual`` parameter in another), so each form keeps its own version range;
    and a few change spelling, so each spelling keeps one too.
    """

    count: int = 0
    parameter_versions: Range | None = None
    value_versions: Range | None = None
    kinds: set[ValueKind] = field(default_factory=set)
    min_version: Version = (99, 0, 0)
    max_version: Version = (0, 0, 0)
    # Every version it was seen in, to check against what that version's schema declares.
    versions: set[Version] = field(default_factory=set)
    # Highest number seen for each placeholder in the path, outermost first.
    max_indices: list[int] = field(default_factory=list)
    # A bounded sample of distinct values, to show how much a setting varies.
    values: set[str] = field(default_factory=set)
    # Each path Live wrote for this setting, and the versions it wrote it in.
    spellings: dict[str, Range] = field(default_factory=dict)
    # The element tags this setting was seen under (track types, clip types).
    owners: set[str] = field(default_factory=set)
    # Set when two spellings Live used were merged into this one setting.
    renamed: bool = False

    @property
    def is_parameter(self) -> bool:
        """Whether the newest form seen is an automatable parameter."""
        if self.parameter_versions is None:
            return False
        return self.value_versions is None or self.parameter_versions[1] >= self.value_versions[1]

    @property
    def changed_shape(self) -> bool:
        return self.parameter_versions is not None and self.value_versions is not None

    def add(
        self, value: str, version: Version, indices: tuple[int, ...], is_parameter: bool, spelled: str, owner: str
    ) -> None:
        self.count += 1
        if is_parameter:
            self.parameter_versions = widen_range(self.parameter_versions, (version, version))
        else:
            self.value_versions = widen_range(self.value_versions, (version, version))
        self.kinds.add(classify(value))
        self.min_version = min(self.min_version, version)
        self.max_version = max(self.max_version, version)
        self.versions.add(version)
        self._widen(list(indices))
        if len(self.values) < DISTINCT_CAP:
            self.values.add(value)
        self.spellings[spelled] = widen_range(self.spellings.get(spelled), (version, version)) or (version, version)
        self.owners.add(owner)

    def _widen(self, indices: list[int]) -> None:
        if not self.max_indices:
            self.max_indices = indices
        elif len(indices) == len(self.max_indices):
            self.max_indices = [max(mine, theirs) for mine, theirs in zip(self.max_indices, indices, strict=True)]
        elif indices:
            raise SystemExit(f"one setting holds {len(self.max_indices)} and {len(indices)} list positions")

    def merge(self, other: SettingTally) -> None:
        self.count += other.count
        self.parameter_versions = widen_range(self.parameter_versions, other.parameter_versions)
        self.value_versions = widen_range(self.value_versions, other.value_versions)
        self.kinds |= other.kinds
        self.min_version = min(self.min_version, other.min_version)
        self.max_version = max(self.max_version, other.max_version)
        self.versions |= other.versions
        self._widen(other.max_indices)
        for value in other.values:
            if len(self.values) >= DISTINCT_CAP:
                break
            self.values.add(value)
        for spelled, seen in other.spellings.items():
            self.spellings[spelled] = widen_range(self.spellings.get(spelled), seen) or seen
        self.owners |= other.owners
        self.renamed = self.renamed or other.renamed


@dataclass(slots=True)
class ObjectTally:
    """One device tag, or one set object, across the corpus."""

    count: int = 0
    set_count: int = 0
    min_version: Version = (99, 0, 0)
    max_version: Version = (0, 0, 0)
    settings: dict[str, SettingTally] = field(default_factory=dict)
    # Every version each owner tag was seen in: the evidence a setting's absence rests on.
    owner_versions: dict[str, set[Version]] = field(default_factory=dict)
    # Templated paths of Id-carrying elements seen with same-tag siblings: real lists.
    id_lists: set[str] = field(default_factory=set)
    # Filled after the scan: list members written without their Id (pre-9.7), not emitted.
    id_less: dict[str, SettingTally] = field(default_factory=dict)

    def see(self, version: Version, owner: str) -> None:
        self.count += 1
        self.min_version = min(self.min_version, version)
        self.max_version = max(self.max_version, version)
        self.owner_versions.setdefault(owner, set()).add(version)

    def merge(self, other: ObjectTally) -> None:
        self.count += other.count
        self.set_count += other.set_count
        self.min_version = min(self.min_version, other.min_version)
        self.max_version = max(self.max_version, other.max_version)
        self.id_lists |= other.id_lists
        for owner, versions in other.owner_versions.items():
            self.owner_versions.setdefault(owner, set()).update(versions)
        for path, tally in other.settings.items():
            mine = self.settings.get(path)
            if mine is None:
                self.settings[path] = tally
            else:
                mine.merge(tally)


type Pair = tuple[str, str]
type Header = tuple[tuple[str, str], ...]

# Attribute orders kept per tag; one order is the norm, a handful covers every variant.
ORDER_CAP = 4


@dataclass(slots=True)
class GrammarTally:
    """Which children and attributes each element carries, in the sets of one Live version."""

    instances: Counter[str] = field(default_factory=Counter)
    # (parent, child): parents holding at least one such child.
    children: Counter[Pair] = field(default_factory=Counter)
    # (tag, attribute): elements carrying the attribute.
    attributes: Counter[Pair] = field(default_factory=Counter)
    values: dict[Pair, Counter[str]] = field(default_factory=dict)
    orders: dict[str, Counter[tuple[str, ...]]] = field(default_factory=dict)
    headers: Counter[Header] = field(default_factory=Counter)
    # (device tag, kind): one vote per device found, from its preset's browser folder,
    # the chain it sits in, or its place beside the chain's instrument.
    kinds: Counter[Pair] = field(default_factory=Counter)
    sets: int = 0

    def merge(self, other: GrammarTally) -> None:
        self.instances.update(other.instances)
        self.children.update(other.children)
        self.attributes.update(other.attributes)
        for pair, values in other.values.items():
            _capped_update(self.values.setdefault(pair, Counter()), values, DISTINCT_CAP)
        for tag, orders in other.orders.items():
            _capped_update(self.orders.setdefault(tag, Counter()), orders, ORDER_CAP)
        self.headers.update(other.headers)
        self.kinds.update(other.kinds)
        self.sets += other.sets


def _capped_update[K](mine: Counter[K], theirs: Counter[K], cap: int) -> None:
    for key, count in theirs.items():
        if key in mine or len(mine) < cap:
            mine[key] += count


def grammar_tag(tag: str) -> str:
    """A tag as the grammar counts it: numbered tags (``ControllerTargets.12``) fold into one."""
    match = NUMBERED.match(tag)
    return tag if match is None else f"{match['stem']}.{{n}}"


# What a device chain can hold, by the element that owns it. A MIDI track's chain and
# an instrument or drum rack's chain hold all three kinds in Live's order: MIDI
# effects, the instrument, audio effects.
CHAIN_KINDS = {
    "AudioTrack": "audio_effect",
    "ReturnTrack": "audio_effect",
    "GroupTrack": "audio_effect",
    "MasterTrack": "audio_effect",
    "MainTrack": "audio_effect",
    "AudioEffectBranch": "audio_effect",
    "MidiEffectBranch": "midi_effect",
    "MidiTrack": "mixed",
    "InstrumentBranch": "mixed",
    "DrumBranch": "mixed",
}


PRESET_FOLDER = re.compile(r"^Devices/(Audio Effects|Instruments|MIDI Effects)/", re.IGNORECASE)
FOLDER_KINDS = {"audio effects": "audio_effect", "instruments": "instrument", "midi effects": "midi_effect"}
INSTRUMENT_TAGS = frozenset({"InstrumentGroupDevice", "DrumGroupDevice"})


def preset_kind(device: ET.Element) -> str | None:
    """The browser folder of the preset a device was last loaded from, when Live recorded one."""
    path = device.find("LastPresetRef//RelativePath")
    match = None if path is None else PRESET_FOLDER.match(path.get("Value", ""))
    return None if match is None else FOLDER_KINDS[match[1].lower()]


def chain_votes(devices: list[ET.Element], kind: str) -> list[str | None]:
    """Each device's kind as its chain shows it: by the chain alone, or by its place beside the instrument."""
    if kind != "mixed":
        return [kind] * len(devices)
    own = [preset_kind(device) or ("instrument" if device.tag in INSTRUMENT_TAGS else None) for device in devices]
    if "instrument" not in own:
        return own
    anchor = own.index("instrument")
    return [own[i] or ("midi_effect" if i < anchor else "audio_effect") for i in range(len(devices))]


def chain_kind(devices: ET.Element, parents: dict[ET.Element, ET.Element]) -> str | None:
    owner = parents.get(devices)
    while owner is not None and owner.tag not in CHAIN_KINDS:
        owner = parents.get(owner)
    return None if owner is None else CHAIN_KINDS[owner.tag]


def census_grammar(root: ET.Element) -> GrammarTally:
    """Every element of one set: its children's tags, its attributes, their values and order."""
    grammar = GrammarTally(sets=1)
    grammar.headers[tuple(root.attrib.items())] += 1
    for element in root.iter():
        tag = grammar_tag(element.tag)
        grammar.instances[tag] += 1
        for child_tag in {grammar_tag(child.tag) for child in element}:
            grammar.children[tag, child_tag] += 1
        if not element.attrib:
            continue
        for name, value in element.attrib.items():
            grammar.attributes[tag, name] += 1
            _capped_update(grammar.values.setdefault((tag, name), Counter()), Counter({value: 1}), DISTINCT_CAP)
        _capped_update(grammar.orders.setdefault(tag, Counter()), Counter({tuple(element.attrib): 1}), ORDER_CAP)
    parents = {child: parent for parent in root.iter() for child in parent}
    for devices in root.iter("Devices"):
        kind = chain_kind(devices, parents)
        if kind is not None:
            chain = list(devices)
            for device, vote in zip(chain, chain_votes(chain, kind), strict=True):
                if vote is not None:
                    grammar.kinds[device.tag, vote] += 1
    return grammar


@dataclass(slots=True)
class SetResult:
    """What one set contributed, or why it was skipped."""

    version: Version | None = None
    devices: dict[str, ObjectTally] = field(default_factory=dict)
    objects: dict[str, ObjectTally] = field(default_factory=dict)
    grammar: GrammarTally | None = None
    skip_reason: str | None = None


@dataclass(slots=True)
class Census:
    """The merged corpus census."""

    sets_scanned: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    versions: Counter[str] = field(default_factory=Counter)
    devices: dict[str, ObjectTally] = field(default_factory=dict)
    objects: dict[str, ObjectTally] = field(default_factory=dict)
    # The document structure, per Live version.
    grammar: dict[Version, GrammarTally] = field(default_factory=dict)

    def merge(self, result: SetResult) -> None:
        if result.skip_reason is not None:
            self.skipped[result.skip_reason] += 1
            return
        assert result.version is not None
        self.sets_scanned += 1
        self.versions[f"{result.version[0]}.{result.version[1]}"] += 1
        if result.grammar is not None:
            if result.version in self.grammar:
                self.grammar[result.version].merge(result.grammar)
            else:
                self.grammar[result.version] = result.grammar
        for mine, theirs in ((self.devices, result.devices), (self.objects, result.objects)):
            for key, tally in theirs.items():
                if key in mine:
                    mine[key].merge(tally)
                else:
                    mine[key] = tally


def iter_corpus(root: Path, skip: list[Path]) -> list[Path]:
    """Every .als under ``root``, leaving out backup folders, the ``skip`` folders and generated sets."""
    skip_subtrees = {path.resolve() for path in skip}
    found: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.resolve() in skip_subtrees or entry.name.lower() in SKIP_DIR_NAMES:
                    continue
                stack.append(entry)
            elif entry.suffix.lower() == ".als" and GENERATED_NAME.search(entry.name) is None:
                found.append(entry)
    return sorted(found)


def live_written_fixtures() -> list[Path]:
    """The version skeletons, less any whose recorded source a generator wrote."""
    skeletons = sorted(REPO_ROOT.glob(FIXTURE_GLOB))
    expected_path = REPO_ROOT / "test" / "version_fixtures" / "skeletons" / "expected.json"
    expected = json.loads(expected_path.read_text(encoding="utf-8")) if expected_path.exists() else {}
    kept: list[Path] = []
    for skeleton in skeletons:
        source = str(expected.get(skeleton.stem, {}).get("source", ""))
        if GENERATED_NAME.search(source) is None:
            kept.append(skeleton)
    return kept


def placeholder_for(depth: int) -> str:
    """``{index}`` for the outermost list in a path, ``{index2}`` inside it, and so on."""
    return "{index}" if depth == 0 else f"{{index{depth + 1}}}"


def bare_tag(tag: str) -> str:
    """``MacroDisplayNames.3`` -> ``MacroDisplayNames``."""
    numbered = NUMBERED.match(tag)
    return tag if numbered is None else numbered["stem"]


def device_skips(tag: str) -> bool:
    bare = bare_tag(tag)
    return bare in DEVICE_SKIP_TAGS or DEVICE_SKIP_PATTERN.search(bare) is not None


# Set objects carry remote bindings (``KeyMidi``, ``StartKeyMidi``, ...) and
# modulation plumbing (``FluxModulationTarget``) the way devices carry ids.
OBJECT_SKIP_PATTERN = re.compile(r"KeyMidi|ModulationTarget$")


def skips_by(tags: frozenset[str]) -> Callable[[str], bool]:
    return lambda tag: bare_tag(tag) in tags or OBJECT_SKIP_PATTERN.search(bare_tag(tag)) is not None


def canonical_segment(segment: str) -> str:
    """A path segment in its newest spelling: ``ColorIndex`` -> ``Color``."""
    for old, (new, _) in RENAMES.items():
        if segment == old or segment.startswith((f"{old}.", f"{old}[")):
            return new + segment[len(old) :]
    return segment


def list_segment(child: ET.Element, depth: int) -> tuple[str, int] | None:
    """Template a list member, or None for a plain element.

    Live spells lists two ways: numbered tags (``Bands.3``) and same-tag
    siblings told apart by an ``Id`` attribute (``MultiSamplePart Id="3"``).
    The second is templated even when a list holds one member, so a set with
    one sample part and a set with forty name the same path.
    """
    placeholder = placeholder_for(depth)
    numbered = NUMBERED.match(child.tag)
    if numbered is not None:
        return f"{numbered['stem']}.{placeholder}", int(numbered["index"])
    element_id = child.get("Id")
    if element_id is not None and INT_RE.match(element_id):
        return f"{child.tag}[@Id='{placeholder}']", int(element_id)
    return None


def census_element(
    root: ET.Element, version: Version, tally: ObjectTally, owner: str, skipped: Callable[[str], bool]
) -> None:
    """Record every parameter and plain setting under one object element."""
    stack: list[tuple[ET.Element, str, str, tuple[int, ...]]] = [(root, "", "", ())]
    while stack:
        element, prefix, spelled_prefix, indices = stack.pop()
        id_tags = Counter(child.tag for child in element if child.get("Id") is not None)
        for tag, n in id_tags.items():
            if n > 1:
                tally.id_lists.add(f"{prefix}{canonical_segment(tag)}[@Id='{placeholder_for(len(indices))}']")
        for child in element:
            if skipped(child.tag):
                continue
            listed = list_segment(child, len(indices))
            if listed is None:
                segment, child_indices = child.tag, indices
            else:
                segment, child_indices = listed[0], (*indices, listed[1])
            path, spelled = f"{prefix}{canonical_segment(segment)}", f"{spelled_prefix}{segment}"
            manual = child.find("Manual")
            if manual is not None:
                record(tally, path, spelled, owner, True, manual.get("Value", ""), version, child_indices)
            elif len(child) == 0:
                value = child.get("Value")
                if value is not None:
                    record(tally, path, spelled, owner, False, value, version, child_indices)
            else:
                stack.append((child, f"{path}/", f"{spelled}/", child_indices))


def record(
    tally: ObjectTally,
    path: str,
    spelled: str,
    owner: str,
    is_parameter: bool,
    value: str,
    version: Version,
    indices: tuple[int, ...],
) -> None:
    setting = tally.settings.get(path)
    if setting is None:
        setting = tally.settings[path] = SettingTally()
    setting.add(value, version, indices, is_parameter, spelled, owner)


def positional(placeholder: str) -> str:
    """``{index}`` -> ``{index_position}``: the 1-based position ElementTree's ``Tag[n]`` takes."""
    return f"{{{placeholder[1:-1]}_position}}"


def collapse_path(
    spelled: str, canonical: str, tally: ObjectTally, max_indices: list[int], *, as_spelled: bool
) -> tuple[str, list[int], bool]:
    """Rewrite one path the way ``canonical``'s list segments resolve; see :func:`collapse_singletons`.

    ``as_spelled`` says whether the result is a spelling Live wrote (a list
    written without ids is addressed by position) or the setting's name (always
    the ``[@Id=...]`` form).
    """
    segments: list[str] = []
    kept: list[int] = []
    original_prefix = ""
    position = 0
    id_less = False
    for segment, spelled_segment in zip(canonical.split("/"), spelled.split("/"), strict=True):
        id_listed = f"{original_prefix}{segment}[@Id='{placeholder_for(position)}']"
        original = original_prefix + segment
        original_prefix = original + "/"
        if PLACEHOLDER_RE.search(segment) is None:
            if id_listed in tally.id_lists:
                # A list written without ids: the same list as the Id'd one, by position.
                id_less = True
                placeholder = placeholder_for(len(kept))
                suffix = f"[{positional(placeholder)}]" if as_spelled else f"[@Id='{placeholder}']"
                segments.append(spelled_segment + suffix)
                kept.append(0)
            else:
                segments.append(spelled_segment)
            continue
        is_id = segment.endswith("']")
        if is_id and original not in tally.id_lists:
            segments.append(spelled_segment.split("[@Id=")[0])
        else:
            segments.append(PLACEHOLDER_RE.sub(placeholder_for(len(kept)), spelled_segment))
            kept.append(max_indices[position])
        position += 1
    return "/".join(segments), kept, id_less


def collapse_singletons(tally: ObjectTally) -> None:
    """Un-template ``Tag[@Id=...]`` segments never seen with a same-tag sibling.

    Live stamps an ``Id`` on some single objects too (``SimplerAuxLfo``,
    ``SimplerAuxEnvelope``); the scan templates every ``Id`` element, and only
    the ones the corpus shows repeating stay lists.

    The same list written without ids -- automation lanes and sends before Live
    10, Simpler and Sampler parts before 9.7 -- is the same setting at an older
    spelling, addressed by position (``AutomationLane[{index_position}]``), and
    joins the Id'd setting. An id-less setting with no Id'd counterpart is a
    setting only those versions had; it moves to ``id_less`` and stays out of
    the module.
    """
    collapsed: dict[str, SettingTally] = {}
    id_less_settings: list[tuple[str, SettingTally]] = []
    for path, setting in tally.settings.items():
        new_path, kept, id_less = collapse_path(path, path, tally, setting.max_indices, as_spelled=False)
        spellings: dict[str, Range] = {}
        for spelled, seen in setting.spellings.items():
            new_spelled = collapse_path(spelled, path, tally, setting.max_indices, as_spelled=True)[0]
            spellings[new_spelled] = widen_range(spellings.get(new_spelled), seen) or seen
        setting.spellings = spellings
        setting.max_indices = kept
        if id_less:
            id_less_settings.append((new_path, setting))
            continue
        mine = collapsed.get(new_path)
        if mine is None:
            collapsed[new_path] = setting
        else:
            mine.merge(setting)
    for new_path, setting in id_less_settings:
        if new_path in collapsed:
            collapsed[new_path].merge(setting)
        else:
            tally.id_less[new_path] = setting
    tally.settings = collapsed


def owner_of(element: ET.Element) -> str:
    return OWNER_SPELLINGS.get(element.tag, element.tag)


def census_objects(live_set: ET.Element, version: Version, objects: dict[str, ObjectTally]) -> None:
    """Record the tracks, their mixers, every clip and the transport of one set."""

    def tally(name: str, element: ET.Element, skipped: Callable[[str], bool]) -> None:
        mine = objects.get(name)
        if mine is None:
            mine = objects[name] = ObjectTally(set_count=1)
        mine.see(version, owner_of(element))
        census_element(element, version, mine, owner_of(element), skipped)

    tracks_element = live_set.find("Tracks")
    tracks = list(tracks_element) if tracks_element is not None else []
    for main_tag in ("MainTrack", "MasterTrack"):
        main = live_set.find(main_tag)
        if main is not None:
            tracks.append(main)
            break
    for track in tracks:
        tally("Track", track, skips_by(TRACK_SKIP_TAGS))
        mixer = track.find(INSIDE["Mixer"])
        if mixer is not None:
            mixers = objects.get("Mixer")
            if mixers is None:
                mixers = objects["Mixer"] = ObjectTally(set_count=1)
            mixers.see(version, owner_of(track))
            census_element(mixer, version, mixers, owner_of(track), skips_by(MIXER_SKIP_TAGS))
    for clip_tag in ("MidiClip", "AudioClip"):
        for clip in live_set.iter(clip_tag):
            tally("Clip", clip, skips_by(CLIP_SKIP_TAGS))
    transport = live_set.find("Transport")
    if transport is not None:
        tally("Transport", transport, skips_by(TRANSPORT_SKIP_TAGS))


def scan_set(path: Path) -> SetResult:
    """Census one set; a set that cannot be read comes back with a skip reason."""
    try:
        raw = path.read_bytes()
    except OSError:
        return SetResult(skip_reason="unreadable file")
    if raw.startswith(PRE_82_MAGIC):
        return SetResult(skip_reason="pre-8.2 container")
    if raw.startswith(b"\x1f\x8b"):
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError, zlib.error):
            return SetResult(skip_reason="corrupt gzip")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return SetResult(skip_reason="xml parse error")
    creator = root.get("Creator")
    if creator is None:
        return SetResult(skip_reason="no Creator attribute")
    try:
        version = parse_creator(creator)
    except ValueError:
        return SetResult(skip_reason="unparseable Creator")

    result = SetResult(version=version, grammar=census_grammar(root))
    for devices in root.iter("Devices"):
        for device in devices:
            tally = result.devices.get(device.tag)
            if tally is None:
                tally = result.devices[device.tag] = ObjectTally(set_count=1)
            tally.see(version, device.tag)
            if device.tag not in HOSTED_TAGS:
                census_element(device, version, tally, device.tag, device_skips)
    live_set = root.find("LiveSet")
    if live_set is not None:
        census_objects(live_set, version, result.objects)
    return result


def run_census(paths: list[Path], workers: int) -> Census:
    census = Census()
    started = time.perf_counter()
    with multiprocessing.Pool(workers) as pool:
        for done, result in enumerate(pool.imap_unordered(scan_set, paths, chunksize=2), start=1):
            census.merge(result)
            if done % 250 == 0:
                print(f"  {done}/{len(paths)} sets, {time.perf_counter() - started:.0f}s", flush=True)
    problems: list[str] = []
    for name, tally in (*census.devices.items(), *census.objects.items()):
        collapse_singletons(tally)
        problems += [f"{name}: {problem}" for problem in apply_moves(tally, MOVES.get(name, {}))]
        merge_renamed(tally)
        problems += [f"{name}: {problem}" for problem in check_spellings(tally)]
    if problems:
        raise SystemExit("spelling conflicts:\n" + "\n".join(problems))
    return census


# --------------------------------------------------------------------------- renames

# Structural wrappers that say nothing about which setting a path names.
FILLER_WORDS = frozenset({"Slot", "Value"})


def path_words(path: str) -> list[str]:
    """Segments with list syntax and wrapper words removed.

    ``Filter/Slot/Value/SimplerFilter/Freq`` -> Filter, SimplerFilter, Freq;
    ``Bands.{index}/ParameterA/Freq`` -> Bands, ParameterA, Freq.
    """
    segments = [PLACEHOLDER_RE.sub("", segment).split("[@Id=")[0].rstrip(".") for segment in path.split("/")]
    return [word for word in segments[:-1] if word not in FILLER_WORDS] + segments[-1:]


def merge_renamed(tally: ObjectTally) -> None:
    """Fold paths that read the same word for word into one setting under the newest path.

    ``SideChainEq/Gain`` (to Live 11) and ``SideChainEq_Gain`` (from 12) name one
    setting. Paths that read alike but were seen in the same versions are two
    settings, and stop the run: no name could tell them apart.
    """
    by_full: dict[str, list[str]] = {}
    for path in tally.settings:
        by_full.setdefault(upper_snake("_".join(path_words(path))), []).append(path)
    for group in by_full.values():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda path: tally.settings[path].min_version)
        for older, newer in zip(ordered, ordered[1:], strict=False):
            if tally.settings[older].max_version >= tally.settings[newer].min_version:
                raise SystemExit(f"{older!r} and {newer!r} read alike and were both seen in one version")
        newest = tally.settings[ordered[-1]]
        for path in ordered[:-1]:
            newest.merge(tally.settings.pop(path))
        newest.renamed = True


def apply_moves(tally: ObjectTally, moves: dict[str, tuple[str, Version]]) -> list[str]:
    """Fold each moved setting's old path into its new one, if the corpus agrees about when."""
    problems: list[str] = []
    for old, (new, boundary) in moves.items():
        if old not in tally.settings or new not in tally.settings:
            problems.append(f"MOVES lists {old!r} -> {new!r}, but the corpus does not hold both")
            continue
        before, after = tally.settings[old], tally.settings[new]
        if not before.max_version < boundary <= after.min_version:
            problems.append(
                f"MOVES says {old!r} became {new!r} in Live {version_text(boundary)}, but the corpus has the old "
                f"path up to {version_text(before.max_version)} and the new from {version_text(after.min_version)}"
            )
            continue
        after.merge(tally.settings.pop(old))
    return problems


def check_spellings(tally: ObjectTally) -> list[str]:
    """Every spelling of a setting has to own its own versions, and a declared rename has to hold."""
    problems: list[str] = []
    for path, setting in tally.settings.items():
        ordered = sorted(setting.spellings.items(), key=lambda item: item[1][0])
        for (older, (_, older_last)), (newer, (newer_first, _)) in zip(ordered, ordered[1:], strict=False):
            if older_last >= newer_first:
                problems.append(
                    f"{path!r}: spelled {older!r} up to Live {version_text(older_last)} and {newer!r} "
                    f"from {version_text(newer_first)}; a rename has to change in one version"
                )
            boundary = declared_boundary(newer, older)
            if boundary is not None and not older_last < boundary <= newer_first:
                problems.append(
                    f"{path!r}: RENAMES says the change came in Live {version_text(boundary)}, but the corpus "
                    f"has {older!r} up to {version_text(older_last)} and {newer!r} from {version_text(newer_first)}"
                )
    return problems


def declared_boundary(newer: str, older: str) -> Version | None:
    """The version a declared rename or move took ``older``'s spelling to ``newer``'s, if one did."""
    for moves in MOVES.values():
        if moves.get(older, ("", ZERO))[0] == newer:
            return moves[older][1]
    found: list[Version] = []
    for new_segment, old_segment in zip(newer.split("/"), older.split("/"), strict=False):
        if new_segment != old_segment and canonical_segment(old_segment) == new_segment:
            found.append(RENAMES[bare_tag(old_segment.split("[")[0])][1])
    return max(found) if found else None


def older_paths(setting: SettingTally, newest: str) -> list[tuple[Version, str]]:
    """Each spelling, newest first, with the first version that writes it; the oldest from (0, 0, 0)."""
    ordered = sorted(setting.spellings.items(), key=lambda item: item[1][0], reverse=True)
    if ordered[0][0] != newest:
        raise SystemExit(f"{newest!r}: the newest spelling seen is {ordered[0][0]!r}")
    entries: list[tuple[Version, str]] = []
    for position, (spelled, (first, _)) in enumerate(ordered):
        if position == len(ordered) - 1:
            entries.append((ZERO, spelled))
        else:
            entries.append((declared_boundary(spelled, ordered[position + 1][0]) or first, spelled))
    return entries


# --------------------------------------------------------------------------- json


def setting_json(setting: SettingTally) -> dict[str, object]:
    distinct = len(setting.values)
    return {
        "parameter": setting.is_parameter,
        "parameter_versions": [version_text(v) for v in setting.parameter_versions or ()],
        "value_versions": [version_text(v) for v in setting.value_versions or ()],
        "count": setting.count,
        "kind": resolve_kind(setting.kinds).value,
        "kinds_seen": sorted(kind.value for kind in setting.kinds),
        "min_version": version_text(setting.min_version),
        "max_version": version_text(setting.max_version),
        "max_indices": setting.max_indices,
        "spellings": {path: [version_text(v) for v in seen] for path, seen in sorted(setting.spellings.items())},
        "owners": sorted(setting.owners),
        "distinct_values": distinct if distinct < DISTINCT_CAP else f"{DISTINCT_CAP}+",
        "sample_values": sorted(setting.values)[:8],
    }


def tally_json(tag: str, tally: ObjectTally) -> dict[str, object]:
    return {
        "count": tally.count,
        "set_count": tally.set_count,
        "min_version": version_text(tally.min_version),
        "max_version": version_text(tally.max_version),
        "hosted": tag in HOSTED_TAGS,
        "owners": {owner: len(versions) for owner, versions in sorted(tally.owner_versions.items())},
        "settings": {path: setting_json(s) for path, s in sorted(tally.settings.items())},
        "id_less_settings": {path: setting_json(s) for path, s in sorted(tally.id_less.items())},
    }


def census_json(census: Census) -> dict[str, object]:
    """The raw census for inspection. Sample values can hold user text (track names): keep it local."""
    return {
        "sets_scanned": census.sets_scanned,
        "skipped": dict(census.skipped),
        "versions": dict(sorted(census.versions.items())),
        "devices": {tag: tally_json(tag, tally) for tag, tally in sorted(census.devices.items())},
        "objects": {name: tally_json(name, tally) for name, tally in sorted(census.objects.items())},
    }


# --------------------------------------------------------------------------- emit

# In a clash, only a setting at most this many words deep may keep the bare name
# (`VolumeAndPan/Volume` is Sampler.VOLUME; a sub-oscillator `Mode` four words down is not MODE).
BARE_NAME_MAX_DEPTH = 2

DEVICES_DOC = '''"""Live's native device tags and the settings inside each, measured from real sets.

GENERATED by scripts/gen_vocab.py; do not edit by hand. Regenerate from the abletoolz
checkout with ``uv run python scripts/gen_vocab.py --corpus DIR`` (it scans a local
corpus of sets; nothing about the sets themselves lands here).

``Device`` names every tag seen as a direct child of a ``Devices`` list, racks
walked into. Member names are Live's display names where the tag differs
(``MultiSampler`` is ``SAMPLER``, ``OriginalSimpler`` is ``SIMPLER``,
``StereoGain`` is ``UTILITY``). Hosted plugins and Max devices are listed but
have no settings enum: their state is the plugin's own.

Each native device has a settings ``StrEnum`` named after its ``Device`` member
(``Device.SAMPLER`` -> ``Sampler``). A value is the setting's path relative to
the device element in the newest Live version that writes it. The kind of
value a member holds and whether it is an automatable parameter (its value in
a ``Manual`` child) come from Live's own schemas; what the census measured
about it -- every earlier spelling, how often and in which versions it was
seen -- is in ``data/settings.json``. Read both through
:mod:`abletoolz.schema.settings` (``kind_of``, ``is_parameter``,
``spellings``, ``path_for``), so code names a member and never a version.

A member is named by the last path segment. When several paths end the same
way, the shallowest keeps the bare name if it sits at most two segments deep,
and the rest take the shortest path suffix that is unique (``Slot`` and
``Value`` wrappers left out), so
``Sampler.VOLUME`` is the amp volume and ``Sampler.MULTI_SAMPLE_PART_VOLUME``
the per-sample-part one. A setting Live renamed takes its full path as its name.

Lists stay one member each, with placeholders in the value: ``{index}`` for the
outermost list in a path, ``{index2}`` for a list inside it. Live spells lists
two ways and both are kept verbatim: numbered tags (``Bands.{index}``, where the
number is the position) and same-tag siblings told apart by an ``Id`` attribute
(``MultiSamplePart[@Id='{index}']``, ElementTree's predicate syntax, where the
number is that ``Id`` and need not be 0..n-1). Fill with
``member.format(index=n)`` before ``find``; ``is_templated`` tells such a
member apart so a caller can refuse an unfilled one. They are kept, not dropped,
because EQ bands, sample parts and macros are most of what those devices are.
Sets before Live 9.7 write Simpler and Sampler sample parts without an ``Id``;
the ``[@Id=...]`` paths do not reach those, and the id-less spellings are left
out rather than doubling every sample-part member.
"""
'''

OBJECTS_DOC = '''"""The settings of a set's tracks, mixers, clips and transport, measured from real sets.

GENERATED by scripts/gen_vocab.py; do not edit by hand. Regenerate from the abletoolz
checkout with ``uv run python scripts/gen_vocab.py --corpus DIR``.

One settings ``StrEnum`` per object, read the same way as the device settings in
:mod:`abletoolz.schema.devices`: a member's value is the setting's path relative
to the object element in the newest Live version that writes it, and what the
census measured about it is in ``data/settings.json``.

* ``Track``: every track element, any type, the main track included.
* ``Mixer``: a track's ``DeviceChain/Mixer``.
* ``Clip``: a ``MidiClip`` or an ``AudioClip``.
* ``Transport``: ``LiveSet/Transport``.

``Track``, ``Mixer`` and ``Clip`` cover several element types (``OWNERS``), and
not every setting is on every one. The main track's element was ``MasterTrack``
before Live 12 and both count as ``MainTrack`` (``OWNER_SPELLINGS``).
"""
'''


def upper_snake(name: str) -> str:
    """``Compressor2`` -> ``COMPRESSOR2``, ``MidiArpeggiator`` -> ``MIDI_ARPEGGIATOR``."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name)
    snake = re.sub(r"[^A-Za-z0-9]+", "_", spaced).strip("_").upper()
    if not snake or snake[0].isdigit():
        raise SystemExit(f"{name!r} does not make a member name")
    return snake


def camel(upper: str) -> str:
    return "".join(part.capitalize() for part in upper.split("_") if part)


def member_name(tag: str) -> str:
    return DISPLAY_NAMES.get(tag) or upper_snake(tag)


def setting_member_names(settings: dict[str, SettingTally]) -> dict[str, str]:
    """Name each path by its shortest unique suffix; the shallowest of a clash keeps the bare name.

    A setting Live renamed (two spellings merged into one) takes its full path
    as its name, so it reads the same whichever spelling a reader knows.
    """
    paths = sorted(settings)
    words = {path: path_words(path) for path in paths}
    renamed = {path for path in paths if settings[path].renamed}
    by_last: dict[str, list[str]] = {}
    for path in paths:
        by_last.setdefault(upper_snake(words[path][-1]), []).append(path)
    keeps_bare: set[str] = set()
    for group in by_last.values():
        depths = sorted(len(words[path]) for path in group)
        if len(group) == 1 or (depths[0] < depths[1] and depths[0] <= BARE_NAME_MAX_DEPTH):
            keeps_bare.add(min(group, key=lambda path: len(words[path])))
    keeps_bare -= renamed
    # A wrapper word alone names nothing: ``TrackDelay/Value`` is TRACK_DELAY_VALUE, not VALUE.
    keeps_bare -= {path for path in paths if words[path][-1] in FILLER_WORDS and len(words[path]) > 1}
    suffix = {path: len(words[path]) if path in renamed else 1 if path in keeps_bare else 2 for path in paths}
    while True:
        names = {path: upper_snake("_".join(words[path][-suffix[path] :])) for path in paths}
        seen = Counter(names.values())
        clashing = [path for path in paths if seen[names[path]] > 1 and path not in keeps_bare]
        if not clashing:
            if len(seen) != len(paths):
                raise SystemExit(f"member names collide: {[name for name, n in seen.items() if n > 1]}")
            return names
        grew = False
        for path in clashing:
            if suffix[path] < len(words[path]):
                suffix[path] += 1
                grew = True
        if not grew:
            raise SystemExit(f"no unique member names for {clashing}")


def version_range(low: Version, high: Version) -> str:
    return version_text(low) if low == high else f"{version_text(low)}-{version_text(high)}"


def setting_note(setting: SettingTally) -> str:
    """What the census saw of ``setting``, in a line: its role, value kind, count, versions and earlier spellings."""
    role = "parameter" if setting.is_parameter else "value"
    if setting.changed_shape:
        assert setting.parameter_versions is not None and setting.value_versions is not None
        role = (
            f"parameter in Live {version_range(*setting.parameter_versions)}, "
            f"value in Live {version_range(*setting.value_versions)}"
        )
    kind = resolve_kind(setting.kinds).value
    if len(setting.kinds) > 1:
        kind += f" (seen as {', '.join(sorted(k.value for k in setting.kinds))})"
    if len(setting.values) == 1:
        kind += " (one value seen)"
    seen = f"{setting.count} seen, Live {version_range(setting.min_version, setting.max_version)}"
    lists = "".join(f", {placeholder_for(i)} <= {high}" for i, high in enumerate(setting.max_indices))
    ordered = sorted(setting.spellings.items(), key=lambda item: item[1][0])
    was = "".join(f"; was {spelled} in Live {version_range(*seen)}" for spelled, seen in ordered[:-1])
    return f"{role}, {kind}, {seen}{lists}{was}"


def member_assignment(name: str, path: str) -> str:
    assignment = f'    {name} = "{path}"'
    if len(f'        "{path}"') > LINE_LENGTH:
        return assignment + "  # noqa: E501 - the path alone is longer than a line"
    if len(assignment) > LINE_LENGTH:
        return f'    {name} = (\n        "{path}"\n    )'
    return assignment


# The repo's ruff line length; the module is run through ruff format after it is written.
LINE_LENGTH = 120


@dataclass(slots=True)
class SettingsClass:
    """One settings enum to emit: its class name, docstring and census."""

    name: str
    doc: str
    tally: ObjectTally
    names: dict[str, str] = field(default_factory=dict)


def emit_class(lines: list[str], settings_class: SettingsClass) -> None:
    tally = settings_class.tally
    settings_class.names = setting_member_names(tally.settings)
    if not settings_class.names:
        raise SystemExit(f"{settings_class.name} has no settings; a settings enum would be empty")
    lines += ["", "", f"class {settings_class.name}(enum.StrEnum):", f'    """{settings_class.doc}"""', ""]
    names = settings_class.names
    lines += [member_assignment(names[path], path) for path in sorted(names, key=names.__getitem__)]


def undeclared_in(setting: SettingTally, owners: Iterable[str], inside: str, minor_of: dict[Version, str]) -> list[str]:
    """The ``MinorVersion``s a set held ``setting`` in although Live's schema of that version does not declare it.

    Read the way :func:`abletoolz.schema.settings.path_for` reads it: declared
    when any owner, under either main-track spelling, declares any spelling; a
    schema that cannot tell is no evidence either way.
    """
    schemas = live_schemas()
    classes = schema_classes(owners)
    prefix = f"{inside}/" if inside else ""
    found: set[str] = set()
    for version in setting.versions:
        minor = minor_of[version]
        answers = {schemas.declares(minor, cls, prefix + spelled) for cls in classes for spelled in setting.spellings}
        if False in answers and True not in answers:
            found.add(minor)
    return sorted(found)


type Record = dict[str, object]


def schema_classes(owners: Iterable[str]) -> set[str]:
    """The schema classes the census's owner tags can be, under either main-track spelling."""
    return {spelled for owner in owners for spelled in (owner, *OWNER_CLASSES.get(owner, ()))}


def corpus_overrides(setting: SettingTally, owners: Iterable[str], inside: str, label: str) -> Record:
    """The census's ``kind`` and ``parameter`` where the newest schema typing the setting does not answer.

    That is where no schema types it, or where the two read differently (``int``
    against ``float`` is not that: Live writes whole floats without a decimal
    point). Each disagreement is printed.
    """
    prefix = f"{inside}/" if inside else ""
    typed = live_schemas().newest_value_type(schema_classes(owners), [prefix + s for s in setting.spellings])
    kind = resolve_kind(setting.kinds).value
    found: Record = {}
    if typed is None or (typed.kind != kind and {typed.kind, kind} - {"int", "float"}):
        found["kind"] = kind
    if typed is None or typed.timeable != setting.is_parameter:
        found["parameter"] = setting.is_parameter
    if typed is not None and found:
        print(f"  {label}: the schema reads {typed}, the census {found}; kept the census's")
    return found


def settings_records(
    classes: list[SettingsClass], minor_of: dict[Version, str], *, with_owners: bool
) -> dict[str, dict[str, Record]]:
    """Per settings class, per member: what ``data/settings.json`` holds about it.

    ``kind`` and ``parameter`` are there only where Live's schemas do not
    answer for the setting (:func:`corpus_overrides`). ``kind`` is what every
    value seen parses as: ``bool`` when only ``true``/``false``, ``int`` when
    all were whole numbers, ``float`` when some had a fraction, else ``str``.
    ``parameter`` says whether the setting is automatable (its value in a
    ``Manual`` child), following the newest form when it changed shape.
    ``older`` lists every spelling of a setting Live renamed, newest first, each
    from the first version that writes it. ``undeclared`` lists the
    ``MinorVersion``s a set held it in although Live's schema of that version
    does not declare it (``minor_of`` gives each release's). ``seen_on`` lists
    the owner tags of an object setting that is not on every one of them, and
    ``census`` says what the census saw.
    """
    records: dict[str, dict[str, Record]] = {}
    for settings_class in classes:
        tally = settings_class.tally
        names = settings_class.names
        inside = INSIDE.get(settings_class.name, "")
        all_owners = set(tally.owner_versions)
        members: dict[str, Record] = {}
        for path in sorted(names, key=names.__getitem__):
            setting = tally.settings[path]
            label = f"{settings_class.name}.{names[path]}"
            record = corpus_overrides(setting, tally.owner_versions, inside, label)
            if len(setting.spellings) > 1:
                record["older"] = [[list(since), spelled] for since, spelled in older_paths(setting, path)]
            minors = undeclared_in(setting, tally.owner_versions, inside, minor_of)
            if minors:
                record["undeclared"] = minors
            if with_owners and setting.owners != all_owners:
                record["seen_on"] = sorted(setting.owners)
            record["census"] = setting_note(setting)
            members[names[path]] = record
        records[settings_class.name] = members
    return records


def table_block(name: str, annotation: str, doc: str, body: list[str]) -> list[str]:
    return ["", f"{name}: {annotation} = {{", *body, "}", f'"""{doc}"""']


def module_header(doc: str) -> list[str]:
    return [doc.rstrip("\n"), "", "from __future__ import annotations", "", "import enum"]


def census_line(census: Census, tallies: Iterable[ObjectTally]) -> str:
    versions = sorted(v for tally in tallies for v in (tally.min_version, tally.max_version))
    return f"{census.sets_scanned} sets saved by Live {version_text(versions[0])} to {version_text(versions[-1])}"


def emit_devices(census: Census) -> tuple[str, dict[str, object]]:
    """``devices.py`` and its part of ``data/settings.json``."""
    device_members = {tag: member_name(tag) for tag in census.devices}
    dupes = [name for name, n in Counter(device_members.values()).items() if n > 1]
    if dupes:
        raise SystemExit(f"device member names collide: {dupes}")
    native = sorted((tag for tag in census.devices if tag not in HOSTED_TAGS), key=lambda t: device_members[t])

    lines = module_header(DEVICES_DOC)
    lines += [
        "",
        "",
        "class ValueKind(enum.StrEnum):",
        '    """What the ``Value`` of a setting reads as."""',
        "",
        '    BOOL = "bool"',
        '    INT = "int"',
        '    FLOAT = "float"',
        '    STR = "str"',
        "",
        "",
        "class Device(enum.StrEnum):",
        '    """Every tag seen as a device in a ``Devices`` list; the value is the XML tag."""',
        "",
    ]
    device_notes: dict[str, str] = {}
    for tag in sorted(census.devices, key=lambda t: device_members[t]):
        tally = census.devices[tag]
        hosted = ", hosted: no settings enum" if tag in HOSTED_TAGS else ""
        device_notes[device_members[tag]] = (
            f"{tally.count} seen, Live {version_range(tally.min_version, tally.max_version)}{hosted}"
        )
        lines.append(f'    {device_members[tag]} = "{tag}"')

    classes = [
        SettingsClass(camel(device_members[tag]), f"Settings of ``Device.{device_members[tag]}``.", census.devices[tag])
        for tag in native
    ]
    for settings_class in classes:
        emit_class(lines, settings_class)
    settings_of = [f"    Device.{device_members[tag]}: {cls.name}," for tag, cls in zip(native, classes, strict=True)]
    lines += [
        "",
        "",
        "SETTINGS_OF: dict[Device, type[enum.StrEnum]] = {",
        *settings_of,
        "}",
        '"""The settings enum of every native device."""',
        "",
    ]
    total = sum(tally.count for tally in census.devices.values())
    data: dict[str, object] = {
        "census": f"{census_line(census, census.devices.values())}; {total} devices",
        "devices": device_notes,
        "settings": settings_records(classes, minor_versions(census), with_owners=False),
    }
    return "\n".join(lines), data


def emit_objects(census: Census) -> tuple[str, dict[str, object]]:
    """``objects.py`` and its part of ``data/settings.json``."""
    missing = [name for name in OBJECT_DOCS if name not in census.objects]
    if missing:
        raise SystemExit(f"the corpus holds no {missing}")
    lines = module_header(OBJECTS_DOC)
    classes = [SettingsClass(name, doc, census.objects[name]) for name, doc in OBJECT_DOCS.items()]
    for settings_class in classes:
        emit_class(lines, settings_class)
    owners = [
        f"    {cls.name}: frozenset({{{', '.join(repr(o) for o in sorted(cls.tally.owner_versions))}}}),"
        for cls in classes
    ]
    lines += [
        "",
        *table_block(
            "OWNERS",
            "dict[type[enum.StrEnum], frozenset[str]]",
            "Per settings class: every element tag it covers.",
            owners,
        ),
        *table_block(
            "OWNER_SPELLINGS",
            "dict[str, str]",
            "Element tags that are another owner under an older name.",
            [f'    "{old}": "{new}",' for old, new in OWNER_SPELLINGS.items()],
        ),
        *table_block(
            "INSIDE",
            "dict[type[enum.StrEnum], str]",
            "Settings classes whose element sits inside the owner element, and where.",
            [f'    {cls}: "{path}",' for cls, path in INSIDE.items()],
        ),
        "",
    ]
    data: dict[str, object] = {
        "census": census_line(census, census.objects.values()),
        "settings": settings_records(classes, minor_versions(census), with_owners=True),
    }
    return "\n".join(lines), data


# --------------------------------------------------------------------------- structure

# Elements of a tag a schema must hold before "on every one of them" counts as required.
REQUIRED_EVIDENCE = 3
BETA_CREATOR = re.compile(r"\db\d")

type Schema = tuple[str, int | None]


def schema_of(header: Header) -> Schema:
    attributes = dict(header)
    count = attributes.get("SchemaChangeCount")
    return attributes["MinorVersion"], None if count is None else int(count)


def minor_versions(census: Census) -> dict[Version, str]:
    """Each release's ``MinorVersion``, from the root attributes it wrote."""
    return {version: dict(release_header(tally))["MinorVersion"] for version, tally in census.grammar.items()}


def release_header(tally: GrammarTally) -> Header:
    """The root attributes a release wrote: its most common, a release build's over a beta's."""
    ranked = sorted(
        tally.headers.items(),
        key=lambda item: (BETA_CREATOR.search(dict(item[0]).get("Creator", "")) is not None, -item[1]),
    )
    return ranked[0][0]


def spans[K](tallies: dict[Version, GrammarTally], counts: Callable[[GrammarTally], Counter[K]]) -> dict[K, Range]:
    found: dict[K, Range] = {}
    for version, tally in tallies.items():
        for key in counts(tally):
            found[key] = widen_range(found.get(key), (version, version)) or (version, version)
    return found


def required(tally: GrammarTally, counts: Counter[Pair]) -> set[Pair]:
    """The pairs present on every element of their tag, where the tag is common enough to say so."""
    return {
        pair
        for pair, count in counts.items()
        if count == tally.instances[pair[0]] and tally.instances[pair[0]] >= REQUIRED_EVIDENCE
    }


def variable(per_schema: dict[Schema, GrammarTally], picked: dict[Schema, set[Pair]]) -> set[Pair]:
    """Pairs required in one schema and left off somewhere in another that holds enough of the tag."""
    every = set().union(*picked.values())
    return {
        pair
        for pair in every
        if any(
            pair not in picked[schema] and tally.instances[pair[0]] >= REQUIRED_EVIDENCE
            for schema, tally in per_schema.items()
        )
    }


def by_tag[V](pairs: dict[Pair, V]) -> dict[str, dict[str, V]]:
    table: dict[str, dict[str, V]] = {}
    for (tag, name), value in sorted(pairs.items()):
        table.setdefault(tag, {})[name] = value
    return dict(sorted(table.items()))


def emit_structure(census: Census) -> dict[str, object]:
    """``data/structure.json``: what each release writes; :mod:`abletoolz.schema.structure` says what each table is."""
    tallies = census.grammar
    releases = sorted(tallies)
    headers = {version: release_header(tallies[version]) for version in releases}
    per_schema: dict[Schema, GrammarTally] = {}
    for version in releases:
        per_schema.setdefault(schema_of(headers[version]), GrammarTally()).merge(tallies[version])

    tag_spans = spans(tallies, lambda tally: tally.instances)
    low, high = version_text(releases[0]), version_text(releases[-1])
    data: dict[str, object] = {
        "census": f"{census.sets_scanned} sets saved by Live {low} to {high}",
        "releases": [
            {"release": list(version), "root": [list(pair) for pair in headers[version]], "sets": tallies[version].sets}
            for version in releases
        ],
    }
    for kind, counts in (("children", lambda t: t.children), ("attributes", lambda t: t.attributes)):
        found = spans(tallies, counts)
        data[f"newer_{kind}"] = by_tag(
            {pair: list(seen[0]) for pair, seen in found.items() if seen[0] > tag_spans[pair[0]][0]}
        )

    picked_attributes = {schema: required(tally, tally.attributes) for schema, tally in per_schema.items()}
    picked_children = {schema: required(tally, tally.children) for schema, tally in per_schema.items()}
    varying_attributes = variable(per_schema, picked_attributes)
    varying_children = variable(per_schema, picked_children)
    schemas = sorted(per_schema, key=lambda schema: min(v for v in releases if schema_of(headers[v]) == schema))

    def grouped(picked: set[Pair]) -> dict[str, list[str]]:
        table: dict[str, list[str]] = {}
        for tag, name in sorted(picked):
            table.setdefault(tag, []).append(name)
        return table

    data["schemas"] = [
        {
            "minor_version": schema[0],
            "schema_change_count": schema[1],
            "required_attributes": grouped(picked_attributes[schema] & varying_attributes),
            "required_children": grouped(picked_children[schema] & varying_children),
            "attribute_order": {
                tag: list(per_schema[schema].orders[tag].most_common(1)[0][0])
                for tag in sorted({tag for tag, _ in picked_attributes[schema] & varying_attributes})
            },
        }
        for schema in schemas
    ]
    every_values: dict[Pair, Counter[str]] = {}
    for tally in per_schema.values():
        for pair in varying_attributes:
            if pair in tally.values:
                every_values.setdefault(pair, Counter()).update(tally.values[pair])
    data["fill_values"] = by_tag({pair: values.most_common(1)[0][0] for pair, values in every_values.items()})
    votes: Counter[Pair] = Counter()
    for tally in tallies.values():
        votes.update(tally.kinds)
    data["device_kinds"] = {
        device: max(("audio_effect", "instrument", "midi_effect"), key=lambda kind: votes[device, kind])
        for device in sorted({device for device, _ in votes})
    }
    # The main track's MasterTrack became MainTrack in Live 12.
    renames = {**RENAMES, **{old: (new, (12, 0, 0)) for old, new in OWNER_SPELLINGS.items()}}
    # A renamed element arrives with everything it carries, so nothing in it looks newer
    # than it; what its old spelling held is the measure instead.
    child_spans = spans(tallies, lambda tally: tally.children)
    attribute_spans = spans(tallies, lambda tally: tally.attributes)
    data["renamed_held"] = {
        old_tag: {
            key: {name: list(seen[0]) for (parent, name), seen in sorted(found.items()) if parent == old_tag}
            for key, found in (("children", child_spans), ("attributes", attribute_spans))
        }
        for old_tag in sorted(renames)
    }
    data["tag_renames"] = {old: {"new": new, "since": list(since)} for old, (new, since) in sorted(renames.items())}
    return data


def write_data(path: Path, data: object) -> None:
    """One JSON data file, written the same way every run so a rerun on the same corpus changes nothing."""
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(f"data -> {path}")


def write_module(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")
    ruff = [sys.executable, "-m", "ruff", "format", "--quiet", str(path)]
    if subprocess.run(ruff, check=False).returncode != 0:
        print(f"ruff format did not run on {path}; format it before committing")
    print(f"module -> {path}")


def main(argv: Iterable[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", type=Path, required=True, help="folder of .als sets, walked recursively")
    parser.add_argument("--skip", type=Path, action="append", default=[], help="a folder under --corpus to leave out")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--sample", type=int, default=0, help="scan a random sample of this many sets")
    parser.add_argument("--json", type=Path, help="write the raw census here")
    parser.add_argument("--devices-out", type=Path, default=DEVICES_OUT)
    parser.add_argument("--objects-out", type=Path, default=OBJECTS_OUT)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR, help="where settings.json and structure.json go")
    parser.add_argument("--dry-run", action="store_true", help="census only, do not write the modules")
    args = parser.parse_args(None if argv is None else list(argv))

    started = time.perf_counter()
    paths = iter_corpus(args.corpus, args.skip) + live_written_fixtures()
    listed = time.perf_counter() - started
    total = len(paths)
    if args.sample:
        paths = random.Random(0).sample(paths, min(args.sample, total))
    print(f"{total} sets found in {listed:.1f}s; scanning {len(paths)} with {args.workers} workers")

    census = run_census(paths, args.workers)
    elapsed = time.perf_counter() - started
    print(f"scanned {census.sets_scanned}, skipped {sum(census.skipped.values())} {dict(census.skipped)}")
    print(f"{len(census.devices)} device tags, {sum(d.count for d in census.devices.values())} devices")
    for name, tally in sorted(census.objects.items()):
        print(f"{name}: {tally.count} seen, {len(tally.settings)} settings")
    print(f"structure: {len(census.grammar)} releases")
    print(f"elapsed {elapsed:.1f}s")
    if args.sample:
        print(f"extrapolated full run: {elapsed / len(paths) * total:.0f}s")

    if args.json is not None:
        args.json.write_text(json.dumps(census_json(census), indent=1), encoding="utf-8")
        print(f"census -> {args.json}")
    if not args.dry_run:
        devices_text, devices_data = emit_devices(census)
        objects_text, objects_data = emit_objects(census)
        write_module(args.devices_out, devices_text)
        write_module(args.objects_out, objects_text)
        write_data(args.data_dir / "settings.json", {"devices": devices_data, "objects": objects_data})
        write_data(args.data_dir / "structure.json", emit_structure(census))


if __name__ == "__main__":
    main()
