"""Colorized rendering of domain results. The only place ANSI colors meet data."""

from __future__ import annotations

import logging
import pathlib
import sys
from collections.abc import Mapping, Sequence

from abletoolz.ansi import CB, B, C, G, M, R, Y
from abletoolz.live_set.apply_ops import (
    CloneClipResult,
    CopySectionResult,
    GraftChainResult,
    OpResult,
    PlaceClipResult,
    RemoveSectionClipsResult,
    SetClipSettingResult,
    SetDeviceSettingResult,
    SetNotesResult,
    SetTrackSettingResult,
    SetTransportSettingResult,
    StraddlerModel,
)
from abletoolz.live_set.plugin_files import PluginFile
from abletoolz.live_set.plugins import PluginRef
from abletoolz.live_set.sample_ref import SampleRef
from abletoolz.live_set.tracks import AbletonTrack
from abletoolz.live_set.transport import SetLength
from abletoolz.live_set.xml_edit import number_value
from abletoolz.plugins.identity.ids import PluginKind
from abletoolz.plugins.identity.plugin_db import FORMAT_LABELS, PluginDatabase
from abletoolz.plugins.plan import Plan, SetOutcome
from abletoolz.plugins.recovery_routes import RecoveryTarget
from abletoolz.plugins.state.catalog import Catalog

logger = logging.getLogger(__name__)


def configure_logging(*, machine_readable: bool, verbose: bool, force: bool = False) -> None:
    """Send what a person reads to stdout, or to stderr when stdout carries a machine-readable document.

    --describe, --apply-ops and --search-samples write JSON to stdout, so the
    banners, progress and warnings move to stderr and a consumer can pipe
    stdout straight into a JSON parser. ``force`` replaces whatever logging
    was set up before, as a worker does for every set's own streams.
    """
    logging.basicConfig(
        stream=sys.stderr if machine_readable else sys.stdout,
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        force=force,
    )
    logging.getLogger("colormath").setLevel(logging.WARNING)


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


def render_missing_plugin_files(missing: list[PluginFile], notes: list[PluginFile]) -> None:
    """Files plugin devices point at that are not on disk, then what is worth saying about other patches."""
    for found in notes:
        logger.warning("%s%s on %s: %s", Y, found.device, found.track, found.note)
    for found in missing:
        assert found.ref is not None  # PluginFiles.check answers only file references
        relink = "" if found.rewrites else " (relink it in the plugin itself)"
        logger.warning(
            "%s%s on %s: %s missing: %s%s", R, found.device, found.track, found.ref.kind, found.ref.path, relink
        )
    color = G if not missing else Y
    logger.info("%sMissing files inside plugin devices: %s%s", color, M, len(missing))


def render_catalog(catalog: Catalog) -> None:
    """Every plugin abletoolz knows: its formats, the files its patch names, and the converters between plugins."""
    labels = {PluginKind.VST: "VST2", PluginKind.VST3: "VST3", PluginKind.AU: "AU"}
    maps = sorted(catalog, key=lambda plugin_map: (plugin_map.vendor.lower(), plugin_map.name.lower()))
    logger.info("%sPlugins abletoolz knows: %s%s", C, M, len(maps))
    for plugin_map in maps:
        formats = ", ".join(
            labels[kind] + ("" if plugin_map.can_write(kind) else " (read)") for kind in plugin_map.formats
        )
        vendor = f" ({plugin_map.vendor})" if plugin_map.vendor else ""
        logger.info("  %s%s%s%s [%s]: %s", G, plugin_map.name, C, vendor, plugin_map.id, formats)
        file_paths = plugin_map.file_paths
        if file_paths is not None:
            relinks = "relinks" if file_paths.rewrites else "reports only"
            logger.info("      files inside its patch: %s", relinks)
    converters = sorted(catalog.converters(), key=lambda converter: (converter.source, converter.target))
    logger.info("%sConverters between plugins: %s%s", C, M, len(converters))
    for converter in converters:
        logger.info("  %s -> %s: %s", converter.source, converter.target, converter.measured.annotation)


def render_cached_plugins(missing: Mapping[str, int]) -> None:
    """The plugins a previous scan of these exact bytes found missing. Nothing was checked again."""
    for name, count in missing.items():
        logger.info("%sPlugin: %s, %sMissing on %s device(s)", R, name, M, count)
    color = G if not missing else Y
    logger.info("%sCached: %s%s missing plugin reference(s)", color, M, sum(missing.values()))


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
    logger.info("%s%s VST3 record(s) carry a class id, which is how a plan knows a VST3 is installed.", M, with_ids)
    logger.info("%sWrote the plugin database to %s%s", M, B, output)


