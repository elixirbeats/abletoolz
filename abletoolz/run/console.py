"""Everything a command prints for a person to read, and the one place colours meet data.

Every command calls :func:`setup` once. From then on what a person reads is
written by :func:`say`, coloured by :func:`paint` from one table of roles, so a
set name, a Live version or a missing file looks the same in every command.
Logging carries diagnostics only: hidden by default, shown with ``-v``.

A set run prints one block per set, rendered from its run record
(:func:`render_set`), then one summary for the run (:func:`render_run`). A
block gives counts and the problems by name; ``-v`` adds every item behind
the counts. Colour is on only when the output is a terminal and ``NO_COLOR``
is not set.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
import logging
import os
import pathlib
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import TextIO

import colorama

from abletoolz.ansi import BOLD, B, C, G, M, R, Y
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
from abletoolz.live_set.tracks import AbletonTrack
from abletoolz.live_set.xml_edit import number_value
from abletoolz.plugins.identity.ids import PluginKind
from abletoolz.plugins.identity.plugin_db import FORMAT_LABELS, PluginDatabase
from abletoolz.plugins.plan import Plan, PluginChoice, SetOutcome
from abletoolz.plugins.plan.model import entries
from abletoolz.plugins.recovery_routes import RecoveryTarget
from abletoolz.plugins.state.catalog import Catalog
from abletoolz.run.report import SetRecord

logger = logging.getLogger(__name__)

LISTED = 6
"""How many problems of one kind a block names before ``-v`` is needed for the rest."""


class Role(enum.Enum):
    """What a piece of text is, which decides its colour everywhere."""

    NAME = enum.auto()  # a set or file the line is about
    META = enum.auto()  # facts about it: Live version, BPM, bars, length
    PATH = enum.auto()
    COUNT = enum.auto()
    DONE = enum.auto()  # relinked, converted, saved
    ATTENTION = enum.auto()  # left as it was, refused, worth knowing
    PROBLEM = enum.auto()  # missing, failed
    HINT = enum.auto()  # what to do next, what -v adds


_COLOURS = {
    Role.NAME: colorama.Fore.LIGHTWHITE_EX + BOLD,
    Role.META: C,
    Role.PATH: B,
    Role.COUNT: M,
    Role.DONE: G,
    Role.ATTENTION: Y,
    Role.PROBLEM: R,
    Role.HINT: colorama.Style.DIM,
}
_RESET = colorama.Style.RESET_ALL


@dataclasses.dataclass(slots=True)
class _Output:
    machine_readable: bool
    colour: bool
    verbose: bool

    @property
    def stream(self) -> TextIO:
        """Looked up at each write, so a redirected or replaced stdout is the one written to."""
        return sys.stderr if self.machine_readable else sys.stdout


_output = _Output(machine_readable=False, colour=False, verbose=False)


def setup(*, verbose: bool = False, machine_readable: bool = False, force: bool = False) -> None:
    """Set up output for a command: every command calls this once, before it reads anything.

    What a person reads goes to stdout, or to stderr when stdout carries a
    machine-readable document: --describe, --apply-ops, --search-samples and a
    plugins command's --json write JSON there, so a consumer can pipe stdout
    straight into a JSON parser. Diagnostics are logged, shown with
    ``verbose``. ``force`` replaces whatever was set up before, as a worker
    does for every set's own streams.
    """
    _output.machine_readable = machine_readable
    _output.verbose = verbose
    stream = _output.stream
    _output.colour = stream.isatty() and "NO_COLOR" not in os.environ
    logging.basicConfig(
        stream=stream,
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(message)s",
        force=force,
    )
    logging.getLogger("colormath").setLevel(logging.WARNING)


def verbose() -> bool:
    """Whether this command was asked for ``-v``."""
    return _output.verbose


def paint(role: Role, text: object) -> str:
    """``text`` in its role's colour, or as it is when colour is off."""
    return f"{_COLOURS[role]}{text}{_RESET}" if _output.colour else str(text)


