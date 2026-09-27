"""Measure Live's native device XML across a corpus of sets; write schema/devices.py.

Run from the abletoolz checkout::

    uv run python scripts/gen_device_vocab.py --corpus DIR [--skip DIR ...]   # writes the module
    uv run python scripts/gen_device_vocab.py --corpus DIR --sample 20 --dry-run  # time a 20-set sample

Every .als under ``--corpus`` is scanned except folders named ``backup`` or
``abletoolz_backup`` and the ``--skip`` folders, plus the version skeletons in
test/version_fixtures.

Every element that is a direct child of a ``Devices`` list is a device, at any
depth (racks nest chains). For each native device tag the census records how
often it was seen, the Live versions it was seen in, and its settings:

* an automatable parameter is an element with a ``Manual`` child; its path is
  recorded relative to the device element and the walk does not descend into it;
* a plain setting is a leaf with a ``Value`` attribute outside any parameter.

Bookkeeping subtrees (pointee/automation ids, view state, sample references) and
nested chains (which hold other devices) are not walked. Hosted plugins and Max
devices are recorded as device tags only.

The raw census goes to ``--json``; the module goes to ``--out``. Neither holds a
set name or path.
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
import time
import xml.etree.ElementTree as ET
import zlib
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

type Version = tuple[int, int, int]


def _load_parse_creator() -> Callable[[str], Version]:
    """``abletoolz.versioning.parse_creator``, loaded from its file.

    versioning.py imports nothing from the package, so loading it alone keeps
    the census independent of whatever else in the checkout is mid-change.
    """
    spec = importlib.util.spec_from_file_location("_census_versioning", REPO_ROOT / "abletoolz" / "versioning.py")
    if spec is None or spec.loader is None:
        raise SystemExit("cannot load abletoolz/versioning.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    parse: Callable[[str], Version] = module.parse_creator
    return parse


parse_creator = _load_parse_creator()

# Live's own backup folders and abletoolz's; the state survey skips the same.
SKIP_DIR_NAMES = {"abletoolz_backup", "backup"}
FIXTURE_GLOB = "test/version_fixtures/skeletons/*.als"
DEFAULT_OUT = REPO_ROOT / "abletoolz" / "schema" / "devices.py"
PRE_82_MAGIC = b"\xab\x1e"

# Devices whose settings live in a hosted plugin's or Max patch's own state.
HOSTED_TAGS = frozenset(
    {"PluginDevice", "AuPluginDevice", "MxDeviceAudioEffect", "MxDeviceInstrument", "MxDeviceMidiEffect"}
)

# Subtrees that are Live's bookkeeping rather than a setting a user changes, and
# nested chains, which hold other devices (walked on their own as devices).
SKIP_TAGS = frozenset(
    {
        # ids and automation plumbing
        "LomId",
        "LomIdView",
        "Pointee",
        "AutomationTarget",
        "ModulationTarget",
        "MidiControllerRange",
        "ArrangerAutomation",
        "ControllerTargets",
        "MpePitchBendUsesTuning",
        # view and editor state
        "ViewData",
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
        # counters and locks Live maintains itself
        "ModulationSourceCount",
        "LockId",
        "LockSeal",
        "ExportCounter",
        "AutogeneratedNameIndex",
        # stored rack snapshots: macro values per variation, not the rack's settings
        "MacroVariations",
    }
)
# View state spelled by pattern: scroll offsets and panel visibility flags.
SKIP_PATTERN = re.compile(r"ScrollPos|Visible$")

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


def widen_range(
    mine: tuple[Version, Version] | None, theirs: tuple[Version, Version] | None
) -> tuple[Version, Version] | None:
    if mine is None:
        return theirs
    if theirs is None:
        return mine
    return min(mine[0], theirs[0]), max(mine[1], theirs[1])


@dataclass(slots=True)
class SettingTally:
    """One setting path under one device tag.

    A few settings change shape across versions (a plain ``Value`` leaf in one,
    a ``Manual`` parameter in another), so each form keeps its own version range.
    """

    count: int = 0
    parameter_versions: tuple[Version, Version] | None = None
    value_versions: tuple[Version, Version] | None = None
    kinds: set[ValueKind] = field(default_factory=set)
    min_version: Version = (99, 0, 0)
    max_version: Version = (0, 0, 0)
    # Highest number seen for each placeholder in the path, outermost first.
    max_indices: list[int] = field(default_factory=list)
    # A bounded sample of distinct values, to show how much a setting varies.
    values: set[str] = field(default_factory=set)

    @property
    def is_parameter(self) -> bool:
        """Whether the newest form seen is an automatable parameter."""
        if self.parameter_versions is None:
            return False
        return self.value_versions is None or self.parameter_versions[1] >= self.value_versions[1]

    @property
    def changed_shape(self) -> bool:
        return self.parameter_versions is not None and self.value_versions is not None

    def add(self, value: str, version: Version, indices: tuple[int, ...], is_parameter: bool) -> None:
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

    def _widen(self, indices: list[int]) -> None:
        if not self.max_indices:
            self.max_indices = indices
        else:
            self.max_indices = [max(mine, theirs) for mine, theirs in zip(self.max_indices, indices, strict=True)]

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


@dataclass(slots=True)
class DeviceTally:
    """One device tag across the corpus."""

    count: int = 0
    set_count: int = 0
    min_version: Version = (99, 0, 0)
    max_version: Version = (0, 0, 0)
    settings: dict[str, SettingTally] = field(default_factory=dict)
    # Templated paths of Id-carrying elements seen with same-tag siblings: real lists.
    id_lists: set[str] = field(default_factory=set)
    # Filled after the scan: list members written without their Id (pre-9.7), not emitted.
    id_less: dict[str, SettingTally] = field(default_factory=dict)

    def see(self, version: Version) -> None:
        self.count += 1
        self.min_version = min(self.min_version, version)
        self.max_version = max(self.max_version, version)

    def merge(self, other: DeviceTally) -> None:
        self.count += other.count
        self.set_count += other.set_count
        self.min_version = min(self.min_version, other.min_version)
        self.max_version = max(self.max_version, other.max_version)
        self.id_lists |= other.id_lists
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
    devices: dict[str, DeviceTally] = field(default_factory=dict)
    skip_reason: str | None = None


@dataclass(slots=True)
class Census:
    """The merged corpus census."""

    sets_scanned: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    versions: Counter[str] = field(default_factory=Counter)
    devices: dict[str, DeviceTally] = field(default_factory=dict)

    def merge(self, result: SetResult) -> None:
        if result.skip_reason is not None:
            self.skipped[result.skip_reason] += 1
            return
        assert result.version is not None
        self.sets_scanned += 1
        self.versions[f"{result.version[0]}.{result.version[1]}"] += 1
        for tag, tally in result.devices.items():
            mine = self.devices.get(tag)
            if mine is None:
                self.devices[tag] = tally
            else:
                mine.merge(tally)


def iter_corpus(root: Path, skip: list[Path]) -> list[Path]:
    """Every .als under ``root``, leaving out backup folders and the ``skip`` folders."""
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
            elif entry.suffix.lower() == ".als":
                found.append(entry)
    return sorted(found)


def placeholder_for(depth: int) -> str:
    """``{index}`` for the outermost list in a path, ``{index2}`` inside it, and so on."""
    return "{index}" if depth == 0 else f"{{index{depth + 1}}}"


def bare_tag(tag: str) -> str:
    """``MacroDisplayNames.3`` -> ``MacroDisplayNames``."""
    numbered = NUMBERED.match(tag)
    return tag if numbered is None else numbered["stem"]


def is_skipped(tag: str) -> bool:
    bare = bare_tag(tag)
    return bare in SKIP_TAGS or SKIP_PATTERN.search(bare) is not None


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


def census_device(device: ET.Element, version: Version, tally: DeviceTally) -> None:
    """Record every parameter and plain setting of one device element."""
    stack: list[tuple[ET.Element, str, tuple[int, ...]]] = [(device, "", ())]
    while stack:
        element, prefix, indices = stack.pop()
        id_tags = Counter(child.tag for child in element if child.get("Id") is not None)
        for tag, n in id_tags.items():
            if n > 1:
                tally.id_lists.add(f"{prefix}{tag}[@Id='{placeholder_for(len(indices))}']")
        for child in element:
            if is_skipped(child.tag):
                continue
            listed = list_segment(child, len(indices))
            if listed is None:
                path, child_indices = f"{prefix}{child.tag}", indices
            else:
                path, child_indices = f"{prefix}{listed[0]}", (*indices, listed[1])
            manual = child.find("Manual")
            if manual is not None:
                record(tally, path, True, manual.get("Value", ""), version, child_indices)
            elif len(child) == 0:
                value = child.get("Value")
                if value is not None:
                    record(tally, path, False, value, version, child_indices)
            else:
                stack.append((child, f"{path}/", child_indices))


def collapse_singletons(tally: DeviceTally) -> None:
    """Un-template ``Tag[@Id=...]`` segments never seen with a same-tag sibling.

    Live stamps an ``Id`` on some single objects too (``SimplerAuxLfo``,
    ``SimplerAuxEnvelope``); the scan templates every ``Id`` element, and only
    the ones the corpus shows repeating stay lists.

    The same list written without ids (Simpler and Sampler parts before Live
    9.7) cannot be reached through an ``[@Id=...]`` path; those paths move to
    ``id_less`` and stay out of the module.
    """
    collapsed: dict[str, SettingTally] = {}
    for path, setting in tally.settings.items():
        segments: list[str] = []
        kept: list[int] = []
        original_prefix = ""
        position = 0
        id_less = False
        for segment in path.split("/"):
            id_less = id_less or f"{original_prefix}{segment}[@Id='{placeholder_for(position)}']" in tally.id_lists
            original = original_prefix + segment
            original_prefix = original + "/"
            if PLACEHOLDER_RE.search(segment) is None:
                segments.append(segment)
                continue
            is_id = segment.endswith("']")
            if is_id and original not in tally.id_lists:
                segments.append(segment.split("[@Id=")[0])
            else:
                segments.append(PLACEHOLDER_RE.sub(placeholder_for(len(kept)), segment))
                kept.append(setting.max_indices[position])
            position += 1
        setting.max_indices = kept
        new_path = "/".join(segments)
        if id_less:
            tally.id_less[new_path] = setting
            continue
        mine = collapsed.get(new_path)
        if mine is None:
            collapsed[new_path] = setting
        else:
            mine.merge(setting)
    tally.settings = collapsed


def record(
    tally: DeviceTally, path: str, is_parameter: bool, value: str, version: Version, indices: tuple[int, ...]
) -> None:
    setting = tally.settings.get(path)
    if setting is None:
        setting = tally.settings[path] = SettingTally()
    setting.add(value, version, indices, is_parameter)


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
                tally = result.devices[device.tag] = DeviceTally(set_count=1)
            tally.see(version)
            if device.tag not in HOSTED_TAGS:
                census_device(device, version, tally)
    return result


def run_census(paths: list[Path], workers: int) -> Census:
    census = Census()
    started = time.perf_counter()
    with multiprocessing.Pool(workers) as pool:
        for done, result in enumerate(pool.imap_unordered(scan_set, paths, chunksize=2), start=1):
            census.merge(result)
            if done % 250 == 0:
                print(f"  {done}/{len(paths)} sets, {time.perf_counter() - started:.0f}s", flush=True)
    for tally in census.devices.values():
        collapse_singletons(tally)
    return census


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
        "distinct_values": distinct if distinct < DISTINCT_CAP else f"{DISTINCT_CAP}+",
        "sample_values": sorted(setting.values)[:8],
    }


def census_json(census: Census) -> dict[str, object]:
    """The raw census for inspection. Sample values can hold user text (sample names): keep it local."""
    return {
        "sets_scanned": census.sets_scanned,
        "skipped": dict(census.skipped),
        "versions": dict(sorted(census.versions.items())),
        "devices": {
            tag: {
                "count": tally.count,
                "set_count": tally.set_count,
                "min_version": version_text(tally.min_version),
                "max_version": version_text(tally.max_version),
                "hosted": tag in HOSTED_TAGS,
                "settings": {path: setting_json(s) for path, s in sorted(tally.settings.items())},
                "id_less_settings": {path: setting_json(s) for path, s in sorted(tally.id_less.items())},
            }
            for tag, tally in sorted(census.devices.items())
        },
    }


# --------------------------------------------------------------------------- emit

# Structural wrappers that say nothing about which setting a path names.
FILLER_WORDS = frozenset({"Slot", "Value"})
# The repo's ruff line length; longer member lines are wrapped the way ruff format wraps them.
LINE_LENGTH = 120
# In a clash, only a setting at most this many words deep may keep the bare name
# (`VolumeAndPan/Volume` is Sampler.VOLUME; a sub-oscillator `Mode` four words down is not MODE).
BARE_NAME_MAX_DEPTH = 2

MODULE_DOC = '''"""Live's native device tags and the settings inside each, measured from real sets.

