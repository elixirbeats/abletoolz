"""``abletoolz undo``: put back the sets abletoolz's last command saved over.

Every in-place save leaves the set's previous bytes in ``abletoolz_backup`` and a
record in the undo history (:func:`~abletoolz.live_set.persistence.record_save`),
grouped by the command that saved it. Undo takes the newest command, lists each
set with the backup it comes back from, and asks; then it moves each backup over
its set in one rename, the set's current version backed up first, so the backup
folder neither grows nor loses a version. The command leaves the history, so
undoing again goes one command further back.
``--list`` shows the history, and ``--trim N`` keeps each set's ``N`` newest
backups and deletes the rest, after asking; an undo reaching a trimmed backup
skips that set.

What a person can do to the files around it is checked, not trusted: a damaged
record is skipped, a set whose backup was deleted is said and left alone, a set
deleted or moved since is put back at its old path only when you answer yes, and
a set that can't be written keeps its record for the next try.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import hashlib
import json
import os
import pathlib
import re
from collections.abc import Callable, Mapping, Sequence

from abletoolz.live_set.persistence import BACKUP_DIR, create_backup, undo_journal


@dataclasses.dataclass(frozen=True, slots=True)
class Save:
    """One set abletoolz saved over, and where its previous version is."""

    record: pathlib.Path
    path: pathlib.Path
    backup: pathlib.Path
    sha256: str
    backup_sha256: str

    @property
    def backup_here(self) -> bool:
        """Whether this save's backup is still there: a file of that name holding the bytes it was saved with."""
        return self.backup.is_file() and hashlib.sha256(self.backup.read_bytes()).hexdigest() == self.backup_sha256

    def listing(self, before: str | None = None) -> str:
        """The set, the backup it comes back from with when that version was last saved, and anything to watch.

        ``before`` is the sha256 a newer undo in the same batch leaves the set at, if one restores it first.
        """
        when = ""
        if self.backup_here:  # a backup keeps the set's own modified time: when that version was last saved
            modified = datetime.datetime.fromtimestamp(self.backup.stat().st_mtime)
            when = f", last saved {modified:%Y-%m-%d %H:%M}"
        return f"  {self.path}\n    <- {self.backup}{when}{self.note(before)}"

    def note(self, before: str | None = None) -> str:
        """What the listing says about this set besides its backup."""
        if not self.backup_here:
            return "  (its backup is gone, so it can't come back)"
        if before is None and not self.path.is_file():
            return "  (the set isn't there any more; it would be put back at this path)"
        current = before or hashlib.sha256(self.path.read_bytes()).hexdigest()
        if current != self.sha256:  # saved again in Live, say
            return "  (changed since abletoolz saved it; the current version is backed up too)"
        return ""


@dataclasses.dataclass(frozen=True, slots=True)
class Run:
    """One command's saves, the undo step it is."""

    command: str
    saved: str
    saves: tuple[Save, ...]


def history() -> list[Run]:
    """Every command that saved sets, newest first; a record that can't be read is skipped and said."""
    runs: dict[str, list[tuple[dict[str, str], Save]]] = {}
    journal = undo_journal()
    for record in sorted(journal.glob("*.json")) if journal.is_dir() else ():
        try:
            entry = json.loads(record.read_text(encoding="utf-8"))
            save = Save(
                record,
                pathlib.Path(entry["set"]),
                pathlib.Path(entry["backup"]),
                entry["sha256"],
                entry["backup_sha256"],
            )
            run, command, saved = entry["run"], entry["command"], entry["saved"]
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"Skipped a damaged undo record, {record}: {error}")
            continue
        runs.setdefault(run, []).append(({"command": command, "saved": saved}, save))
    ordered = sorted(runs.values(), key=lambda saves: saves[-1][1].record.name, reverse=True)
    return [Run(saves[0][0]["command"], saves[0][0]["saved"], tuple(save for _, save in saves)) for saves in ordered]


def restore(run: Run) -> list[str]:
    """Move each backup ``run`` names over its set, the current version backed up first; answers what failed.

    A set whose backup is gone loses its record, since nothing can bring it back; a set that can't be written keeps
    its record.
    """
    failed: list[str] = []
    for save in run.saves:
        if not save.backup_here:
            save.record.unlink(missing_ok=True)
            continue
        try:
            if save.path.is_file():
                create_backup(save.path)
            save.path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(save.backup, save.path)  # one rename: the backup folder sits beside the set, on its drive
        except OSError as error:
            failed.append(f"{save.path}: {error}")
            continue
        save.record.unlink(missing_ok=True)
    return failed


