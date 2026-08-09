"""Create sample database for fast searches and fixing broken sample paths."""

import json
import logging
import pathlib

import tqdm

from abletoolz.misc import DEFAULT_DB_PATH

logger = logging.getLogger(__name__)

DatabaseT = dict[str, dict[str, dict[str, str]]]


def get_all_audio_files(path: pathlib.Path) -> list[pathlib.Path]:
    """Find all supported audio files in directory."""
    file_suffixes = [
        "*.aiff",
        "*.AIFF",
        "*.aif",
        "*.AIF",
        "*.wav",
        "*.WAV",
        "*.mp3",
        "*.MP3",
        "*.flac",
        "*.FLAC",
        "*.ogg",
        "*.OGG",
        "*.mp4",
        "*.MP4",
        "*.rx2",
    ]
    all_files: list[pathlib.Path] = []
    for file_type in file_suffixes:
        all_files.extend(list(path.rglob(file_type)))
    return all_files


def create_or_update_db(paths: list[str], db_path: pathlib.Path | None = None) -> pathlib.Path:
    """Search all samples and add to database, create new one if it doesn't exist.

    db {
        path_string: {
            file_size: sample file size,
            # Crc unsupported for now, can't figure out ableton's crc algorithm! This would allow for
            # near perfect matches, although file size and perfectly matching file name is probably good enough.
            crc: crc size
            last_modified: time of last file modification.
        }
    }
    """
    if db_path is None:
        db_path = DEFAULT_DB_PATH
    # Create the parent dir now, before the (potentially long) scan — a first run
    # on a fresh machine must not crash at write time after scanning everything.
    db_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info(
        "Using db path %s. Creating database from scratch can take a while, please be patient. Updating "
        "an existing one is much faster!",
        db_path.resolve(),
    )
    if not db_path.exists():
        db = {}
    else:
        with db_path.open("r") as f:
            db = json.load(f)
    all_files = []
    for path in paths:
        all_files.extend(get_all_audio_files(pathlib.Path(path)))

    # Remove any broken paths.
    to_remove: list[str] = []
    for path in tqdm.tqdm(db.keys(), desc="Validating current db..."):
        if not pathlib.Path(path).exists():
            to_remove.append(path)
    for path in to_remove:
        db.pop(path)

    for sample in tqdm.tqdm(all_files, desc="Progress"):
        if str(sample.resolve()) in db:
            continue
        db[str(sample.resolve())] = {
            "name": sample.name,
            "size": sample.stat().st_size,
            "last_modified": sample.stat().st_mtime,
        }

    # Write db!
    with db_path.open("w") as f:
        json.dump(db, f, indent=2, sort_keys=True)
    logger.info("Updated database at %s", db_path.resolve())
    return db_path


def load_db(db_path: pathlib.Path | None = None) -> DatabaseT:
    """Load db from json."""
    if db_path is None:
        db_path = DEFAULT_DB_PATH
    if not db_path.exists():
        raise FileNotFoundError(f"Database {db_path} doesn't exist! Run --db with sample dir(s) first.")
    with db_path.open() as f:
        return json.load(f)
