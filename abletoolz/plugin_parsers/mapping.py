"""Which plugin a broken plugin becomes, guessed from what this machine has.

Repair cannot invent a mapping table: a wrong class id does not fail, it makes
Live silently load a different plugin. So the table stays hand written, and this
module's job is to do the tedious part of writing it -- read the local plugin
database, pair the names that look like the same plugin in two formats, and hand
the user a YAML file to read, prune and merge.

Nothing here decides anything. Every suggestion is emitted commented out --
including the ones that look certain -- so enabling one is something the user
does on purpose, by deleting a "# ", after checking it in Live. A file that
quietly turned on twenty mappings would be a file nobody reads.

Which way a suggestion points
-----------------------------
Toward something installed. A mapping only helps if its target is a plugin this
machine actually has, so both formats are treated as sources and as targets: a
VST2 whose VST3 is installed suggests ``vst -> vst3``, and a VST3 whose VST2 is
installed suggests ``vst3 -> vst``. Where both directions describe the same
pair, the one aiming at the VST3 wins -- that is the format Live is happiest
with and the only pair that can be translated today. A suggestion pointing the
other way is still written down, marked "no translator yet", because knowing
what a device should become is worth recording before anything can do it.

The tiers, and what each is worth
---------------------------------
:attr:`MatchTier.EXACT` is the same name in both formats. :attr:`NORMALIZED` is
the same name minus a bitness marker ("Serum_x64" against "Serum") or minus the
vendor's first word ("FabFilter Pro-Q 3" against "Pro-Q 3") -- the two shapes
every hand-checked mapping in
:data:`~abletoolz.plugin_parsers.format_translation.KNOWN_TRANSLATIONS` already
has. :attr:`FUZZY` is a name similarity score, which is a place to start looking
and nothing more. The tier is written into the comment; it no longer decides
anything, because nothing here decides anything.

Vendor is what stops the whole thing being dangerous. Live's plugin database
carries a vendor for every row of both formats -- measured 2026-08-12, 910 VST2
and 390 VST3 rows, none of them blank -- and the normalization that turns
"FabFilter Pro-Q 3" into "Pro-Q 3" also turns "Midnight Compressor" into
"Compressor", which is a real and completely unrelated Kilohearts plugin. Same
name, different vendor: the comment says so in capitals. So does a version
migration ("Pro-Q 2" against "Pro-Q 3"), which is a different plugin that will
not read the old one's patch.

A vendor is compared by who they are, not by how the database spelled them that
day -- see :func:`normalize_vendor`.

Name matching is the identity axis, and identity is only two thirds of a
conversion. Whether the patch survives is the third, so every suggestion also
carries what :data:`~abletoolz.plugin_parsers.state.MEASURED_STATE` knows about
that plugin -- measured by ear, inferred from the bytes, or nothing at all. Two
plugins can be certainly the same plugin and still need a listen, and a comment
that said only "exact" would hide exactly that.
"""

from __future__ import annotations

import dataclasses
import datetime
import difflib
import enum
import json
import logging
import pathlib
import re
from collections.abc import Iterable, Mapping, Sequence

from abletoolz.plugin_parsers.base import PluginKind
from abletoolz.plugin_parsers.config import AbletoolzConfig, get_config_path
from abletoolz.plugin_parsers.format_translation import KNOWN_TRANSLATIONS, has_translator
from abletoolz.plugin_parsers.plugin_db import (
    FORMAT_LABELS,
    PluginDatabase,
    PluginEntry,
    SourceCount,
    load_plugin_db,
)
from abletoolz.plugin_parsers.state import UNMEASURED, MeasuredState, measured_state

logger = logging.getLogger(__name__)

# Below this, difflib's ratio stops meaning anything on names this short --
# "Driver" and "Diver" score 0.73, "Driver" and "Delay" 0.36.
FUZZY_CUTOFF = 0.72

# How many runner-up names a fuzzy suggestion names in its comment.
_ALTERNATE_LIMIT = 3

# The formats a mapping can run between, in the order a file lists them.
_MAPPED_FORMATS = (PluginKind.VST, PluginKind.VST3)

# The directions tried, in preference order. A pair that can be described both
# ways is written down once, aiming at the VST3 -- so the reverse direction only
# ever produces an entry for a plugin the forward pass did not already cover.
_DIRECTIONS = ((PluginKind.VST, PluginKind.VST3), (PluginKind.VST3, PluginKind.VST))