def backups_of(path: pathlib.Path, made: Mapping[pathlib.Path, float]) -> list[pathlib.Path]:
    """Every backup of the set at ``path`` in its ``abletoolz_backup``, newest first.

    Numbers are reused once an undo moves a backup away, and a backup keeps the modified time of the set it copies,
    which abletoolz's own saves leave alone, so neither orders backups. ``made`` says when the backups the history
    records were made; any other is ordered by when its file was created.
    """
    folder = path.parent / BACKUP_DIR
    name = re.compile(rf"{re.escape(path.stem)}__\d+{re.escape(path.suffix)}")
    found = [item for item in folder.iterdir() if name.fullmatch(item.name)] if folder.is_dir() else []

    def when(item: pathlib.Path) -> float:
        if item in made:
            return made[item]
        info = item.stat()
        # shortcut: Linux keeps no creation time, so there an unrecorded backup sorts by its set's modified time
        return float(getattr(info, "st_birthtime", info.st_mtime))

    return sorted(found, key=when, reverse=True)


def trimmed(runs: Sequence[Run], keep: int) -> dict[pathlib.Path, list[pathlib.Path]]:
    """For each set the history knows, its backups past the ``keep`` newest; sets with nothing to trim are left out."""
    made = {save.backup: save.record.stat().st_mtime for run in runs for save in run.saves}  # a record is written once
    sets = {save.path for run in runs for save in run.saves}
    old = {path: backups_of(path, made)[keep:] for path in sorted(sets)}
    return {path: backups for path, backups in old.items() if backups}


def trim(runs: Sequence[Run], old: dict[pathlib.Path, list[pathlib.Path]]) -> None:
    """Delete the ``old`` backups for good, and the history's records of them."""
    gone = {backup for backups in old.values() for backup in backups}
    for backup in gone:
        backup.unlink(missing_ok=True)
    for run in runs:
        for save in run.saves:
            if save.backup in gone:
                save.record.unlink(missing_ok=True)


def run_undo(argv: Sequence[str], ask: Callable[[str], str] = input) -> int:
    """``abletoolz undo [COUNT] [--list] [--trim KEEP]``; answers the exit code. ``ask`` asks, at a terminal."""
    parser = argparse.ArgumentParser(
        prog="abletoolz undo",
        description="Put back the sets abletoolz's last commands saved over, after asking.",
    )
    parser.add_argument(
        "count", nargs="?", type=int, default=1, help="How many commands to undo, newest first (default: the last one)"
    )
    parser.add_argument("--list", action="store_true", help="Show every command that can be undone, newest first")
    parser.add_argument(
        "--trim",
        type=int,
        metavar="KEEP",
        help="Keep each set's KEEP newest backups and delete the rest, after asking; they can't be undone after",
    )
    args = parser.parse_args(argv)
    if args.count < 1:
        parser.error("COUNT is how many commands to undo, 1 or more")
    if args.trim is not None and args.trim < 0:
        parser.error("KEEP is how many backups to keep for each set, 0 or more")
    runs = history()
    if not runs:
        print("Nothing to undo: abletoolz hasn't saved over any set yet.")
        return 0
    if args.list:
        for number, run in enumerate(runs, 1):
            print(f"{number}. {run.saved}  {run.command}  ({len(run.saves)} set(s))")
        return 0
    if args.trim is not None:
        old = trimmed(runs, args.trim)
        count = sum(len(backups) for backups in old.values())
        if not count:
            print(f"Every set has {args.trim} backup(s) or fewer; nothing to trim.")
            return 0
        print(f"Keeping each set's {args.trim} newest backup(s). These older ones will be deleted for good:")
        for path, backups in old.items():
            print(f"  {path}")
            for backup in backups:
                modified = datetime.datetime.fromtimestamp(backup.stat().st_mtime)
                print(f"    {backup.name}, last saved {modified:%Y-%m-%d %H:%M}")
        question = f"Delete {count} backup(s) of {len(old)} set(s)? They can't be undone after. [y/N] "
        if ask(question).strip().lower() not in ("y", "yes"):
            print("Nothing was deleted.")
            return 0
        trim(runs, old)
        print(f"Deleted {count} backup(s).")
        return 0
    chosen = runs[: args.count]
    if args.count > len(runs):
        print(f"Only {len(runs)} command(s) can be undone; undoing those.")
    restored: dict[pathlib.Path, str] = {}  # what the newer undos listed so far leave each set at
    for run in chosen:
        print(f"Undo {run.command}, run {run.saved}. Each set comes back from its backup:")
        for save in run.saves:
            print(save.listing(restored.get(save.path)))
        restored |= {save.path: save.backup_sha256 for save in run.saves if save.backup_here}
    sets = sum(save.backup_here for run in chosen for save in run.saves)
    if not sets:
        print("None of these backups are there any more, so nothing can be restored.")
        return 1
    if ask(f"Restore {sets} set(s) from {len(chosen)} command(s)? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Nothing was restored.")
        return 0
    failed = [problem for run in chosen for problem in restore(run)]  # newest first: a set ends at its oldest version
    for problem in failed:
        print(f"Couldn't restore {problem}")
    print(f"Restored {sets - len(failed)} set(s).")
    return 1 if failed else 0
