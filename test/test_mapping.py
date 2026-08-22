"""Suggesting plugin mappings from the local plugin database.

Hermetic. Every plugin here is synthetic or a well known product name and the
database is built in the test, so nothing reads the machine running the suite.

The rule this file exists to hold down: NOTHING the suggester writes is live.
Every entry comes out commented, and an active entry in the rendered file is a
failure, not a nicety -- enabling a mapping is a thing the user does on purpose.
"""

from __future__ import annotations

import datetime
import pathlib

import pytest
import yaml
from test_plugin_db import BUILT
from test_repair import OTHER_CID, SERUM_CID, installed, write_database

from abletoolz import cli
from abletoolz.plugin_parsers import mapping, plugin_db
from abletoolz.plugin_parsers.base import PluginKind
from abletoolz.plugin_parsers.config import AbletoolzConfig
from abletoolz.plugin_parsers.format_translation import NamedTarget, parse_config_targets
from abletoolz.plugin_parsers.mapping import (
    FUZZY_CUTOFF,
    MappingSuggestion,
    MatchTier,
    SuggestionReport,
    VendorAgreement,
    is_migration,
    render_targets_yaml,
    suggest_mappings,
    suggest_target_name,
    survey_machine,
)
from abletoolz.plugin_parsers.plugin_db import PluginDatabase, PluginEntry, PluginSource