# -- names ------------------------------------------------------------------
# The one implementation of what two plugin names have to have in common. Repair
# suggests a config line with it (see :mod:`abletoolz.plugin_parsers.repair`)
# and the suggester tiers with it, so the two can never drift apart.

# Measured on installed VST2 file names: a plugin's VST2 is routinely the VST3
# name plus a bitness marker, stackable ("Thing_x64.64").
_NAME_SUFFIXES = (".64", "_x64", " x64", "(x64)", " (x64)", "-64bit")

# A trailing major version, the way plugin names spell one: "Pro-Q 3", "Mini V4".
_TRAILING_VERSION = re.compile(r"\s*[vV]?(\d+)$")


def strip_bitness(name: str) -> str:
    """Drop every trailing bitness marker from a plugin name."""
    stripped = name
    changed = True
    while changed:
        changed = False
        for suffix in _NAME_SUFFIXES:
            if len(stripped) > len(suffix) and stripped.casefold().endswith(suffix.casefold()):
                stripped = stripped[: -len(suffix)].rstrip()
                changed = True
    return stripped


def name_variants(name: str) -> tuple[str, ...]:
    """The name itself and the ways the other format might spell it, best first.

    Two rewrites, both measured on real pairs: a VST2 carries a bitness marker
    its VST3 drops ("Serum_x64" against "Serum"), and a VST2 carries the vendor
    as a first word its VST3 drops ("FabFilter Pro-Q 3" against "Pro-Q 3").
    """
    found: list[str] = []
    for base in (name, strip_bitness(name)):
        for candidate in (base, base.split(" ", 1)[1] if " " in base else base):
            if candidate and candidate not in found:
                found.append(candidate)
    return tuple(found)


def normalized_keys(name: str) -> frozenset[str]:
    """Every spelling of ``name`` two plugins are allowed to agree on."""
    return frozenset(variant.casefold() for variant in name_variants(name))


def split_trailing_version(name: str) -> tuple[str, str | None]:
    """A name split into its stem and its trailing version number, if it has one."""
    matched = _TRAILING_VERSION.search(name)
    if matched is None:
        return name, None
    return name[: matched.start()].rstrip(), matched.group(1)


def is_migration(source_name: str, target_name: str) -> bool:
    """Whether two names differ only by a trailing version number.

    "FabFilter Pro-Q 2" against "Pro-Q 3" is not a format translation, it is an
    upgrade to a different plugin that will not read the old one's patch.
    """
    for left in name_variants(source_name):
        left_stem, left_version = split_trailing_version(left)
        for right in name_variants(target_name):
            right_stem, right_version = split_trailing_version(right)
            if left_stem.casefold() == right_stem.casefold() and left_version != right_version:
                return True
    return False


def suggest_target_name(source_name: str, known_names: Iterable[str]) -> str | None:
    """A plausible target name for an unmapped plugin, or None.

    A guess and nothing more. "Midnight Compressor" reduces to "Compressor",
    which is a real and completely unrelated Kilohearts plugin, so this never
    decides anything -- it only offers the user a line to paste.
    """
    names = set(known_names)
    for candidate in name_variants(source_name):
        if candidate in names:
            return candidate
    return None


def suggestion_line(source_name: str, to: PluginKind, target_name: str) -> str:
    """The config entry that would map ``source_name``, ready to paste.

    The target format is always written out, even though ``vst3`` is what an
    entry defaults to, because the entry is the only thing that says which way a
    translation runs and a user reading the file should not have to know that.
    """
    return f"{json.dumps(source_name)}: {{to: {to.value}, name: {json.dumps(target_name)}}}"


def similarity(left: str, right: str) -> float:
    """How alike two plugin names are, ignoring case. 1.0 is identical."""
    return difflib.SequenceMatcher(None, left.casefold(), right.casefold()).ratio()


# -- vendors ----------------------------------------------------------------

# A company's legal form is not part of who made the plugin. Measured on Live's
# own database 2026-08-12: it stores "Native Instruments GmbH" against a VST2 row
# and "Native Instruments" against that same plugin's VST3 row, and iZotope is
# "iZotope, Inc." on some rows and "iZotope" on others. Comparing the raw strings
# calls twelve identical-name pairs a mismatch.
_LEGAL_SUFFIXES = frozenset({"gmbh", "inc", "llc", "ltd", "co", "corp", "kg", "ab"})

# What is left dangling once a suffix comes off: "iZotope, Inc." -> "izotope,".
_VENDOR_PUNCTUATION = " ,."