def say(line: str = "") -> None:
    """Write one line for a person to read; a character the output can't encode shows as a stand-in."""
    stream = _output.stream
    try:
        print(line, file=stream)
    except UnicodeEncodeError:  # a file name the code page has no character for, as when piped on Windows
        encoding = getattr(stream, "encoding", None) or "ascii"
        print(line.encode(encoding, errors="replace").decode(encoding), file=stream)


def problem(text: str) -> None:
    """A command that cannot go on, said as a problem."""
    say(paint(Role.PROBLEM, text))


def attention(text: str) -> None:
    """Something worth knowing before the command goes on."""
    say(paint(Role.ATTENTION, text))


def hint(text: str) -> None:
    say(paint(Role.HINT, text))


def _count(number: int, noun: str) -> str:
    return f"{paint(Role.COUNT, number)} {noun}"


def _named(counts: Iterable[tuple[str, int]], role: Role) -> str:
    """Names with their counts, as many as a block lists by default, every one with -v."""
    items = sorted(counts, key=lambda item: (-item[1], item[0].lower()))
    shown = items if _output.verbose else items[:LISTED]
    text = ", ".join(paint(role, name) + (f" x{count}" if count > 1 else "") for name, count in shown)
    rest = len(items) - len(shown)
    return text + (paint(Role.HINT, f", and {rest} more (-v lists them)") if rest else "")


def _line(label: str, *parts: str) -> None:
    say(f"  {label:<13}" + " | ".join(part for part in parts if part))


# -- a set run ---------------------------------------------------------------------------------------


def render_set(record: SetRecord) -> None:
    """One set's block: its header, then a line for each kind of thing the run found or did."""
    path = pathlib.Path(record.path)
    if record.error is not None:
        say(f"{paint(Role.NAME, path.name)}  {paint(Role.PROBLEM, 'failed: ' + record.error)}")
        if _output.verbose:
            say(f"  {paint(Role.PATH, path)}")
        return
    facts = []
    if record.live_version:
        facts.append(record.live_version.removeprefix("Ableton ").strip())
    if record.bpm is not None:
        facts.append(f"{number_value(round(record.bpm, 2))} BPM")
    if record.bars is not None:
        facts.append(f"{record.bars} bars")
    if record.seconds is not None:
        minutes, seconds = divmod(round(record.seconds), 60)
        facts.append(f"{minutes}:{seconds:02d}")
    cached = paint(Role.HINT, " (cached)") if record.cached else ""
    say(f"{paint(Role.NAME, path.name)}  {paint(Role.META, ', '.join(facts))}{cached}")
    if _output.verbose:
        say(f"  {paint(Role.PATH, path)}")
    _render_samples(record)
    _render_plugin_files(record)
    _render_plugins(record)
    _render_refused(record)
    if record.edits:
        _line("edits", ", ".join(record.edits))
    for notice in record.notices:
        _line("note", paint(Role.ATTENTION, notice))
    _render_save(record)


def _render_samples(record: SetRecord) -> None:
    relinked = [fix for fix in record.relinks if fix.device is None]
    missing = record.samples_missing_by_name
    if missing is None and not relinked:
        return
    parts = []
    if missing:
        total = sum(missing.values())
        parts.append(f"{_count(total, paint(Role.PROBLEM, 'missing'))}: {_named(missing.items(), Role.NAME)}")
    elif missing is not None:
        parts.append(paint(Role.DONE, "none missing"))
    if relinked:
        parts.append(_count(len(relinked), paint(Role.DONE, "relinked")))
    if record.collected:
        parts.append(_count(len(record.collected), "collected into the project"))
    _line("samples", *parts)
    if _output.verbose:
        for fix in relinked:
            say(f"      {paint(Role.PATH, fix.old)} -> {paint(Role.PATH, fix.new)}")
        for copy in record.collected:
            say(f"      collected {paint(Role.PATH, copy.source)} -> {paint(Role.PATH, copy.destination)}")


