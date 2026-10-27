"""Guessing which installed plugin an unmapped one probably is.

Hermetic. Every plugin here is synthetic or a well known product name, and the
candidates are built in the test, so nothing reads the machine running the
suite. A guess is only ever offered: a plan lists it as an experiment and never
picks it for you.
"""

from __future__ import annotations

import dataclasses
import datetime
import pathlib

import pytest

from abletoolz.plugins.identity.ids import Identity, PluginKind
from abletoolz.plugins.identity.mapping import FUZZY_CUTOFF, MatchTier, PluginGuess, best_match, is_migration
from abletoolz.plugins.identity.plugin_db import PluginDatabase, PluginEntry, PluginSource
from abletoolz.plugins.plan import Machine, known_state
from abletoolz.plugins.plan.machine import InstalledVst2

# A plugin as the guess sees it: its format, its name and its vendor, None where unknown.
type Plugin = tuple[PluginKind, str, str | None]


def vst2(name: str, vendor: str | None = None) -> Plugin:
    return PluginKind.VST, name, vendor


def vst3(name: str, vendor: str | None = None) -> Plugin:
    return PluginKind.VST3, name, vendor


def guess(source: Plugin, *targets: Plugin) -> PluginGuess | None:
    kind, name, vendor = source
    to = targets[0][0] if targets else PluginKind.VST3
    return best_match(name, kind, to, {target: target_vendor for _, target, target_vendor in targets}, vendor=vendor)


def only(source: Plugin, *targets: Plugin) -> PluginGuess:
    """The guess for ``source`` among ``targets``, which there has to be."""
    found = guess(source, *targets)
    assert found is not None
    return found


# -- which tier a pairing lands in ------------------------------------------


def test_an_identical_name_is_an_exact_match() -> None:
    suggestion = only(vst2("Effectrix", "Sugar Bytes"), vst3("Effectrix", "Sugar Bytes"))
    assert suggestion.tier is MatchTier.EXACT
    assert suggestion.target_name == "Effectrix"
    assert suggestion.score == 1.0


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("Serum_x64", "Serum"),
        ("Effectrix.64", "Effectrix"),
        ("Ohmicide_vstwin.64", "Ohmicide_vstwin"),
        ("Thing x64", "Thing"),
        ("Thing (x64)", "Thing"),
        ("Thing-64bit", "Thing"),
        ("Thing_x64.64", "Thing"),
        ("FabFilter Pro-Q 3", "Pro-Q 3"),
        ("FabFilter Pro-G x64.64", "Pro-G"),
    ],
)
def test_a_bitness_or_vendor_word_away_is_a_normalized_match(source: str, target: str) -> None:
    suggestion = only(vst2(source, "Vendor"), vst3(target, "Vendor"))
    assert suggestion.tier is MatchTier.NORMALIZED
    assert suggestion.target_name == target


def test_a_close_but_different_name_is_only_a_fuzzy_match() -> None:
    suggestion = only(vst2("Transient Shapr", "Vendor"), vst3("Transient Shaper", "Vendor"))
    assert suggestion.tier is MatchTier.FUZZY
    assert suggestion.score >= FUZZY_CUTOFF


def test_nothing_close_is_no_guess_at_all() -> None:
    assert guess(vst2("Effectrix", "Sugar Bytes"), vst3("Serum", "Xfer Records")) is None


def test_a_fuzzy_match_names_its_runners_up() -> None:
    suggestion = only(
        vst2("Transient Shapr", "Vendor"),
        vst3("Transient Shaper", "Vendor"),
        vst3("Transient Master", "Vendor"),
        vst3("Serum", "Xfer Records"),
    )
    assert suggestion.tier is MatchTier.FUZZY
    assert suggestion.target_name == "Transient Shaper"
    assert [candidate.name for candidate in suggestion.alternates] == ["Transient Master"]
    assert "also considered Transient Master" in suggestion.annotation


# -- vendor, the false friend killer ----------------------------------------


def test_another_company_s_plugin_is_never_guessed_and_the_same_company_s_is() -> None:
    """Midnight Compressor reduces to Compressor, a real and unrelated kHs plugin.

    The name rule alone would pair them. The vendors settle it.
    """
    assert guess(vst2("Midnight Compressor", "Loudness Inc"), vst3("Compressor", "Kilohearts")) is None
    suggestion = only(vst2("Midnight Compressor", "Kilohearts"), vst3("Compressor", "Kilohearts GmbH"))
    assert suggestion.tier is MatchTier.NORMALIZED
    assert suggestion.annotation == "normalized, VST2 -> VST3, score=0.69, vendor Kilohearts"