GENERATED by scripts/gen_device_vocab.py; do not edit by hand. Regenerate from the
abletoolz checkout with ``uv run python scripts/gen_device_vocab.py --corpus DIR`` (it scans a
local corpus of sets; nothing about the sets themselves lands here).

Census: {sets} sets saved by Live {low} to {high}; {devices} devices.

``Device`` names every tag seen as a direct child of a ``Devices`` list, racks
walked into. Member names are Live's display names where the tag differs
(``MultiSampler`` is ``SAMPLER``, ``OriginalSimpler`` is ``SIMPLER``,
``StereoGain`` is ``UTILITY``). Hosted plugins and Max devices are listed but
have no settings enum: their state is the plugin's own.

Each native device has a settings ``StrEnum`` named after its ``Device`` member
(``Device.SAMPLER`` -> ``Sampler``). A value is the setting's path relative to
the device element, as ``device_element.find(path)`` takes it:

* automatable parameters (``is_parameter(member)``) name an element whose
  ``Manual`` child holds the value;
* every other member is a leaf whose own ``Value`` attribute holds it.

A setting can change shape between versions (a plain value that later became a
parameter); ``is_parameter`` follows the newest form and the member's comment
gives both version ranges, so a reader should take ``Manual`` when the element
has one and the element's own ``Value`` otherwise.