def _render_plugin_files(record: SetRecord) -> None:
    relinked = [fix for fix in record.relinks if fix.device is not None]
    missing = record.plugin_files_missing
    if not (missing or relinked or record.plugin_notes):
        return
    parts = []
    if missing:
        names = Counter(pathlib.PurePath(found.path.replace("\\", "/")).name for found in missing)
        parts.append(f"{_count(len(missing), paint(Role.PROBLEM, 'missing'))}: {_named(names.items(), Role.NAME)}")
    if relinked:
        parts.append(_count(len(relinked), paint(Role.DONE, "relinked")))
    if record.plugin_notes:
        parts.append(_count(len(record.plugin_notes), paint(Role.ATTENTION, "to know about")))
    _line("plugin files", *parts)
    if _output.verbose:
        for found in missing:
            where = "" if found.relinkable else paint(Role.HINT, " (relink it in the plugin itself)")
            say(f"      {found.device} on {found.track}: {found.kind} {paint(Role.PATH, found.path)}{where}")
        for fix in relinked:
            say(f"      {fix.device} on {fix.track}: {paint(Role.PATH, fix.old)} -> {paint(Role.PATH, fix.new)}")
    for note in record.plugin_notes if _output.verbose else record.plugin_notes[:LISTED]:
        say(f"      {note.device} on {note.track}: {paint(Role.ATTENTION, note.note)}")


def _render_plugins(record: SetRecord) -> None:
    missing = record.plugins_missing
    if missing is None:
        return
    parts = []
    if missing:
        parts.append(
            f"{_count(sum(missing.values()), paint(Role.PROBLEM, 'missing'))}: {_named(missing.items(), Role.NAME)}"
        )
    else:
        parts.append(paint(Role.DONE, "all load"))
    if record.plugins_loaded and missing:
        parts.append(_count(sum(record.plugins_loaded.values()), "load"))
    if record.plugin_alternatives:
        parts.append(_count(len(record.plugin_alternatives), "load from another path"))
    _line("plugins", *parts)
    if _output.verbose:
        for name, alternative in record.plugin_alternatives.items():
            say(f"      {name}: loads from {paint(Role.PATH, alternative)}")
    if _output.verbose and record.plugins_loaded:
        say(f"      load: {_named(record.plugins_loaded.items(), Role.NAME)}")


def _render_refused(record: SetRecord) -> None:
    if not record.refused:
        return
    _line("left as is", _count(len(record.refused), paint(Role.ATTENTION, "file(s) left as they were")))
    shown = record.refused if _output.verbose else record.refused[:LISTED]
    for refusal in shown:
        say(f"      {refusal.what}: {paint(Role.ATTENTION, refusal.why)}")
    if len(shown) < len(record.refused):
        hint(f"      and {len(record.refused) - len(shown)} more (-v lists them)")


def _render_save(record: SetRecord) -> None:
    if record.written is not None:
        backup = f", backup {paint(Role.PATH, record.backup)}" if record.backup else ""
        _line("saved", f"{paint(Role.DONE, 'to')} {paint(Role.PATH, record.written)}{backup}")
    elif record.changed:
        _line("not saved", paint(Role.HINT, "add -s to write these changes"))


def render_run(records: Sequence[SetRecord], *, elapsed: datetime.timedelta, report: pathlib.Path) -> None:
    """The run in one line, where its report went, and which sets failed."""
    failed = [record for record in records if record.error is not None]
    changed = sum(1 for record in records if record.written is not None)
    missing_samples = sum(record.samples_missing or 0 for record in records)
    relinked = sum(len(record.relinks) for record in records)
    left = sum(len(record.refused) for record in records)
    missing_plugins = sum(sum((record.plugins_missing or {}).values()) for record in records)
    parts = [
        f"{_count(len(records), 'set(s)')} in {paint(Role.META, str(elapsed).split('.')[0])}",
        _count(changed, "saved") if changed else "",
        _count(len(failed), paint(Role.PROBLEM, "failed")) if failed else "",
        _count(missing_samples, paint(Role.PROBLEM, "missing file(s)")) if missing_samples else "",
        _count(relinked, paint(Role.DONE, "relinked")) if relinked else "",
        _count(left, paint(Role.ATTENTION, "left as they were")) if left else "",
        _count(missing_plugins, paint(Role.PROBLEM, "missing plugin device(s)")) if missing_plugins else "",
    ]
    say()
    say(" | ".join(part for part in parts if part))
    say(f"run report: {paint(Role.PATH, report)}")
    for record in failed:
        say(f"  {paint(Role.PROBLEM, 'failed')} {paint(Role.PATH, record.path)}: {record.error}")