@pytest.mark.parametrize(
    ("source_vendor", "target_vendor"),
    [
        ("Native Instruments GmbH", "Native Instruments"),
        ("iZotope, Inc.", "iZotope"),
        ("Valhalla DSP, LLC", "Valhalla DSP"),
        ("Plogue Art et Technologie, Inc", "Plogue Art et Technologie"),
        ("Waves Audio Ltd.", "Waves Audio"),
        ("Toontrack Music AB", "Toontrack Music"),
        ("XFER RECORDS", "Xfer Records"),
    ],
)
def test_one_vendor_spelled_two_ways_is_still_one_vendor(source_vendor: str, target_vendor: str) -> None:
    """Live's own database spells the same company differently per format; case and legal form are not who they are.

    Measured 2026-08-12: "Native Instruments GmbH" on a VST2 row, "Native
    Instruments" on that plugin's VST3 row. Reading that as two companies would
    refuse twelve identical-name pairs -- Battery 4, FM8, Massive, Reaktor 6 --
    which is the opposite of what the vendor check is for.
    """
    suggestion = only(vst2("Battery 4", source_vendor), vst3("Battery 4", target_vendor))
    assert suggestion.vendor == source_vendor
    # The comment still quotes what the database said, not the reduced form.
    assert suggestion.annotation == f"exact, VST2 -> VST3, vendor {source_vendor}"


@pytest.mark.parametrize(
    ("source", "source_vendor", "target", "target_vendor"),
    [
        # Both reduce to "2 FX" once the vendor's first word comes off.
        ("Maschine 2 FX", "Native Instruments GmbH", "Serum 2 FX", "Xfer Records"),
        # Both reduce to "Master".
        ("Bass Master", "Loopmasters", "Transient Master", "Native Instruments"),
    ],
)
def test_a_real_false_friend_is_still_refused_after_the_legal_form_comes_off(
    source: str, source_vendor: str, target: str, target_vendor: str
) -> None:
    """The two this machine actually has. Neither may ever be offered."""
    assert guess(vst2(source, source_vendor), vst3(target, target_vendor)) is None


def test_a_missing_vendor_says_nothing_either_way() -> None:
    suggestion = only(vst2("Serum_x64", "Xfer Records"), vst3("Serum", None))
    assert suggestion.vendor is None
    assert "vendor unknown" in suggestion.annotation


# -- where a plan's vendors come from ----------------------------------------

COMPRESSOR = PluginEntry(
    name="Compressor",
    kind=PluginKind.VST3,
    source=PluginSource.LIVE_DATABASE,
    vendor="Kilohearts",
    uid_fields=(1, 2, 3, 4),
)


def database_machine(*entries: PluginEntry) -> Machine:
    """This machine as a plan sees it, from a plugin database holding ``entries``."""
    database = PluginDatabase(built=datetime.datetime.now(datetime.UTC), plugins=entries)
    return Machine.of(database, (), loadable=lambda info: True, au=lambda identifier: False)


def named(kind: PluginKind, name: str, manufacturer: str | None = None) -> Identity:
    return Identity(kind=kind, name=name, is_instrument=False, manufacturer=manufacturer)


@pytest.mark.parametrize(
    ("source_vendor", "guessed"), [("Loudness Inc", None), ("Kilohearts", "Compressor"), (None, "Compressor")]
)
def test_a_plan_s_guess_checks_the_vendor_live_s_database_holds_for_the_source(
    source_vendor: str | None, guessed: str | None
) -> None:
    """The set names no vendor for a VST, so the source's own row in Live's database is what gives one.

    A source Live has no row for -- here, a vendor of None -- is offered, saying its vendor is unknown.
    """
    source = PluginEntry(
        name="Midnight Compressor", kind=PluginKind.VST, source=PluginSource.LIVE_DATABASE, vendor=source_vendor
    )
    found = database_machine(source, COMPRESSOR).guess(named(PluginKind.VST, "Midnight Compressor"), PluginKind.VST3)
    assert (None if found is None else found[0]) == guessed
    if found is not None:
        assert ("vendor Kilohearts" if source_vendor else "vendor unknown") in found[1]