``SETTING_KIND``, ``PARAMETERS`` and ``TEMPLATED`` are keyed by settings class
first. A ``StrEnum`` member is equal to its string, so ``EqEight.MODE`` and
another device's ``"Mode"`` member would be one key in a flat table; use
``kind_of``, ``is_parameter`` and ``is_templated`` rather than indexing by hand.

A member is named by the last path segment. When several paths end the same
way, the shallowest keeps the bare name if it sits at most two segments deep,
and the rest take the shortest path suffix that is unique (``Slot`` and
``Value`` wrappers left out), so
``Sampler.VOLUME`` is the amp volume and ``Sampler.MULTI_SAMPLE_PART_VOLUME``
the per-sample-part one.

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
was seen in, so a setting added, renamed or removed shows as a range that
starts late or ends early.
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


def path_words(path: str) -> list[str]:
    """Segments with list syntax and wrapper words removed.

    ``Filter/Slot/Value/SimplerFilter/Freq`` -> Filter, SimplerFilter, Freq;
    ``Bands.{index}/ParameterA/Freq`` -> Bands, ParameterA, Freq.
    """
    segments = [PLACEHOLDER_RE.sub("", segment).split("[@Id=")[0].rstrip(".") for segment in path.split("/")]
    return [word for word in segments[:-1] if word not in FILLER_WORDS] + segments[-1:]