def normalize_vendor(vendor: str) -> str:
    """A vendor reduced to who they are, without their legal form."""
    stripped = vendor.casefold().strip(_VENDOR_PUNCTUATION)
    while " " in stripped:
        head, _, last = stripped.rpartition(" ")
        if last not in _LEGAL_SUFFIXES:
            break
        stripped = head.strip(_VENDOR_PUNCTUATION)
    return stripped


# -- what a suggestion is ---------------------------------------------------


class MatchTier(enum.StrEnum):
    """How a source name was paired with a target name, best evidence first."""

    EXACT = "exact"
    NORMALIZED = "normalized"
    FUZZY = "fuzzy"


class VendorAgreement(enum.StrEnum):
    """What the two plugins' vendors say about a pairing."""

    AGREES = "agrees"
    UNKNOWN = "unknown"
    MISMATCH = "mismatch"


@dataclasses.dataclass(frozen=True)
class Candidate:
    """A runner-up target name and how alike the two names are."""

    name: str
    score: float


@dataclasses.dataclass(frozen=True)
class MappingSuggestion:
    """One installed plugin paired with what it probably becomes in the other format."""

    source_name: str
    source_kind: PluginKind
    target_name: str
    target_kind: PluginKind
    tier: MatchTier
    score: float
    source_vendor: str | None
    target_vendor: str | None
    vendor: VendorAgreement
    migration: bool
    translator: bool
    state: MeasuredState = UNMEASURED
    alternates: tuple[Candidate, ...] = ()

    @property
    def direction(self) -> str:
        """The pair this entry would translate, as the comment spells it."""
        return f"{FORMAT_LABELS[self.source_kind]} -> {FORMAT_LABELS[self.target_kind]}"

    @property
    def annotation(self) -> str:
        """The trailing comment explaining what this suggestion rests on."""
        parts = [str(self.tier), self.direction]
        if self.tier is not MatchTier.EXACT:
            parts.append(f"score={self.score:.2f}")
        if self.vendor is VendorAgreement.MISMATCH:
            parts.append(f"vendor MISMATCH: {self.source_vendor} vs {self.target_vendor}")
        elif self.vendor is VendorAgreement.AGREES:
            parts.append(f"vendor {self.source_vendor}")
        else:
            parts.append("vendor unknown")
        if self.migration:
            parts.append("MIGRATION: a version change, not a format change")
        if not self.translator:
            parts.append("no translator yet")
        parts.append(self.state.annotation)
        if self.alternates:
            others = ", ".join(f"{candidate.name} ({candidate.score:.2f})" for candidate in self.alternates)
            parts.append(f"also considered {others}")
        return ", ".join(parts)

    @property
    def entry(self) -> str:
        """The ``plugin_translation.targets`` line this suggestion would become."""
        return suggestion_line(self.source_name, self.target_kind, self.target_name)


@dataclasses.dataclass(frozen=True)
class SuggestionReport:
    """Everything one machine survey found, suggestion by suggestion."""

    database: PluginDatabase
    suggestions: tuple[MappingSuggestion, ...]
    already_mapped: tuple[str, ...]
    unmatched: tuple[str, ...]

    def by_tier(self, tier: MatchTier) -> tuple[MappingSuggestion, ...]:
        return tuple(suggestion for suggestion in self.suggestions if suggestion.tier is tier)

    @property
    def vendor_mismatch_count(self) -> int:
        return sum(1 for suggestion in self.suggestions if suggestion.vendor is VendorAgreement.MISMATCH)

    @property
    def migration_count(self) -> int:
        return sum(1 for suggestion in self.suggestions if suggestion.migration)

    @property
    def untranslatable_count(self) -> int:
        """Suggestions whose direction nothing can translate yet."""
        return sum(1 for suggestion in self.suggestions if not suggestion.translator)

    @property
    def measured_state_count(self) -> int:
        """Suggestions naming a plugin whose state rung somebody has measured."""
        return sum(1 for suggestion in self.suggestions if suggestion.state.predictable)

    def installed(self, kind: PluginKind) -> tuple[PluginEntry, ...]:
        return self.database.installed(kind)

    def inventory(self, kind: PluginKind) -> tuple[SourceCount, ...]:
        """How many plugins of ``kind`` each place in the database contributed."""
        return tuple(count for count in self.database.counts() if count.kind is kind)


# -- pairing ----------------------------------------------------------------