@pytest.fixture(autouse=True)
def no_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing falls back to the machine running the tests."""
    monkeypatch.setattr(plugin_db, "default_vst_dirs", lambda: [])
    monkeypatch.setattr(plugin_db, "default_live_database_dir", lambda: None)


def vst2(name: str, vendor: str | None = None) -> PluginEntry:
    return PluginEntry(name=name, kind=PluginKind.VST, source=PluginSource.LIVE_DATABASE, vendor=vendor)


def vst3(name: str, vendor: str | None = None) -> PluginEntry:
    return PluginEntry(name=name, kind=PluginKind.VST3, source=PluginSource.LIVE_DATABASE, vendor=vendor)


def database(*entries: PluginEntry) -> PluginDatabase:
    return PluginDatabase(built=BUILT, plugins=entries)


def survey(*entries: PluginEntry) -> SuggestionReport:
    return suggest_mappings(database(*entries))


def only(*entries: PluginEntry) -> MappingSuggestion:
    """The one suggestion a two-plugin database produced."""
    report = survey(*entries)
    assert len(report.suggestions) == 1
    return report.suggestions[0]


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


def test_nothing_close_is_reported_as_unmatched_rather_than_guessed() -> None:
    report = survey(vst2("Effectrix", "Sugar Bytes"), vst3("Serum", "Xfer Records"))
    assert report.suggestions == ()
    assert set(report.unmatched) == {"Effectrix", "Serum"}


def test_a_fuzzy_match_names_its_runners_up() -> None:
    report = survey(
        vst2("Transient Shapr", "Vendor"),
        vst3("Transient Shaper", "Vendor"),
        vst3("Transient Master", "Vendor"),
        vst3("Serum", "Xfer Records"),
    )
    suggestion = next(s for s in report.suggestions if s.source_name == "Transient Shapr")
    assert suggestion.tier is MatchTier.FUZZY
    assert suggestion.target_name == "Transient Shaper"
    assert [candidate.name for candidate in suggestion.alternates] == ["Transient Master"]
    assert "also considered Transient Master" in suggestion.annotation


def test_an_already_mapped_source_is_counted_not_suggested() -> None:
    report = suggest_mappings(
        database(
            vst2("Serum_x64", "Xfer Records"),
            vst2("Effectrix", "Sugar Bytes"),
            vst3("Serum", "Xfer Records"),
            vst3("Effectrix", "Sugar Bytes"),
        ),
        mapped=["Serum_x64"],
    )
    assert report.already_mapped == ("Serum_x64",)
    assert [suggestion.source_name for suggestion in report.suggestions] == ["Effectrix"]


# -- which way a suggestion points ------------------------------------------


def test_a_plugin_installed_in_both_formats_is_suggested_toward_the_vst3() -> None:
    """One entry, not two: the reverse describes the same pair and aims the wrong way."""
    report = survey(vst2("Serum_x64", "Xfer Records"), vst3("Serum", "Xfer Records"))
    (suggestion,) = report.suggestions
    assert suggestion.source_kind is PluginKind.VST
    assert suggestion.target_kind is PluginKind.VST3
    assert suggestion.translator
    assert "VST2 -> VST3" in suggestion.annotation


def test_a_vst3_the_forward_pass_never_covered_is_suggested_back_toward_a_vst2() -> None:
    """A target has to be something installed, whichever format that turns out to be.

    "Massive X" has no VST2 of its own; the only thing this machine has that
    looks like it is the VST2 "Massive". Worth writing down -- and worth saying
    that nothing can perform it yet.
    """
    report = survey(vst2("Massive", "Native Instruments"), vst3("Massive", "Native Instruments"),
                    vst3("Massive X", "Native Instruments"))
    by_source = {suggestion.source_name: suggestion for suggestion in report.suggestions}
    assert by_source["Massive"].target_kind is PluginKind.VST3

    reverse = by_source["Massive X"]
    assert reverse.source_kind is PluginKind.VST3
    assert reverse.target_kind is PluginKind.VST
    assert reverse.target_name == "Massive"
    assert not reverse.translator
    assert "no translator yet" in reverse.annotation
    assert "VST3 -> VST2" in reverse.annotation


def test_the_only_registered_pair_is_the_one_that_says_nothing_about_translators() -> None:
    """(vst, vst3) is implemented; every other direction is honest about not being."""
    forward = only(vst2("Effectrix", "Sugar Bytes"), vst3("Effectrix", "Sugar Bytes"))
    assert forward.translator
    assert "no translator yet" not in forward.annotation


def test_nothing_is_suggested_toward_a_plugin_that_is_itself_mapped_away() -> None:
    """Aiming at what the user is migrating off would undo their own table."""
    report = suggest_mappings(
        database(vst2("Effectrix.64", "Sugar Bytes"), vst3("Effectrix", "Sugar Bytes")),
        mapped=["Effectrix.64"],
    )
    assert report.suggestions == ()


# -- vendor, the false friend killer ----------------------------------------


def test_a_vendor_mismatch_is_written_into_the_comment_in_capitals() -> None:
    """Midnight Compressor reduces to Compressor, a real and unrelated kHs plugin.

    The name rule alone would pair them. The vendors settle it, and the reader
    has to be told before they uncomment the line.
    """
    suggestion = only(vst2("Midnight Compressor", "Loudness Inc"), vst3("Compressor", "Kilohearts"))
    assert suggestion.tier is MatchTier.NORMALIZED
    assert suggestion.vendor is VendorAgreement.MISMATCH
    assert "vendor MISMATCH: Loudness Inc vs Kilohearts" in suggestion.annotation


@pytest.mark.parametrize(
    ("vendor", "normalized"),
    [
        ("Native Instruments GmbH", "native instruments"),
        ("iZotope, Inc.", "izotope"),
        ("Valhalla DSP, LLC", "valhalla dsp"),
        ("Plogue Art et Technologie, Inc", "plogue art et technologie"),
        ("Waves Audio Ltd.", "waves audio"),
        ("Toontrack Music AB", "toontrack music"),
        ("Xfer Records", "xfer records"),
        ("Kilohearts", "kilohearts"),
    ],
)
def test_a_vendors_legal_form_is_not_part_of_who_they_are(vendor: str, normalized: str) -> None:
    assert mapping.normalize_vendor(vendor) == normalized


@pytest.mark.parametrize(
    ("source_vendor", "target_vendor"),
    [
        ("Native Instruments GmbH", "Native Instruments"),
        ("iZotope, Inc.", "iZotope"),
        ("Valhalla DSP, LLC", "Valhalla DSP"),
    ],
)
def test_one_vendor_spelled_two_ways_is_still_one_vendor(source_vendor: str, target_vendor: str) -> None:
    """Live's own database spells the same company differently per format.

    Measured 2026-08-12: "Native Instruments GmbH" on a VST2 row, "Native
    Instruments" on that plugin's VST3 row. Reading that as two companies put a
    MISMATCH warning on twelve identical-name pairs -- Battery 4, FM8, Massive,
    Reaktor 6 -- which is the opposite of what the vendor check is for.
    """
    suggestion = only(vst2("Battery 4", source_vendor), vst3("Battery 4", target_vendor))
    assert suggestion.vendor is VendorAgreement.AGREES
    # The comment still quotes what the database said, not the reduced form, and
    # says on the other axis that nobody has heard this conversion.
    assert suggestion.annotation == (
        f"exact, VST2 -> VST3, vendor {source_vendor},"
        " state: unknown -- experiment, audition before trusting"
    )


@pytest.mark.parametrize(
    ("source", "source_vendor", "target", "target_vendor"),
    [
        # Both reduce to "2 FX" once the vendor's first word comes off.
        ("Maschine 2 FX", "Native Instruments GmbH", "Serum 2 FX", "Xfer Records"),
        # Both reduce to "Master".
        ("Bass Master", "Loopmasters", "Transient Master", "Native Instruments"),
    ],
)
def test_a_real_false_friend_is_still_flagged_after_the_legal_form_comes_off(
    source: str, source_vendor: str, target: str, target_vendor: str
) -> None:
    """The two this machine actually has. Neither may ever be pasted in unread."""
    suggestion = only(vst2(source, source_vendor), vst3(target, target_vendor))
    assert suggestion.tier is MatchTier.NORMALIZED
    assert suggestion.vendor is VendorAgreement.MISMATCH
    assert "MISMATCH" in suggestion.annotation


def test_a_missing_vendor_says_nothing_either_way() -> None:
    suggestion = only(vst2("Serum_x64", "Xfer Records"), vst3("Serum", None))
    assert suggestion.vendor is VendorAgreement.UNKNOWN
    assert "vendor unknown" in suggestion.annotation


def test_vendor_comparison_ignores_case() -> None:
    suggestion = only(vst2("Serum_x64", "XFER RECORDS"), vst3("Serum", "Xfer Records"))
    assert suggestion.vendor is VendorAgreement.AGREES


def test_a_same_vendor_target_wins_over_a_false_friend() -> None:
    """Two VST3s normalize to the same name; the one the vendor agrees with is picked."""
    report = survey(
        vst2("Midnight Compressor", "Kilohearts"),
        vst3("Compressor", "Somebody Else"),
        vst3("kHs Compressor", "Kilohearts"),
    )
    suggestion = next(s for s in report.suggestions if s.source_name == "Midnight Compressor")
    assert suggestion.target_name == "kHs Compressor"
    assert suggestion.vendor is VendorAgreement.AGREES


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


def test_a_measured_plugin_says_what_is_known_about_its_patch() -> None:
    """Identity and state are separate questions, and the comment answers both."""
    suggestion = only(vst2("Serum_x64", "Xfer Records"), vst3("Serum", "Xfer Records"))
    assert suggestion.state.predictable
    assert "state: verbatim (ear+declared 2026-08-10)" in suggestion.annotation


def test_the_reframe_rung_is_named_rather_than_implied() -> None:
    suggestion = only(vst2("kHs Distortion", "Kilohearts"), vst3("kHs Distortion", "Kilohearts"))
    assert "state: reframe (ear 2026-08-10)" in suggestion.annotation


def test_an_exact_name_match_nobody_has_heard_is_still_an_experiment() -> None:
    """The trap this label exists for: certain identity, unknown patch."""
    suggestion = only(vst2("Effectrix", "Sugar Bytes"), vst3("Effectrix", "Sugar Bytes"))
    assert suggestion.tier is MatchTier.EXACT
    assert not suggestion.state.predictable
    assert "state: unknown -- experiment, audition before trusting" in suggestion.annotation


def test_the_file_says_how_many_of_its_suggestions_are_experiments() -> None:
    report = survey(*SPREAD)
    document = render_targets_yaml(report, generated="2026-08-12")
    experiments = len(report.suggestions) - report.measured_state_count
    assert report.measured_state_count == 2
    assert f"{experiments} of the {len(report.suggestions)} below are experiments." in document


# -- one normalization, shared with repair ----------------------------------


@pytest.mark.parametrize(
    "source",
    ["Serum_x64", "FabFilter Pro-Q 3", "Effectrix.64", "kHs Distortion", "Midnight Compressor"],
)
def test_the_suggester_pairs_names_exactly_the_way_repair_suggests_them(source: str) -> None:
    """Repair's one-device suggestion and the survey run through the same rules.

    Repair offers a config line for a broken unmapped device; the survey tiers a
    whole machine. If these two ever disagreed about what a name reduces to, a
    user would be told two different things about the same plugin.
    """
    names = ["Serum", "Pro-Q 3", "Effectrix", "kHs Distortion", "Compressor"]
    report = survey(vst2(source), *[vst3(name) for name in names])
    from_survey = next(
        (s.target_name for s in report.suggestions if s.source_name == source and s.source_kind is PluginKind.VST),
        None,
    )
    assert suggest_target_name(source, names) == from_survey


# -- the file the user merges -----------------------------------------------

# One database with something in every tier, plus both known false friends.
SPREAD = (
    vst2("Serum_x64", "Xfer Records"),
    vst2("Effectrix", "Sugar Bytes"),
    vst2("kHs Distortion", "Kilohearts"),
    vst2("Midnight Compressor", "Loudness Inc"),
    vst2("Transient Shapr", "Vendor"),
    vst2("Bass Master", "Loopmasters"),
    vst3("Serum", "Xfer Records"),
    vst3("Effectrix", "Sugar Bytes"),
    vst3("kHs Distortion", "Kilohearts"),
    vst3("Compressor", "Kilohearts"),
    vst3("Transient Shaper", "Vendor"),
    vst3("Transient Master", "Native Instruments"),
)


def entry_lines(document: str) -> list[str]:
    """Every line of the file that is or was a mapping entry."""
    return [line for line in document.splitlines() if ": {to:" in line]


def parsed(document: str) -> object:
    return yaml.safe_load(document)["plugin_translation"]["targets"]


def test_not_one_suggestion_is_emitted_live() -> None:
    """The rule. An active entry here is a mapping nobody consciously enabled."""
    document = render_targets_yaml(survey(*SPREAD), generated="2026-08-12")
    assert parsed(document) is None
    lines = entry_lines(document)
    assert lines
    assert all(line.lstrip().startswith("#") for line in lines)


def test_uncommenting_a_line_is_all_it_takes_to_enable_it() -> None:
    """Which means every emitted line has to be a valid entry exactly where it sits."""
    document = render_targets_yaml(survey(*SPREAD), generated="2026-08-12")
    enabled = "\n".join(line.replace("# ", "", 1) if ": {to:" in line else line for line in document.splitlines())
    targets = parse_config_targets(parsed(enabled))

    assert targets["Serum_x64"] == NamedTarget(PluginKind.VST3, "Serum")
    assert "Midnight Compressor" in targets
    assert "Transient Shapr" in targets


def test_a_kilohearts_entry_needs_no_state_to_get_the_kilohearts_framing() -> None:
    document = render_targets_yaml(survey(vst2("kHs Distortion", "Kilohearts"), vst3("kHs Distortion", "Kilohearts")))
    (line,) = entry_lines(document)
    targets = parse_config_targets(yaml.safe_load(line.replace("# ", "", 1)))
    assert targets["kHs Distortion"].state_transform == "kilohearts"


def test_entries_are_grouped_by_tier_with_a_count_and_a_reason() -> None:
    document = render_targets_yaml(survey(*SPREAD), generated="2026-08-12")
    for tier in MatchTier:
        found = survey(*SPREAD).by_tier(tier)
        assert f"# -- {tier.value} ({len(found)})" in document
        for suggestion in found:
            assert f"# {suggestion.entry}  # {suggestion.annotation}" in document


def test_a_file_with_nothing_to_say_is_still_valid_yaml() -> None:
    document = render_targets_yaml(survey())
    assert parsed(document) is None
    assert "(nothing)" in document


def test_a_name_with_a_quote_in_it_survives_the_round_trip() -> None:
    document = render_targets_yaml(survey(vst2('The "Thing"_x64', "Vendor"), vst3('The "Thing"', "Vendor")))
    (line,) = entry_lines(document)
    assert set(yaml.safe_load(line.replace("# ", "", 1))) == {'The "Thing"_x64'}


def test_the_header_says_the_suggestions_are_off_and_how_to_turn_one_on() -> None:
    document = render_targets_yaml(survey(*SPREAD), generated="2026-08-12")
    header = document.split("plugin_translation:")[0]
    assert "NOTHING HERE IS IN FORCE" in header
    assert "commented out" in header
    assert "2026-08-12" in header


def test_a_false_friend_is_visible_in_the_file_it_is_written_to() -> None:
    """Both of this machine's real false friends, comment and all."""
    document = render_targets_yaml(
        survey(
            vst2("Maschine 2 FX", "Native Instruments GmbH"),
            vst3("Serum 2 FX", "Xfer Records"),
            vst2("Bass Master", "Loopmasters"),
            vst3("Transient Master", "Native Instruments"),
        )
    )
    assert 'vendor MISMATCH: Native Instruments GmbH vs Xfer Records' in document
    assert 'vendor MISMATCH: Loopmasters vs Native Instruments' in document
    assert parsed(document) is None