def render_applied_ops(results: Sequence[OpResult]) -> None:
    """One human line per op, from the same facts the JSON document carries.

    The JSON on stdout is what a program reads; this is what the person
    watching the run reads. Straddlers get their own line because a section
    that reports as emptied can still have clips sounding through it.
    """
    for result in results:
        logger.info("%s%s", G, _applied_op_line(result))
        for straddler in _straddlers(result):
            logger.info(
                "%s  kept %r on %r (%s-%s): crosses the section %s",
                Y,
                straddler.clip,
                straddler.track,
                number_value(straddler.start),
                number_value(straddler.end),
                straddler.crosses,
            )


def _straddlers(result: OpResult) -> Sequence[StraddlerModel]:
    if isinstance(result, CopySectionResult):
        return result.skipped
    if isinstance(result, RemoveSectionClipsResult):
        return result.kept
    return ()


def _applied_op_line(result: OpResult) -> str:
    """One line naming what an op did, in the terms the op was asked in."""
    if isinstance(result, SetNotesResult):
        return f"set_notes: wrote {result.notes} note(s) to {result.clip!r} on track {result.track!r}"
    if isinstance(result, CloneClipResult):
        return f"clone_clip: cloned into slot {result.slot} of track {result.track!r} as {result.name!r}"
    if isinstance(result, GraftChainResult):
        return f"graft_chain: grafted {result.devices} device(s) onto track {result.target_track!r} ({result.mode})"
    if isinstance(result, PlaceClipResult):
        return f"place_clip: placed {result.clip!r} on track {result.track!r} at beat {number_value(result.at)}"
    if isinstance(result, CopySectionResult):
        line = f"copy_section: copied {len(result.copied)} clip(s) to beat {number_value(result.dest_start)}"
        if result.replaced:
            line += f", deleted {result.replaced} at the destination"
        if result.envelope_events:
            line += f", copied {result.envelope_events} automation event(s)"
        return line
    if isinstance(result, SetTrackSettingResult):
        return f"set_track_setting: {result.setting} = {result.value} on {len(result.tracks)} track(s)"
    if isinstance(result, SetClipSettingResult):
        return f"set_clip_setting: Clip.{result.setting} = {result.value} on {result.clip!r} on track {result.track!r}"
    if isinstance(result, SetTransportSettingResult):
        return f"set_transport_setting: Transport.{result.setting} = {result.value}"
    if isinstance(result, SetDeviceSettingResult):
        return f"set_device_setting: {result.device}.{result.setting} = {result.value} on {result.devices} device(s)"
    if not result.removed and not result.kept:
        return "remove_section_clips: nothing to delete"
    return f"remove_section_clips: deleted {len(result.removed)} clip(s)"


# -- abletoolz plugins: printed rather than logged, it sets up no logging ------------------


def render_recovery_routes(routes: Sequence[RecoveryTarget]) -> None:
    """Every recovery route, whether its binary is here, and where."""
    for target in routes:
        print(f"{target.route.id}: {target.status.value} ({target.route.state_evidence})")
        print(f"  {', '.join(target.route.source_names)}")
        print(f"  {target.path if target.path is not None else target.reason}")


def render_plan(plan: Plan, output: pathlib.Path) -> None:
    """Each plugin of a plan with its pick and targets, and what to do with the file."""
    for choice in plan.plugins:
        devices = len(choice.devices)
        sets = len({ref.set for ref in choice.devices})
        state = "broken" if choice.broken else "loads"
        print(f"{choice.plugin} ({choice.format.value}), {devices} device(s) in {sets} set(s), {state}: {choice.pick}")
        for target in choice.targets:
            refused = sum(target.refused.values())
            notes = [] if target.installed else ["not installed"]
            if refused:
                notes.append(f"{refused} refused")
            extra = f" [{', '.join(notes)}]" if notes else ""
            print(f"    {target.id}: {target.evidence}{extra}")
    print(f"Plan written to {output}. Edit `pick`, then run: abletoolz plugins apply {output}")


def render_applied_plan(outcomes: Sequence[SetOutcome]) -> None:
    """What applying a plan did to each set, device by device."""
    for outcome in outcomes:
        if outcome.skipped is not None:
            print(f"{outcome.path}: skipped, {outcome.skipped}")
            continue
        refused = [device for device in outcome.devices if not device.done]
        print(f"{outcome.path}: {outcome.changed} device(s) rewritten, {len(refused)} left as they were")
        for device in outcome.devices:
            mark = "->" if device.done else "kept, because"
            detail = device.pick if device.done else device.reason
            print(f"  [{device.device}] {device.plugin} on {device.track} {mark} {detail}")
