"""Colorized rendering of domain results. The only place ANSI colors meet data."""

from __future__ import annotations

import logging
import pathlib

from abletoolz.live_set.plugins import PluginRef
from abletoolz.live_set.sample_ref import SampleRef
from abletoolz.live_set.tracks import AbletonTrack
from abletoolz.live_set.transport import SetLength
from abletoolz.misc import CB, B, C, G, M, R, Y
from abletoolz.plugin_parsers import PluginKind
from abletoolz.plugin_parsers.mapping import MatchTier, SuggestionReport, VendorAgreement
from abletoolz.plugin_parsers.plugin_db import FORMAT_LABELS, PluginDatabase
from abletoolz.plugin_parsers.repair import RepairReport, RepairStatus
from abletoolz.plugin_parsers.state import UNMEASURED

logger = logging.getLogger(__name__)


def render_tracks(tracks: list[AbletonTrack]) -> None:
    """One colorized summary line per track."""
    lines = [
        f"{B}Track type {track.type:>12}, {G}Name {track.name:>50}, {C}Id {track.id:>4}, "
        f"Group id {track.group_id or '-':>4}, {M}Color {track.color:>3}, Width {track.width or '-':>3}, "
        f"Height {track.height or '-':>3}, Unfolded: {track.unfolded}"
        for track in tracks
    ]
    logger.info("Tracks:\n%s", "\n".join(lines))


def render_length(length: SetLength) -> None:
    logger.info(
        "%sLongest clip or furthest arrangement position: %s bars. %sEstimated length(Only valid for 4/4): %s",
        M,
        length.bars,
        C,
        length,
    )


def render_missing_samples(missing: list[SampleRef]) -> None:
    for ref in missing:
        logger.warning(
            "%sSample %s missing: \n\tAbsolute[%s], Relative [%s]",
            R,
            ref.name,
            ref.absolute,
            ref.relative,
        )
    color = G if not missing else Y
    logger.info("%sMissing sample references: %s%s", color, M, len(missing))


def render_plugins(refs: list[PluginRef]) -> None:
    for ref in refs:
        color = G if ref.exists else (Y if ref.alternative else R)
        name = ref.name if ref.name is not None else "<unknown>"
        if ref.kind == PluginKind.AU and ref.manufacturer:
            name = f"{ref.manufacturer}: {name}"
        logger.info(
            "%s[%s%s%s] Plugin: %s, %sPath: %s, %sExists: %s",
            color,
            C,
            ref.track_location,
            color,
            name,
            M,
            ref.path,
            color,
            ref.exists,
        )
        if ref.alternative:
            logger.info(
                "%s\tPotential alternative path for %s found: %s%s",
                CB,
                ref.name,
                M,
                ref.alternative,
            )


def render_plugin_db(database: PluginDatabase, output: pathlib.Path) -> None:
    """Summarize a plugin database build: what was found, where, and where it went."""
    for kind in (PluginKind.VST, PluginKind.VST3):
        counts = [count for count in database.counts() if count.kind is kind]
        total = sum(count.count for count in counts)
        logger.info(
            "%s%s %s record(s): %s",
            C,
            total,
            FORMAT_LABELS[kind],
            ", ".join(f"{count.count} from {count.source}" for count in counts) or "none",
        )
        named = len(database.installed(kind))
        logger.info("%s    %s distinct plugin name(s) after the sources are merged.", CB, named)
    with_ids = sum(1 for entry in database.of_kind(PluginKind.VST3) if entry.uid_fields is not None)
    logger.info("%s%s VST3 record(s) carry a class id, which is what repair looks up by name.", M, with_ids)
    logger.info("%sWrote the plugin database to %s%s", M, B, output)