@pytest.mark.parametrize(("manufacturer", "guessed"), [("Loudness Inc", None), ("Kilohearts", "Compressor")])
def test_an_audio_unit_s_guess_checks_the_manufacturer_its_device_stores(
    manufacturer: str, guessed: str | None
) -> None:
    machine = database_machine(COMPRESSOR)
    found = machine.guess(named(PluginKind.AU, "Midnight Compressor", manufacturer), PluginKind.VST3)
    assert (None if found is None else found[0]) == guessed


def test_a_same_vendor_target_wins_over_a_false_friend() -> None:
    """Two VST3s normalize to the same name; the one the vendor agrees with is picked."""
    suggestion = only(
        vst2("Midnight Compressor", "Kilohearts"),
        vst3("Compressor", "Somebody Else"),
        vst3("kHs Compressor", "Kilohearts"),
    )
    assert suggestion.target_name == "kHs Compressor"
    assert suggestion.vendor == "Kilohearts"


# -- version migrations -----------------------------------------------------


@pytest.mark.parametrize(
    ("source", "target", "migration"),
    [
        ("FabFilter Pro-Q 2", "Pro-Q 3", True),
        ("Pro-Q 2", "Pro-Q 3", True),
        ("Sylenth1", "Sylenth", True),
        ("Mini V4", "Mini V3", True),
        ("FabFilter Pro-Q 3", "Pro-Q 3", False),
        ("Serum_x64", "Serum", False),
        ("ARP 2600 V3", "ARP 2600 V3", False),
    ],
)
def test_a_trailing_version_change_is_detected(source: str, target: str, migration: bool) -> None:
    assert is_migration(source, target) is migration


def test_a_migration_says_so_in_capitals_whatever_the_name_looks_like() -> None:
    suggestion = only(vst2("Pro-Q 2", "FabFilter"), vst3("Pro-Q 3", "FabFilter"))
    assert suggestion.migration
    assert "MIGRATION" in suggestion.annotation


# -- the other axis: what is known about the patch --------------------------


# Identity and state are separate questions: the guess answers the first, and a
# plan asks known_state the second before it shows the guess.


def test_a_measured_plugin_says_what_is_known_about_its_patch() -> None:
    state = known_state("Serum_x64", "Serum", PluginKind.VST3)
    assert state.predictable
    assert state.annotation == "state: verbatim (ear+declared 2026-08-10)"


def test_the_reframe_rung_is_named_rather_than_implied() -> None:
    assert (
        known_state("kHs Distortion", "kHs Distortion", PluginKind.VST3).annotation == "state: reframe (ear 2026-08-10)"
    )


def test_an_exact_name_match_nobody_has_heard_is_still_an_experiment() -> None:
    """The trap this label exists for: certain identity, unknown patch."""
    suggestion = only(vst2("Effectrix", "Sugar Bytes"), vst3("Effectrix", "Sugar Bytes"))
    assert suggestion.tier is MatchTier.EXACT
    state = known_state("Effectrix", "Effectrix", PluginKind.VST3)
    assert not state.predictable
    assert state.annotation == "state: unknown -- experiment, audition before trusting"


def test_a_plugin_is_never_its_own_guess_but_a_same_named_other_plugin_is() -> None:
    """A VST2 file of the same unique id is the same plugin, a file target rather than a guess."""
    machine = Machine(
        vst2=(InstalledVst2("Effectrix", 7, pathlib.Path("Effectrix.dll")),),
        vst3={},
        au=lambda identifier: False,
        loadable=lambda info: True,
    )
    effectrix = dataclasses.replace(named(PluginKind.VST, "Effectrix"), unique_id=7)
    assert machine.guess(effectrix, PluginKind.VST) is None
    found = machine.guess(dataclasses.replace(effectrix, unique_id=8), PluginKind.VST)
    assert found is not None and found[0] == "Effectrix"


def test_a_shared_version_number_is_not_a_shared_name() -> None:
    """Discord 3 and Volcano 3 both drop to "3" once a first word comes off."""
    assert guess(vst2("Discord 3", "Audio Damage"), vst3("Volcano 3", "FabFilter")) is None


def test_a_vst2_file_swap_guess_does_not_claim_there_is_no_translator() -> None:
    """A plan applies a VST2 file swap itself; its guess used to say "no translator yet"."""
    suggestion = only(vst2("Effectrix.64", "Sugar Bytes"), vst2("Effectrix", "Sugar Bytes"))
    assert "translator" not in suggestion.annotation