def setting_member_names(settings: dict[str, SettingTally]) -> dict[str, str]:
    """Name each path by its shortest unique suffix; the shallowest of a clash keeps the bare name.

    Two paths that still clash at full length are one setting Live renamed
    (``SideChainEq/Gain`` became ``SideChainEq_Gain`` in 12): the newer spelling
    keeps the name and the older one gets ``_UNTIL_<last major seen>``.
    """
    paths = sorted(settings)
    words = {path: path_words(path) for path in paths}
    by_last: dict[str, list[str]] = {}
    for path in paths:
        by_last.setdefault(upper_snake(words[path][-1]), []).append(path)
    keeps_bare: set[str] = set()
    for group in by_last.values():
        depths = sorted(len(words[path]) for path in group)
        if len(group) == 1 or (depths[0] < depths[1] and depths[0] <= BARE_NAME_MAX_DEPTH):
            keeps_bare.add(min(group, key=lambda path: len(words[path])))
    # Spellings of one renamed setting all take the full name, so the pair reads as a pair.
    by_full: dict[str, list[str]] = {}
    for path in paths:
        by_full.setdefault(upper_snake("_".join(words[path])), []).append(path)
    renamed = {path for group in by_full.values() if len(group) > 1 for path in group}
    keeps_bare -= renamed
    suffix = {path: len(words[path]) if path in renamed else 1 if path in keeps_bare else 2 for path in paths}
    superseded: set[str] = set()
    while True:
        names = {path: upper_snake("_".join(words[path][-suffix[path] :])) for path in paths}
        for path in superseded:
            names[path] += f"_UNTIL_{settings[path].max_version[0]}"
        seen = Counter(names.values())
        clashing = [path for path in paths if seen[names[path]] > 1 and path not in keeps_bare | superseded]
        if not clashing:
            if len(seen) != len(paths):
                raise SystemExit(f"member names collide: {[name for name, n in seen.items() if n > 1]}")
            return names
        for path in clashing:
            if suffix[path] < len(words[path]):
                suffix[path] += 1
                continue
            rivals = [other for other in paths if other != path and names[other] == names[path]]
            if any(suffix[other] < len(words[other]) for other in rivals):
                continue
            mine = settings[path]
            newer = [other for other in rivals if settings[other].min_version > mine.max_version]
            older = [other for other in rivals if settings[other].max_version < mine.min_version]
            if len(newer) + len(older) != len(rivals):
                raise SystemExit(f"no unique member name for {path!r}: seen alongside {rivals}")
            if newer:
                superseded.add(path)