def _agreement(source_vendor: str | None, target_vendor: str | None) -> VendorAgreement:
    """What two vendors say about a pairing. Missing either way says nothing."""
    if not source_vendor or not target_vendor:
        return VendorAgreement.UNKNOWN
    source, target = normalize_vendor(source_vendor), normalize_vendor(target_vendor)
    if not source or not target:
        return VendorAgreement.UNKNOWN
    return VendorAgreement.AGREES if source == target else VendorAgreement.MISMATCH


def _pair(
    source: PluginEntry,
    target: PluginEntry,
    tier: MatchTier,
    alternates: tuple[Candidate, ...] = (),
) -> MappingSuggestion:
    return MappingSuggestion(
        source_name=source.name,
        source_kind=source.kind,
        target_name=target.name,
        target_kind=target.kind,
        tier=tier,
        score=similarity(source.name, target.name),
        source_vendor=source.vendor,
        target_vendor=target.vendor,
        vendor=_agreement(source.vendor, target.vendor),
        migration=is_migration(source.name, target.name),
        translator=has_translator(source.kind, target.kind),
        state=measured_state(source.name, target.name),
        alternates=alternates,
    )


def _preferred(source: PluginEntry, candidates: Sequence[PluginEntry]) -> PluginEntry:
    """Of several targets sharing a normalized name, the least surprising one."""
    return min(
        candidates,
        key=lambda target: (
            _agreement(source.vendor, target.vendor) is VendorAgreement.MISMATCH,
            is_migration(source.name, target.name),
            -similarity(source.name, target.name),
            target.name,
        ),
    )


def _suggest_one(
    source: PluginEntry,
    by_name: Mapping[str, PluginEntry],
    by_key: Mapping[str, Sequence[PluginEntry]],
    by_folded_name: Mapping[str, PluginEntry],
    cutoff: float,
) -> MappingSuggestion | None:
    """The best target for one plugin, or None when nothing is close enough."""
    exact = by_name.get(source.name)
    if exact is not None:
        return _pair(source, exact, MatchTier.EXACT)

    normalized: list[PluginEntry] = []
    for key in normalized_keys(source.name):
        for target in by_key.get(key, ()):
            if target not in normalized:
                normalized.append(target)
    if normalized:
        return _pair(source, _preferred(source, normalized), MatchTier.NORMALIZED)

    close = difflib.get_close_matches(
        source.name.casefold(), list(by_folded_name), n=_ALTERNATE_LIMIT + 1, cutoff=cutoff
    )
    if not close:
        return None
    scored = sorted(
        ((by_folded_name[folded], similarity(source.name, by_folded_name[folded].name)) for folded in close),
        key=lambda pair: (-pair[1], pair[0].name),
    )
    best = scored[0][0]
    alternates = tuple(Candidate(target.name, score) for target, score in scored[1 : _ALTERNATE_LIMIT + 1])
    return _pair(source, best, MatchTier.FUZZY, alternates)


def _indexes(
    targets: Sequence[PluginEntry],
) -> tuple[dict[str, PluginEntry], dict[str, list[PluginEntry]], dict[str, PluginEntry]]:
    """One pool of targets, indexed the three ways the tiers ask about it."""
    by_name: dict[str, PluginEntry] = {}
    by_key: dict[str, list[PluginEntry]] = {}
    by_folded_name: dict[str, PluginEntry] = {}
    for target in targets:
        by_name.setdefault(target.name, target)
        by_folded_name.setdefault(target.name.casefold(), target)
        for key in normalized_keys(target.name):
            by_key.setdefault(key, []).append(target)
    return by_name, by_key, by_folded_name


def suggest_mappings(
    database: PluginDatabase,
    *,
    mapped: Iterable[str] = (),
    cutoff: float = FUZZY_CUTOFF,
) -> SuggestionReport:
    """Pair every unmapped installed plugin with what it becomes in the other format.

    Runs once per direction and keeps the VST3-aiming one where both describe
    the same pair, so a plugin installed in both formats is suggested as
    ``vst -> vst3`` and not twice. ``mapped`` is every source name some table
    already answers for -- the seed table plus the user's config -- and those are
    counted, never re-suggested.
    """
    already = set(mapped)
    pools = {kind: database.installed(kind) for kind in _MAPPED_FORMATS}

    suggestions: list[MappingSuggestion] = []
    already_mapped: list[str] = []
    unmatched: list[str] = []
    # Pairs already written down, as {source name, target name}: the reverse of a
    # kept suggestion describes the same two plugins and adds nothing.
    covered: set[frozenset[str]] = set()

    for source_kind, target_kind in _DIRECTIONS:
        by_name, by_key, by_folded_name = _indexes(pools[target_kind])
        for source in pools[source_kind]:
            if source.name in already:
                already_mapped.append(source.name)
                continue
            suggestion = _suggest_one(source, by_name, by_key, by_folded_name, cutoff)
            if suggestion is None:
                unmatched.append(source.name)
                continue
            # Aiming at a plugin some entry already maps away from would point at
            # the thing the user is migrating off, so that pair is done with too.
            pair = frozenset({suggestion.source_name, suggestion.target_name})
            if pair in covered or suggestion.target_name in already:
                continue
            covered.add(pair)
            suggestions.append(suggestion)

    return SuggestionReport(database, tuple(suggestions), tuple(already_mapped), tuple(unmatched))