# -- reading a machine ------------------------------------------------------


def machine_database(tmp_path: pathlib.Path) -> pathlib.Path:
    """A written plugin database holding Serum and Effectrix in both formats."""
    database_dir = tmp_path / "Live Database"
    database_dir.mkdir()
    write_database(
        database_dir / "Live-plugins-1.db",
        [
            ("Serum", f"device:vst3:instr:{SERUM_CID}", installed(tmp_path, "Serum.vst3"), "global", True),
            (
                "Serum_x64",
                "device:vst:instr:1483109208?n=Serum_x64",
                installed(tmp_path, "Serum_x64.dll"),
                "custom",
                False,
            ),
            ("Effectrix", f"device:vst3:audiofx:{OTHER_CID}", installed(tmp_path, "Effectrix.vst3"), "global", True),
            (
                "Effectrix.64",
                "device:vst:audiofx:1935828326?n=Effectrix",
                installed(tmp_path, "Effectrix.dll"),
                "custom",
                False,
            ),
        ],
    )
    path = tmp_path / "plugin_db.json"
    plugin_db.write_plugin_db(plugin_db.build_plugin_db(database_dir=database_dir, vst_dirs=[]), path)
    return path


def test_a_survey_reads_the_written_plugin_database(tmp_path: pathlib.Path) -> None:
    path = machine_database(tmp_path)
    report = survey_machine(AbletoolzConfig(), db_path=path)
    # Serum_x64 is in the seed table already; Effectrix.64 is not.
    assert report.already_mapped == ("Serum_x64",)
    assert [suggestion.source_name for suggestion in report.suggestions] == ["Effectrix.64"]
    assert report.inventory(PluginKind.VST3)[0].source is PluginSource.LIVE_DATABASE


