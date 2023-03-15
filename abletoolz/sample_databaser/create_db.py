"""Create sample database for fast searches and fixing broken sample paths."""

import json
import logging
import pathlib
from typing import Dict, List, Optional

import tqdm

from abletoolz.misc import DEFAULT_DB_PATH

logger = logging.getLogger(__name__)


def get_all_audio_files(path: pathlib.Path) -> List[pathlib.Path]:
    """Find all supported audio files in directory."""
    aiff_files = path.rglob("*.aiff")
    aif_files = path.rglob("*.aif")
    wav_files = path.rglob("*.wav")
    mp3_files = path.rglob("*.mp3")
    flacc_files = path.rglob("*.flacc")
    ogg_files = path.rglob("*.ogg")
    mp4_files = path.rglob("*.mp4")

    all_files: List[pathlib.Path] = []
    for file_type in (
            aiff_files,
            wav_files,
            mp3_files,
            flacc_files,
            ogg_files,
            mp4_files,
    ):
        all_files.extend(list(file_type))
    return all_files


def create_or_update_db(paths: List[str], db_path: Optional[pathlib.Path] = None) -> pathlib.Path:
    """Search all samples and add to database, create new one if it doesn't exist.

    db {
        path_string: {
            file_size: sample file size,
            crc: crc size # Unsupported for now, can't figure out ableton's crc algorithm!
            last_modified: time of last file modification.
        }
    }
    """
    if db_path is None:
        db_path = DEFAULT_DB_PATH
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

    for sample in tqdm.tqdm(all_files, desc="Progress"):
        if str(sample.resolve()) in db:
            continue
        db[str(sample.resolve())] = {
            "name": sample.name,
            "size": sample.stat().st_size,
            "last_modified": sample.stat().st_mtime,
        }
    with db_path.open("w") as f:
        json.dump(db, f, indent=2, sort_keys=True)
    logger.info("Updated database at %s", db_path.resolve())
    return db_path


def load_db() -> Dict[str, Dict[str, str]]:
    """Load db from json."""
    if not DEFAULT_DB_PATH.exists():
        raise FileNotFoundError("Database doesn't exist! Run --db with sample dir(s) first.")
    with DEFAULT_DB_PATH.open() as f:
        return json.load(f)
