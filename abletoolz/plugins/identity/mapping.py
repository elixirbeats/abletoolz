"""Which installed plugin an unmapped plugin probably is, guessed by name.

A plan offers these guesses as experiments for a plugin abletoolz has not
mapped, and never picks one for you: a wrong guess does not fail, it makes Live
load a different plugin. :func:`best_match` pairs a plugin's name with the
installed plugin of a target format that looks most like it, and says how sure
that is and what it rests on.

The tiers, and what each is worth
---------------------------------
:attr:`MatchTier.EXACT` is the same name in both formats. :attr:`NORMALIZED` is
the same name minus a bitness marker ("Serum_x64" against "Serum") or minus the
vendor's first word ("FabFilter Pro-Q 3" against "Pro-Q 3") -- the two shapes
the hand-checked plugin maps already have. :attr:`FUZZY` is a name similarity
score, which is a place to start looking and nothing more.

Vendor is what stops the whole thing being dangerous. Live's plugin database
carries a vendor for every row of both formats -- measured 2026-08-12, 910 VST2
and 390 VST3 rows, none of them blank -- and an Audio Unit device stores its
manufacturer, while the normalization that turns "FabFilter Pro-Q 3" into
"Pro-Q 3" also turns "Midnight Compressor" into "Compressor", which is a real
and completely unrelated Kilohearts plugin. Same name, different vendor: no
guess. Where either vendor is unknown -- a plugin Live's database has no row for
-- the guess says so. A version migration ("Pro-Q 2" against "Pro-Q 3") is a
different plugin that will not read the old one's patch, and the annotation
says so in capitals.

A vendor is compared by who they are, not by how the database spelled them that
day -- see :func:`normalize_vendor`.

Name matching is the identity axis, and identity is only two thirds of a
conversion. Whether the patch survives is the third, so every guess also
carries what is known about that plugin's patch -- its plugin map's evidence for
the target format, a measured migration, or nothing at all.
"""

from __future__ import annotations

import dataclasses
import difflib
import enum
import re
from collections.abc import Mapping

from abletoolz.plugins.identity.ids import PluginKind
from abletoolz.plugins.identity.plugin_db import FORMAT_LABELS

# Below this, difflib's ratio stops meaning anything on names this short --
# "Driver" and "Diver" score 0.73, "Driver" and "Delay" 0.36.
FUZZY_CUTOFF = 0.72

# How many runner-up names a fuzzy guess names in its comment.
_ALTERNATE_LIMIT = 3


# -- names ------------------------------------------------------------------
# The one implementation of what two plugin names have to have in common.

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
    and the vendor as a first word, and its VST3 drops both.
    """
    found: list[str] = []
    for base in (name, strip_bitness(name)):
        for candidate in (base, base.split(" ", 1)[1] if " " in base else base):
            if candidate and candidate not in found and (candidate == name or _names_a_plugin(candidate)):
                found.append(candidate)
    return tuple(found)


def _names_a_plugin(candidate: str) -> bool:
    """Whether a shortened name still says which plugin it is.

    Dropping the first word of "Discord 3" leaves "3", which "Volcano 3" also
    reduces to: a version number names no plugin.
    """
    return len(candidate) >= 3 and any(char.isalpha() for char in candidate)


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


# -- what a guess is --------------------------------------------------------


class MatchTier(enum.StrEnum):
    """How a source name was paired with a target name, best evidence first."""

    EXACT = "exact"
    NORMALIZED = "normalized"
    FUZZY = "fuzzy"


@dataclasses.dataclass(frozen=True)
class Candidate:
    """A runner-up target name and how alike the two names are."""

    name: str
    score: float


@dataclasses.dataclass(frozen=True)
class PluginGuess:
    """One installed plugin paired with what it probably becomes in the other format.

    ``vendor`` is the vendor both plugins agree on, as the source's own record
    spells it; None when either vendor is unknown.
    """

    source_name: str
    source_kind: PluginKind
    target_name: str
    target_kind: PluginKind
    tier: MatchTier
    score: float
    vendor: str | None
    migration: bool
    alternates: tuple[Candidate, ...] = ()

    @property
    def direction(self) -> str:
        """The pair this entry would translate, as the comment spells it."""
        return f"{FORMAT_LABELS[self.source_kind]} -> {FORMAT_LABELS[self.target_kind]}"

    @property
    def annotation(self) -> str:
        """What this guess rests on, as a plan shows it beside the target."""
        parts = [str(self.tier), self.direction]
        if self.tier is not MatchTier.EXACT:
            parts.append(f"score={self.score:.2f}")
        parts.append(f"vendor {self.vendor}" if self.vendor else "vendor unknown")
        if self.migration:
            parts.append("MIGRATION: a version change, not a format change")
        if self.alternates:
            others = ", ".join(f"{candidate.name} ({candidate.score:.2f})" for candidate in self.alternates)
            parts.append(f"also considered {others}")
        return ", ".join(parts)


# -- pairing ----------------------------------------------------------------


def _same_vendor(left: str | None, right: str | None) -> bool | None:
    """Whether two vendors are one company; None when either is unknown."""
    left, right = normalize_vendor(left or ""), normalize_vendor(right or "")
    return None if not left or not right else left == right


def best_match(
    name: str,
    kind: PluginKind,
    to: PluginKind,
    targets: Mapping[str, str | None],
    *,
    vendor: str | None = None,
    cutoff: float = FUZZY_CUTOFF,
) -> PluginGuess | None:
    """The plugin of format ``to`` among ``targets`` a plugin called ``name`` most likely is, or None.

    ``targets`` maps each installed name to its vendor, None where unknown, and
    ``vendor`` is the source plugin's. A target whose vendor is known to be
    another company is never offered, however alike the names. Leaving the
    source plugin itself out of ``targets`` is the caller's job: only it knows
    the ids that say a same-named file is the same plugin.
    """
    pool = {
        target: agreed
        for target, target_vendor in targets.items()
        if (agreed := _same_vendor(vendor, target_vendor)) is not False
    }

    def pair(target: str, tier: MatchTier, alternates: tuple[Candidate, ...] = ()) -> PluginGuess:
        score, migration = similarity(name, target), is_migration(name, target)
        agreed = vendor if pool[target] else None
        return PluginGuess(name, kind, target, to, tier, score, agreed, migration, alternates)

    if name in pool:
        return pair(name, MatchTier.EXACT)
    keys = normalized_keys(name)
    normalized = [target for target in pool if normalized_keys(target) & keys]
    if normalized:
        # Of several targets sharing a normalized name, the least surprising one.
        best = min(
            normalized,
            key=lambda target: (not pool[target], is_migration(name, target), -similarity(name, target), target),
        )
        return pair(best, MatchTier.NORMALIZED)
    folded = {target.casefold(): target for target in pool}
    close = difflib.get_close_matches(name.casefold(), list(folded), n=_ALTERNATE_LIMIT + 1, cutoff=cutoff)
    if not close:
        return None
    scored = sorted(
        ((folded[key], similarity(name, folded[key])) for key in close), key=lambda item: (-item[1], item[0])
    )
    return pair(scored[0][0], MatchTier.FUZZY, tuple(Candidate(target, score) for target, score in scored[1:]))
