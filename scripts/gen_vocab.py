"""Measure Live's XML vocabulary across a corpus of sets; write schema/devices.py and schema/objects.py.

Run from the abletoolz checkout::

    uv run python scripts/gen_vocab.py --corpus DIR [--skip DIR ...]        # writes both modules
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
the newest spelling; the older spellings go to ``OLDER_PATHS`` with the version
each one stops at. Renames are found two ways: a tag rename declared in
``RENAMES`` below and checked against the corpus (the run stops if the corpus
contradicts it), and two paths that read the same word for word once Live's
separators are ignored (``SideChainEq/Gain`` became ``SideChainEq_Gain``).

The raw census goes to ``--json``; the modules go to ``--devices-out`` and
``--objects-out``. Neither module holds a set name, a path or a value from a set.
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

REPO_ROOT = Path(__file__).resolve().parent.parent

type Version = tuple[int, int, int]
type Range = tuple[Version, Version]


def _load_parse_creator() -> Callable[[str], Version]:
    """``abletoolz.schema.versioning.parse_creator``, loaded from its file.

    versioning.py imports nothing from the package, so loading it alone keeps
    the census independent of whatever else in the checkout is mid-change.
    """
    spec = importlib.util.spec_from_file_location(
        "_census_versioning", REPO_ROOT / "abletoolz" / "schema" / "versioning.py"
    )
    if spec is None or spec.loader is None:
        raise SystemExit("cannot load abletoolz/schema/versioning.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    parse: Callable[[str], Version] = module.parse_creator
    return parse


parse_creator = _load_parse_creator()

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


@dataclass(slots=True)
class SetResult:
    """What one set contributed, or why it was skipped."""

    version: Version | None = None
    devices: dict[str, ObjectTally] = field(default_factory=dict)
    objects: dict[str, ObjectTally] = field(default_factory=dict)
    skip_reason: str | None = None


@dataclass(slots=True)
class Census:
    """The merged corpus census."""

    sets_scanned: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    versions: Counter[str] = field(default_factory=Counter)
    devices: dict[str, ObjectTally] = field(default_factory=dict)
    objects: dict[str, ObjectTally] = field(default_factory=dict)

    def merge(self, result: SetResult) -> None:
        if result.skip_reason is not None:
            self.skipped[result.skip_reason] += 1
            return
        assert result.version is not None
        self.sets_scanned += 1
        self.versions[f"{result.version[0]}.{result.version[1]}"] += 1
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
        mixer = track.find("DeviceChain/Mixer")
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

    result = SetResult(version=version)
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

Census: {sets} sets saved by Live {low} to {high}; {devices} devices.

``Device`` names every tag seen as a direct child of a ``Devices`` list, racks
walked into. Member names are Live's display names where the tag differs
(``MultiSampler`` is ``SAMPLER``, ``OriginalSimpler`` is ``SIMPLER``,
``StereoGain`` is ``UTILITY``). Hosted plugins and Max devices are listed but
have no settings enum: their state is the plugin's own.

Each native device has a settings ``StrEnum`` named after its ``Device`` member
(``Device.SAMPLER`` -> ``Sampler``). A value is the setting's path relative to
the device element in the newest Live version that writes it:

* automatable parameters (``is_parameter(member)``) name an element whose
  ``Manual`` child holds the value;
* every other member is a leaf whose own ``Value`` attribute holds it.

A setting can change shape between versions (a plain value that later became a
parameter); ``is_parameter`` follows the newest form and the member's comment
gives both version ranges, so a reader should take ``Manual`` when the element
has one and the element's own ``Value`` otherwise.

A setting can also change spelling. ``OLDER_PATHS`` holds every earlier path
with the first version that writes each, and ``FIRST_SEEN`` the first version
the setting was seen in; :func:`abletoolz.schema.settings.path_for` reads both,
so code names a member and never a version.

The tables are keyed by settings class first. A ``StrEnum`` member is equal to
its string, so ``EqEight.MODE`` and another device's ``"Mode"`` member would be
one key in a flat table; use ``abletoolz.schema.settings`` (or ``kind_of``,
``is_parameter`` and ``is_templated`` here) rather than indexing by hand.

A member is named by the last path segment. When several paths end the same
way, the shallowest keeps the bare name if it sits at most two segments deep,
and the rest take the shortest path suffix that is unique (``Slot`` and
``Value`` wrappers left out), so
``Sampler.VOLUME`` is the amp volume and ``Sampler.MULTI_SAMPLE_PART_VOLUME``
the per-sample-part one. A setting Live renamed takes its full path as its name.

Lists stay one member each, with placeholders in the value: ``{{index}}`` for the
outermost list in a path, ``{{index2}}`` for a list inside it. Live spells lists
two ways and both are kept verbatim: numbered tags (``Bands.{{index}}``, where the
number is the position) and same-tag siblings told apart by an ``Id`` attribute
(``MultiSamplePart[@Id='{{index}}']``, ElementTree's predicate syntax, where the
number is that ``Id`` and need not be 0..n-1). Fill with
``member.format(index=n)`` before ``find``; ``is_templated`` tells such a
member apart so a caller can refuse an unfilled one. They are kept, not dropped,
because EQ bands, sample parts and macros are most of what those devices are.
Sets before Live 9.7 write Simpler and Sampler sample parts without an ``Id``;
the ``[@Id=...]`` paths do not reach those, and the id-less spellings are left
out rather than doubling every sample-part member.

``SETTING_KIND`` is what every value seen for a setting parses as: BOOL when
only ``true``/``false``, INT when all were whole numbers, FLOAT when some had a
fraction, else STR. Live writes whole floats without a decimal point, so a
continuous parameter that was never moved off a whole number reads INT; treat
INT as "a number" when writing.

Comments give how often a device or setting was seen and the Live versions it
was seen in, and every earlier spelling a setting had.
"""
'''

OBJECTS_DOC = '''"""The settings of a set's tracks, mixers, clips and transport, measured from real sets.

GENERATED by scripts/gen_vocab.py; do not edit by hand. Regenerate from the abletoolz
checkout with ``uv run python scripts/gen_vocab.py --corpus DIR``.

Census: {sets} sets saved by Live {low} to {high}.

One settings ``StrEnum`` per object, read the same way as the device settings in
:mod:`abletoolz.schema.devices`: a member's value is the setting's path relative
to the object element in the newest Live version that writes it, an automatable
parameter keeps its value in a ``Manual`` child, and list members hold
``{{index}}`` placeholders to fill.

* ``Track``: every track element, any type, the main track included.
* ``Mixer``: a track's ``DeviceChain/Mixer``.
* ``Clip``: a ``MidiClip`` or an ``AudioClip``.
* ``Transport``: ``LiveSet/Transport``.

``Track``, ``Mixer`` and ``Clip`` cover several element types, and not every
setting is on every one: ``SEEN_ON`` lists the owners of each setting that is
not on all of them (``OWNERS``). The main track's element was ``MasterTrack``
before Live 12 and both count as ``MainTrack``.

``FIRST_SEEN``, ``OLDER_PATHS`` and ``VERSIONS_SEEN`` hold the version facts
:func:`abletoolz.schema.settings.path_for` reads.
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


def version_literal(version: Version) -> str:
    return f"({version[0]}, {version[1]}, {version[2]})"


def setting_comment(setting: SettingTally) -> str:
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
    return f"    # {role}, {kind}, {seen}{lists}"


def spelling_comments(setting: SettingTally) -> list[str]:
    if len(setting.spellings) < 2:
        return []
    ordered = sorted(setting.spellings.items(), key=lambda item: item[1][0])
    lines = [f"    # was {spelled} in Live {version_range(*seen)}" for spelled, seen in ordered[:-1]]
    return [
        line if len(line) <= LINE_LENGTH else f"{line}  # noqa: E501 - the path alone is that long" for line in lines
    ]


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
    for path in sorted(names, key=names.__getitem__):
        setting = tally.settings[path]
        lines.append(setting_comment(setting))
        lines += spelling_comments(setting)
        lines.append(member_assignment(names[path], path))


def table_block(name: str, annotation: str, doc: str, body: list[str]) -> list[str]:
    return ["", f"{name}: {annotation} = {{", *body, "}", f'"""{doc}"""']


def emit_tables(
    classes: list[SettingsClass], *, with_owners: bool, extra_versions: Iterable[ObjectTally] = ()
) -> list[str]:
    """The per-class tables both modules share, keyed by settings class first.

    ``extra_versions`` are tallies with no settings enum (hosted devices) whose
    versions still count as evidence in ``VERSIONS_SEEN``.
    """
    kind: list[str] = []
    parameters: list[str] = []
    templated: list[str] = []
    first_seen: list[str] = []
    older: list[str] = []
    seen_on: list[str] = []
    owners: list[str] = []
    versions_seen: list[str] = []
    for settings_class in classes:
        cls = settings_class.name
        tally = settings_class.tally
        names = settings_class.names
        ordered = sorted(names, key=names.__getitem__)
        kind += [
            f"    {cls}: {{",
            *(f"        {cls}.{names[p]}: ValueKind.{resolve_kind(tally.settings[p].kinds).name}," for p in ordered),
            "    },",
        ]
        members = [f"            {cls}.{names[p]}," for p in ordered if tally.settings[p].is_parameter]
        parameters += (
            [f"    {cls}: frozenset(", "        {", *members, "        }", "    ),"]
            if members
            else [f"    {cls}: frozenset(),"]
        )
        members = [f"            {cls}.{names[p]}," for p in ordered if PLACEHOLDER_RE.search(p)]
        templated += (
            [f"    {cls}: frozenset(", "        {", *members, "        }", "    ),"]
            if members
            else [f"    {cls}: frozenset(),"]
        )
        first_seen += [
            f"    {cls}: {{",
            *(f"        {cls}.{names[p]}: {version_literal(tally.settings[p].min_version)}," for p in ordered),
            "    },",
        ]
        moved = [p for p in ordered if len(tally.settings[p].spellings) > 1]
        older.append(f"    {cls}: {{")
        for path in moved:
            entries = older_paths(tally.settings[path], path)
            older.append(f"        {cls}.{names[path]}: (")
            older += [f'            ({version_literal(v)}, "{spelled}"),' for v, spelled in entries]
            older.append("        ),")
        older.append("    },")
        if with_owners:
            all_owners = set(tally.owner_versions)
            owners.append(f"    {cls}: frozenset({{{', '.join(repr(o) for o in sorted(all_owners))}}}),")
            seen_on.append(f"    {cls}: {{")
            for path in ordered:
                mine = tally.settings[path].owners
                if mine != all_owners:
                    seen_on.append(
                        f"        {cls}.{names[path]}: frozenset({{{', '.join(repr(o) for o in sorted(mine))}}}),"
                    )
            seen_on.append("    },")
    every_owner: dict[str, set[Version]] = {}
    for tally in (*(settings_class.tally for settings_class in classes), *extra_versions):
        for owner, versions in tally.owner_versions.items():
            every_owner.setdefault(owner, set()).update(versions)
    for owner in sorted(every_owner):
        versions_seen.append(f'    "{owner}": (')
        versions_seen += [f"        {version_literal(v)}," for v in sorted(every_owner[owner])]
        versions_seen.append("    ),")

    settings_type = "type[enum.StrEnum]"
    lines = [
        *table_block(
            "SETTING_KIND",
            f"dict[{settings_type}, dict[enum.StrEnum, ValueKind]]",
            "Per settings class: what every value seen for each setting parses as.",
            kind,
        ),
        *table_block(
            "PARAMETERS",
            f"dict[{settings_type}, frozenset[enum.StrEnum]]",
            "Per settings class: automatable settings, whose element holds the value in a ``Manual`` child.",
            parameters,
        ),
        *table_block(
            "TEMPLATED",
            f"dict[{settings_type}, frozenset[enum.StrEnum]]",
            "Per settings class: settings whose path holds ``{index}`` placeholders to fill before ``find``.",
            templated,
        ),
        *table_block(
            "FIRST_SEEN",
            f"dict[{settings_type}, dict[enum.StrEnum, Version]]",
            "Per settings class: the first Live version each setting was seen in.",
            first_seen,
        ),
        *table_block(
            "OLDER_PATHS",
            f"dict[{settings_type}, dict[enum.StrEnum, tuple[tuple[Version, str], ...]]]",
            "Per settings class: every spelling of a setting Live renamed, newest first, from the first version "
            "that writes it.",
            older,
        ),
    ]
    if with_owners:
        lines += [
            *table_block(
                "OWNERS",
                f"dict[{settings_type}, frozenset[str]]",
                "Per settings class: every element tag it covers.",
                owners,
            ),
            *table_block(
                "SEEN_ON",
                f"dict[{settings_type}, dict[enum.StrEnum, frozenset[str]]]",
                "Per settings class: the owners of each setting that is not on every one of them.",
                seen_on,
            ),
        ]
    lines += table_block(
        "VERSIONS_SEEN",
        "dict[str, tuple[Version, ...]]",
        "Per element tag: every Live version the corpus holds one in, the evidence a missing setting rests on.",
        versions_seen,
    )
    return lines


def module_header(doc: str) -> list[str]:
    return [
        doc.rstrip("\n"),
        "",
        "from __future__ import annotations",
        "",
        "import enum",
        "",
        "from abletoolz.schema.versioning import Version",
    ]


def emit_devices(census: Census) -> str:
    device_members = {tag: member_name(tag) for tag in census.devices}
    dupes = [name for name, n in Counter(device_members.values()).items() if n > 1]
    if dupes:
        raise SystemExit(f"device member names collide: {dupes}")
    native = sorted((tag for tag in census.devices if tag not in HOSTED_TAGS), key=lambda t: device_members[t])
    versions = sorted(v for tally in census.devices.values() for v in (tally.min_version, tally.max_version))

    lines = module_header(
        DEVICES_DOC.format(
            sets=census.sets_scanned,
            low=version_text(versions[0]),
            high=version_text(versions[-1]),
            devices=sum(tally.count for tally in census.devices.values()),
        )
    )
    lines += [
        "",
        "",
        "class ValueKind(enum.StrEnum):",
        '    """What every ``Value`` seen for a setting parses as."""',
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
    for tag in sorted(census.devices, key=lambda t: device_members[t]):
        tally = census.devices[tag]
        hosted = ", hosted: no settings enum" if tag in HOSTED_TAGS else ""
        lines.append(f"    # {tally.count} seen, Live {version_range(tally.min_version, tally.max_version)}{hosted}")
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
    ]
    hosted = [census.devices[tag] for tag in sorted(census.devices) if tag in HOSTED_TAGS]
    lines += emit_tables(classes, with_owners=False, extra_versions=hosted)
    lines += [
        "",
        "",
        "def kind_of(setting: enum.StrEnum) -> ValueKind:",
        '    """What every value seen for ``setting`` parsed as."""',
        "    return SETTING_KIND[type(setting)][setting]",
        "",
        "",
        "def is_parameter(setting: enum.StrEnum) -> bool:",
        '    """Whether ``setting`` names an automatable parameter (value in its ``Manual`` child)."""',
        "    return setting in PARAMETERS[type(setting)]",
        "",
        "",
        "def is_templated(setting: enum.StrEnum) -> bool:",
        '    """Whether ``setting``\'s path holds ``{index}`` placeholders to fill before use."""',
        "    return setting in TEMPLATED[type(setting)]",
        "",
    ]
    return "\n".join(lines)