def test_a_survey_builds_a_database_in_memory_when_none_was_written(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never a refusal to run because nobody built the database yet."""
    folder = tmp_path / "VstPlugins"
    folder.mkdir()
    (folder / "Effectrix.64.dll").write_bytes(b"")
    (folder / "Effectrix.vst3").write_bytes(b"")
    monkeypatch.setattr(plugin_db, "default_vst_dirs", lambda: [folder])

    report = survey_machine(AbletoolzConfig(), db_path=tmp_path / "nothing here.json")
    assert [suggestion.source_name for suggestion in report.suggestions] == ["Effectrix.64"]


def test_the_report_carries_the_database_it_was_built_from(tmp_path: pathlib.Path) -> None:
    report = survey_machine(AbletoolzConfig(), db_path=machine_database(tmp_path))
    assert isinstance(report.database.built, datetime.datetime)
    assert len(report.installed(PluginKind.VST)) == 2
    assert len(report.installed(PluginKind.VST3)) == 2


# -- through the cli --------------------------------------------------------


def run_cli(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr("sys.argv", ["abletoolz", *argv])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    code = excinfo.value.code
    assert isinstance(code, int)
    return code


def test_the_command_runs_without_a_set_and_writes_a_file_of_comments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    path = machine_database(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: AbletoolzConfig())

    output = tmp_path / "suggested.yaml"
    assert run_cli(monkeypatch, "--suggest-plugin-mappings", str(output), "--plugin-db-path", str(path)) == 0
    document = output.read_text(encoding="utf-8")
    assert parsed(document) is None
    assert '"Effectrix.64": {to: vst3, name: "Effectrix"}' in document


def test_the_command_refuses_to_share_a_run_with_the_sample_database(monkeypatch: pytest.MonkeyPatch) -> None:
    assert run_cli(monkeypatch, "--db", "--suggest-plugin-mappings") == 2