def version_range(low: Version, high: Version) -> str:
    return version_text(low) if low == high else f"{version_text(low)}-{version_text(high)}"


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


def emit_module(census: Census) -> str:
    device_members = {tag: member_name(tag) for tag in census.devices}
    dupes = [name for name, n in Counter(device_members.values()).items() if n > 1]
    if dupes:
        raise SystemExit(f"device member names collide: {dupes}")
    native = sorted((tag for tag in census.devices if tag not in HOSTED_TAGS), key=lambda t: device_members[t])
    versions = sorted(v for tally in census.devices.values() for v in (tally.min_version, tally.max_version))

    lines = [
        MODULE_DOC.format(
            sets=census.sets_scanned,
            low=version_text(versions[0]),
            high=version_text(versions[-1]),
            devices=sum(tally.count for tally in census.devices.values()),
        ).rstrip("\n"),
        "",
        "from __future__ import annotations",
        "",
        "import enum",
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

    kind_lines: list[str] = []
    parameter_lines: list[str] = []
    templated_lines: list[str] = []
    settings_of_lines: list[str] = []
    for tag in native:
        tally = census.devices[tag]
        class_name = camel(device_members[tag])
        names = setting_member_names(tally.settings)
        if not names:
            raise SystemExit(f"{tag} has no settings; a settings enum would be empty")
        docstring = f'    """Settings of ``Device.{device_members[tag]}``."""'
        lines += ["", "", f"class {class_name}(enum.StrEnum):", docstring, ""]
        kind_lines.append(f"    {class_name}: {{")
        parameter_lines.append(f"    {class_name}: frozenset(")
        parameter_lines.append("        {")
        templated_lines.append(f"    {class_name}: frozenset(")
        templated_lines.append("        {")
        for path in sorted(names, key=names.__getitem__):
            setting = tally.settings[path]
            member = f"{class_name}.{names[path]}"
            lines.append(setting_comment(setting))
            assignment = f'    {names[path]} = "{path}"'
            if len(f'        "{path}"') > LINE_LENGTH:
                assignment += "  # noqa: E501 - the path alone is longer than a line"
            elif len(assignment) > LINE_LENGTH:
                assignment = f'    {names[path]} = (\n        "{path}"\n    )'
            lines.append(assignment)
            kind_lines.append(f"        {member}: ValueKind.{resolve_kind(setting.kinds).name},")
            if setting.is_parameter:
                parameter_lines.append(f"            {member},")
            if PLACEHOLDER_RE.search(path):
                templated_lines.append(f"            {member},")
        kind_lines.append("    },")
        for table in (parameter_lines, templated_lines):
            if table[-1] == "        {":
                del table[-2:]
                table.append(f"    {class_name}: frozenset(),")
            else:
                table += ["        }", "    ),"]
        settings_of_lines.append(f"    Device.{device_members[tag]}: {class_name},")

    lines += [
        "",
        "",
        "SETTINGS_OF: dict[Device, type[enum.StrEnum]] = {",
        *settings_of_lines,
        "}",
        '"""The settings enum of every native device."""',
        "",
        "SETTING_KIND: dict[type[enum.StrEnum], dict[enum.StrEnum, ValueKind]] = {",
        *kind_lines,
        "}",
        '"""Per settings class: what every value seen for each setting parses as."""',
        "",
        "PARAMETERS: dict[type[enum.StrEnum], frozenset[enum.StrEnum]] = {",
        *parameter_lines,
        "}",
        '"""Per settings class: automatable settings, whose element holds the value in a ``Manual`` child."""',
        "",
        "TEMPLATED: dict[type[enum.StrEnum], frozenset[enum.StrEnum]] = {",
        *templated_lines,
        "}",
        '"""Per settings class: settings whose path holds ``{index}`` placeholders to fill before ``find``."""',
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", type=Path, required=True, help="folder of .als sets, walked recursively")
    parser.add_argument("--skip", type=Path, action="append", default=[], help="a folder under --corpus to leave out")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--sample", type=int, default=0, help="scan a random sample of this many sets")
    parser.add_argument("--json", type=Path, help="write the raw census here")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--dry-run", action="store_true", help="census only, do not write the module")
    args = parser.parse_args()

    started = time.perf_counter()
    paths = iter_corpus(args.corpus, args.skip) + sorted(REPO_ROOT.glob(FIXTURE_GLOB))
    listed = time.perf_counter() - started
    total = len(paths)
    if args.sample:
        paths = random.Random(0).sample(paths, min(args.sample, total))
    print(f"{total} sets found in {listed:.1f}s; scanning {len(paths)} with {args.workers} workers")

    census = run_census(paths, args.workers)
    elapsed = time.perf_counter() - started
    print(f"scanned {census.sets_scanned}, skipped {sum(census.skipped.values())} {dict(census.skipped)}")
    print(f"{len(census.devices)} device tags, {sum(d.count for d in census.devices.values())} devices")
    print(f"elapsed {elapsed:.1f}s")
    if args.sample:
        print(f"extrapolated full run: {elapsed / len(paths) * total:.0f}s")

    if args.json is not None:
        args.json.write_text(json.dumps(census_json(census), indent=1), encoding="utf-8")
        print(f"census -> {args.json}")
    if not args.dry_run:
        args.out.write_text(emit_module(census), encoding="utf-8", newline="\n")
        print(f"module -> {args.out}")


if __name__ == "__main__":
    main()