def emit_objects(census: Census) -> str:
    missing = [name for name in OBJECT_DOCS if name not in census.objects]
    if missing:
        raise SystemExit(f"the corpus holds no {missing}")
    versions = sorted(v for tally in census.objects.values() for v in (tally.min_version, tally.max_version))
    lines = module_header(
        OBJECTS_DOC.format(sets=census.sets_scanned, low=version_text(versions[0]), high=version_text(versions[-1]))
    )
    lines.insert(
        lines.index("from abletoolz.schema.versioning import Version"), "from abletoolz.schema.devices import ValueKind"
    )
    classes = [SettingsClass(name, doc, census.objects[name]) for name, doc in OBJECT_DOCS.items()]
    for settings_class in classes:
        emit_class(lines, settings_class)
    lines += emit_tables(classes, with_owners=True)
    lines += [
        "",
        "OWNER_SPELLINGS: dict[str, str] = {",
        *(f'    "{old}": "{new}",' for old, new in OWNER_SPELLINGS.items()),
        "}",
        '"""Element tags that are another owner under an older name."""',
        "",
    ]
    return "\n".join(lines)


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
    print(f"elapsed {elapsed:.1f}s")
    if args.sample:
        print(f"extrapolated full run: {elapsed / len(paths) * total:.0f}s")

    if args.json is not None:
        args.json.write_text(json.dumps(census_json(census), indent=1), encoding="utf-8")
        print(f"census -> {args.json}")
    if not args.dry_run:
        write_module(args.devices_out, emit_devices(census))
        write_module(args.objects_out, emit_objects(census))


if __name__ == "__main__":
    main()