def render_tracks(tracks: list[AbletonTrack]) -> None:
    """One line per track, for --list-tracks."""
    for track in tracks:
        say(
            f"  {paint(Role.META, f'{track.type:>12}')} {paint(Role.NAME, f'{track.name:<40}')} "
            f"id {paint(Role.COUNT, f'{track.id:>4}')} group {track.group_id or '-':>4} colour {track.color:>3} "
            f"width {track.width or '-':>3} height {track.height or '-':>3} unfolded {track.unfolded}"
        )


def render_catalog(catalog: Catalog) -> None:
    """Every plugin abletoolz knows: its formats, the files its patch names, and the converters between plugins."""
    labels = {PluginKind.VST: "VST2", PluginKind.VST3: "VST3", PluginKind.AU: "AU"}
    maps = sorted(catalog, key=lambda plugin_map: (plugin_map.vendor.lower(), plugin_map.name.lower()))
    say(f"Plugins abletoolz knows: {paint(Role.COUNT, len(maps))}")
    for plugin_map in maps:
        formats = ", ".join(
            labels[kind] + ("" if plugin_map.can_write(kind) else " (read)") for kind in plugin_map.formats
        )
        vendor = f" ({plugin_map.vendor})" if plugin_map.vendor else ""
        say(f"  {paint(Role.NAME, plugin_map.name)}{paint(Role.META, vendor)} [{plugin_map.id}]: {formats}")
        file_paths = plugin_map.file_paths
        if file_paths is not None:
            say(f"      files inside its patch: {'relinks' if file_paths.rewrites else 'reports only'}")
    converters = sorted(catalog.converters(), key=lambda converter: (converter.source, converter.target))
    say(f"Converters between plugins: {paint(Role.COUNT, len(converters))}")
    for converter in converters:
        say(f"  {converter.source} -> {converter.target}: {paint(Role.META, converter.measured.annotation)}")


def render_plugin_db(database: PluginDatabase, output: pathlib.Path) -> None:
    """Summarize a plugin database build: what was found, where, and where it went."""
    for kind in (PluginKind.VST, PluginKind.VST3):
        counts = [count for count in database.counts() if count.kind is kind]
        sources = ", ".join(f"{count.count} from {count.source}" for count in counts) or "none"
        say(f"{_count(sum(count.count for count in counts), FORMAT_LABELS[kind] + ' record(s)')}: {sources}")
        say(f"    {_count(len(database.installed(kind)), 'distinct plugin name(s)')} after the sources are merged")
    with_ids = sum(1 for entry in database.of_kind(PluginKind.VST3) if entry.uid_fields is not None)
    say(f"{_count(with_ids, 'VST3 record(s)')} carry a class id, which is how a plan knows a VST3 is installed")
    say(f"Wrote the plugin database to {paint(Role.PATH, output)}")