def render_mapping_suggestions(report: SuggestionReport, output: pathlib.Path) -> None:
    """Summarize a machine survey: what was found, what was guessed, where it went."""
    for kind in (PluginKind.VST, PluginKind.VST3):
        counts = report.inventory(kind)
        logger.info(
            "%s%s installed %s: %s",
            C,
            len(report.installed(kind)),
            FORMAT_LABELS[kind],
            ", ".join(str(count) for count in counts) or "none",
        )
    logger.info(
        "%s%s name(s) already mapped, %s with no candidate at all.",
        CB,
        len(report.already_mapped),
        len(report.unmatched),
    )
    for tier in MatchTier:
        found = report.by_tier(tier)
        logger.info("%s%s match(es): %s", C, tier.value.capitalize(), len(found))
    for suggestion in report.suggestions:
        if suggestion.vendor is VendorAgreement.MISMATCH:
            logger.warning(
                "%sVendor mismatch: %s%s%s looks like %s%s%s but %s is not %s.",
                R,
                Y,
                suggestion.source_name,
                R,
                Y,
                suggestion.target_name,
                R,
                suggestion.source_vendor,
                suggestion.target_vendor,
            )
    if report.untranslatable_count:
        logger.info(
            "%s%s suggestion(s) point a way nothing can translate yet, and say so.",
            Y,
            report.untranslatable_count,
        )
    logger.info(
        "%s%s suggestion(s) name a plugin whose patch is known to survive; the other %s are"
        " experiments, and each one says so.",
        C,
        report.measured_state_count,
        len(report.suggestions) - report.measured_state_count,
    )
    logger.info("%sWrote %s suggestion(s) to %s%s", M, len(report.suggestions), B, output)
    logger.info(
        "%sEvery one of them is commented out. Uncomment the ones you have checked and move them"
        " into your config yourself -- nothing is in force until you do.",
        CB,
    )


def render_repair(report: RepairReport) -> None:
    """Report every device repaired, every one that could not be, and why."""
    for action in report.by_status(RepairStatus.FIXED):
        logger.info(
            "%sRepaired %s[%s] %s%s%s as %s %s -- %s",
            G,
            C,
            action.track,
            Y,
            action.source_name,
            G,
            action.target_format,
            action.target_name,
            action.state.annotation if action.state is not None else UNMEASURED.annotation,
        )
    for action in report.by_status(RepairStatus.BROKEN_NO_UID):
        logger.warning(
            "%sNo class id known for %s, so %s[%s] %s%s stays broken."
            " Add a uid to its entry, or point plugin_translation.uid_db at a probed file.",
            R,
            action.target_name,
            C,
            action.track,
            Y,
            action.source_name,
        )
    for action in report.by_status(RepairStatus.BROKEN_UNMAPPED):
        logger.warning(
            "%sBroken and unmapped: %s[%s] %s%s -- left as it was.",
            R,
            C,
            action.track,
            Y,
            action.source_name,
        )
    for action in report.by_status(RepairStatus.UNSUPPORTED_PAIR):
        logger.warning(
            "%s%s[%s] %s%s%s is mapped to %s %s, and translating %s to %s is not implemented yet"
            " -- left as it was.",
            R,
            C,
            action.track,
            Y,
            action.source_name,
            R,
            action.target_format,
            action.target_name,
            action.source_format,
            action.target_format,
        )
    for action in report.by_status(RepairStatus.SET_TOO_OLD_FOR_TARGET):
        logger.warning(
            "%sThis set is too old to hold a %s device, so %s[%s] %s%s%s stays as it is."
            " Open it in Live and save it, then repair -- Live upgrades the set's format,"
            " and writing %s into it as it stands would leave a file Live refuses to open.",
            R,
            action.target_format,
            C,
            action.track,
            Y,
            action.source_name,
            R,
            action.target_format,
        )
    for action in report.by_status(RepairStatus.INCOMPLETE_DEVICE):
        logger.warning(
            "%s%s[%s] %s%s%s is all that was ever written down -- no category, sometimes no"
            " patch -- so there is nothing to turn into %s %s. Left as it was.",
            R,
            C,
            action.track,
            Y,
            action.source_name,
            R,
            action.target_format,
            action.target_name,
        )
    for action in report.by_status(RepairStatus.MAPPED_NOT_BROKEN):
        logger.info(
            "%s%s[%s] %s%s%s still loads, so it was left alone.",
            CB,
            C,
            action.track,
            Y,
            action.source_name,
            CB,
        )
    if report.suggestions:
        logger.info("%sMap these to fix more, under plugin_translation.targets in config.yaml:", M)
        for suggestion in report.suggestions:
            logger.info("%s    %s", B, suggestion)
    broken_left = (
        report.broken_unmapped_count
        + report.broken_no_uid_count
        + report.unsupported_pair_count
        + report.set_too_old_count
        + report.incomplete_device_count
    )
    color = G if not broken_left else Y
    logger.info(
        "%sRepaired %s device(s), %s still broken, %s left alone as loadable.",
        color,
        report.fixed_count,
        broken_left,
        report.mapped_not_broken_count + report.ok_count,
    )
    if report.fixed_count:
        logger.info(
            "%s%s of them are conversions somebody has measured; %s are experiments"
            " -- open the set and listen to those before you keep it.",
            CB if not report.fixed_experimental_count else Y,
            report.fixed_measured_count,
            report.fixed_experimental_count,
        )