def survey_machine(config: AbletoolzConfig, *, db_path: pathlib.Path | None = None) -> SuggestionReport:
    """Read this machine's plugin database and suggest the mappings it is missing.

    The database is the only thing read here. Building one takes a full scan, so
    a machine that has run ``--plugin-db`` answers from the file; one that has
    not gets a database built in memory through the same code path.
    """
    database = load_plugin_db(config, db_path)
    mapped = set(KNOWN_TRANSLATIONS) | set(config.plugin_translation_targets)
    logger.debug("Surveying %s plugin record(s) against %s mapped names", len(database.plugins), len(mapped))
    return suggest_mappings(database, mapped=mapped)


# -- the file the user merges -----------------------------------------------


def default_suggestions_path() -> pathlib.Path:
    """Where suggestions land when the flag is given no path: beside config.yaml."""
    return get_config_path().parent / "suggested_plugin_mappings.yaml"


# What each tier group says for itself, above the entries in it.
_TIER_NOTES: dict[MatchTier, tuple[str, ...]] = {
    MatchTier.EXACT: ("The same name in both formats.",),
    MatchTier.NORMALIZED: (
        "The same name once a bitness marker or the vendor's first word comes off",
        '("Serum_x64" against "Serum", "FabFilter Pro-Q 3" against "Pro-Q 3").',
    ),
    MatchTier.FUZZY: (
        "Names that merely look alike. A similarity score is a place to start",
        "looking, not evidence -- check every one of these in Live first.",
    ),
}


def render_targets_yaml(report: SuggestionReport, *, generated: str | None = None) -> str:
    """The reviewable YAML file. Every entry commented out, grouped by tier.

    Uncommenting a line is the whole enabling step, so each line has to be a
    valid config entry exactly as written, indented where it already sits.
    """
    when = generated if generated is not None else datetime.date.today().isoformat()
    counts = ", ".join(str(count) for kind in _MAPPED_FORMATS for count in report.inventory(kind))
    installed = sum(len(report.installed(kind)) for kind in _MAPPED_FORMATS)
    lines = [
        f"# Plugin mappings abletoolz suggests for this machine, {when}.",
        "#",
        f"# Read from {installed} installed plugin(s) in the local plugin database ({counts}).",
        f"# {len(report.already_mapped)} name(s) are already mapped and were skipped;"
        f" {len(report.unmatched)} had no candidate at all.",
        "#",
        "# NOTHING HERE IS IN FORCE, AND NOTHING TURNS ITSELF ON. Every suggestion below is",
        "# commented out, including the ones that look certain. To use one: check in Live",
        '# that the two plugins really are the same plugin, delete the leading "# ", and',
        f"# move the line into {get_config_path()}. Deciding is the part that is yours.",
        "#",
        "# No uid is given anywhere below, and none is needed: --repair-plugins looks a",
        "# target's class id up by name at run time, from the local plugin database. Add",
        "# one by hand only for a plugin nothing on this machine knows.",
        "#",
        '# Each comment ends with what is known about that plugin\'s patch. "state:',
        '# verbatim (ear <date>)" is a conversion somebody has listened to; "(structural)"',
        '# is one the bytes look right for; "state: unknown" is an experiment -- convert a',
        f"# copy and audition it. {len(report.suggestions) - report.measured_state_count}"
        f" of the {len(report.suggestions)} below are experiments.",
        "plugin_translation:",
        "  targets:",
    ]
    for tier in MatchTier:
        found = report.by_tier(tier)
        lines.append("")
        lines.append(f"    # -- {tier.value} ({len(found)}) " + "-" * (56 - len(tier.value)))
        lines.extend(f"    # {note}" for note in _TIER_NOTES[tier])
        if not found:
            lines.append("    # (nothing)")
        lines.extend(f"    # {suggestion.entry}  # {suggestion.annotation}" for suggestion in found)
    lines.append("")
    return "\n".join(lines)