def render_applied_ops(results: Sequence[OpResult]) -> None:
    """One human line per op, from the same facts the JSON document carries.

    The JSON on stdout is what a program reads; this is what the person
    watching the run reads. Straddlers get their own line because a section
    that reports as emptied can still have clips sounding through it.
    """
    for result in results:
        say(paint(Role.DONE, _applied_op_line(result)))
        for straddler in _straddlers(result):
            start, end = number_value(straddler.start), number_value(straddler.end)
            say(
                paint(
                    Role.ATTENTION,
                    f"  kept {straddler.clip!r} on {straddler.track!r} ({start}-{end}): crosses the section "
                    f"{straddler.crosses}",
                )
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


# -- abletoolz plugins ------------------------------------------------------------------------------


def render_recovery_routes(routes: Sequence[RecoveryTarget]) -> None:
    """Every recovery route, whether its binary is here, and where."""
    for target in routes:
        say(
            f"{paint(Role.NAME, target.route.id)}: {target.status.value} "
            f"{paint(Role.META, target.route.state_evidence)}"
        )
        say(f"  {', '.join(target.route.source_names)}")
        say(f"  {paint(Role.PATH, target.path) if target.path is not None else target.reason}")


def render_plan(plan: Plan, output: pathlib.Path, *, applying: bool = False) -> None:
    """The plugins that don't load here, every plugin with -v, how many have a target, and what to do next."""
    for name, choice in entries(plan):
        if choice.broken or _output.verbose:
            say(_plan_line(name, choice))
    for unread in plan.unread:
        say(f"{paint(Role.PROBLEM, 'not read, so not planned:')} {paint(Role.PATH, unread.path)}: {unread.reason}")
    broken = [choice for choice in plan.plugins if choice.broken]
    targeted = [choice for choice in broken if choice.picks]
    devices = sum(len(choice.devices) for choice in targeted), sum(len(choice.devices) for choice in broken)
    say()
    say(
        f"{_count(len(targeted), 'of')} {paint(Role.COUNT, len(broken))} plugin(s) that don't load here "
        f"({paint(Role.COUNT, devices[0])} of {paint(Role.COUNT, devices[1])} device(s)) have a target installed here"
    )
    if applying:
        say(f"Plan written to {paint(Role.PATH, output)}; applying it.")
    else:
        say(f"Plan written to {paint(Role.PATH, output)}.")
        hint(f"Check convert_to, then run: abletoolz plugins apply {output}")


def _plan_line(name: str, choice: PluginChoice) -> str:
    state = f" {paint(Role.PROBLEM, 'does not load here')}" if choice.broken else ""
    options = ", ".join(target.id for target in choice.targets) or paint(Role.HINT, "nothing to become here")
    picked = f" -> {paint(Role.DONE, ', then '.join(choice.picks))}" if choice.picks else ""
    devices = _count(len(choice.devices), "device(s)")
    label = paint(Role.META, FORMAT_LABELS[choice.format])
    return f"{paint(Role.NAME, name)} {label}, {devices}{state}: {options}{picked}"


def render_applied_plan(outcomes: Sequence[SetOutcome]) -> None:
    """Each set the plan rewrote, with the devices it left; -v adds every converted device and its warnings."""
    for outcome in outcomes:
        name = paint(Role.NAME, pathlib.Path(outcome.path).name)
        if outcome.skipped is not None:
            say(f"{name}  {paint(Role.ATTENTION, 'skipped: ' + outcome.skipped)}")
            continue
        refused = [device for device in outcome.devices if not device.done]
        left = f", {_count(len(refused), paint(Role.ATTENTION, 'left as they were'))}" if refused else ""
        say(f"{name}  {_count(outcome.changed, paint(Role.DONE, 'converted'))}{left}")
        for device in outcome.devices if _output.verbose else refused:
            if device.done:
                say(f"  [{device.device}] {device.plugin} on {device.track} -> {paint(Role.DONE, device.pick)}")
            else:
                say(f"  [{device.device}] {device.plugin} on {device.track}: {paint(Role.ATTENTION, device.reason)}")
            if _output.verbose:
                for warning in device.warnings:
                    say(f"      {paint(Role.ATTENTION, 'warning:')} {warning}")
    converted = sum(outcome.changed for outcome in outcomes)
    kept = sum(1 for outcome in outcomes for device in outcome.devices if not device.done)
    sets = sum(1 for outcome in outcomes if outcome.changed)
    skipped = sum(1 for outcome in outcomes if outcome.skipped is not None)
    say()
    say(
        f"{_count(converted, paint(Role.DONE, 'device(s) converted'))} in {paint(Role.COUNT, sets)} set(s)"
        f", {_count(kept, 'left as they were')}, {_count(skipped, 'set(s) skipped')}"
    )
    if converted and not _output.verbose:
        hint("Add -v to list each converted device with its warnings, such as which to audition.")
