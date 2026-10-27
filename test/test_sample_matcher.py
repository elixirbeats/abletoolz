"""Candidate lookup must scale with matching names and reuse audio metadata."""

from __future__ import annotations

import os
import pathlib
import wave
from unittest.mock import patch

from mutagen import File as mutagen_file

from abletoolz.samples import matcher
from abletoolz.samples.catalog import SampleCatalog, SampleValue
from abletoolz.samples.relink import Evidence, find


def best(
    catalog: SampleCatalog,
    name: str,
    original: pathlib.Path,
    *,
    size: int | None = None,
    mtime: int | None = None,
    length: float | None = None,
) -> pathlib.Path | None:
    """The file a relink would pick, through the one lookup both relink paths use."""
    return find(catalog, Evidence(name, original, size=size, mtime=mtime, length=length)).found


def ranked(
    catalog: SampleCatalog, name: str, original: pathlib.Path, *, size: int | None = None, mtime: int | None = None
) -> tuple[pathlib.Path, ...]:
    """Every file the lookup keeps, best first."""
    return find(catalog, Evidence(name, original, size=size, mtime=mtime)).candidates


def test_indexed_lookup_never_iterates_unrelated_records(tmp_path: pathlib.Path) -> None:
    records: dict[str, dict[str, SampleValue]] = {
        str(tmp_path / f"sample-{number}.wav"): {"name": f"sample-{number}.wav", "size": number}
        for number in range(1000)
    }
    catalog = SampleCatalog.from_records(records)
    target = tmp_path / "sample-500.wav"
    target.touch()
    assert ranked(catalog, target.name, target) == (target,)
    assert best(catalog, target.name, target) == target
    assert ranked(catalog, "absent.wav", target) == ()


def test_size_mtime_and_folder_ranking(tmp_path: pathlib.Path) -> None:
    correct = tmp_path / "Drums" / "kick.wav"
    other = tmp_path / "Other" / "kick.wav"
    for path in [correct, other]:
        path.parent.mkdir()
        path.touch()
    records: dict[str, dict[str, SampleValue]] = {
        str(other): {"name": other.name, "size": 8, "last_modified": 10.5},
        str(correct): {"name": correct.name, "size": 8, "last_modified": 20.5},
    }
    catalog = SampleCatalog.from_records(records)
    assert ranked(catalog, "kick.wav", correct, size=8, mtime=20) == (correct, other)
    assert best(catalog, "kick.wav", correct, size=8, mtime=20) == correct


def test_folder_ranking_understands_windows_paths_on_other_platforms(tmp_path: pathlib.Path) -> None:
    correct = tmp_path / "Packs" / "Drums" / "kick.wav"
    other = tmp_path / "Other" / "kick.wav"
    for path in [correct, other]:
        path.parent.mkdir(parents=True)
        path.touch()
    catalog = SampleCatalog.from_records({str(path): {"name": path.name} for path in [other, correct]})
    assert ranked(catalog, "kick.wav", pathlib.Path(r"D:\Old\Packs\Drums\kick.wav")) == (correct, other)


def test_ranking_uses_metadata_under_the_original_database_path_key(tmp_path: pathlib.Path) -> None:
    correct = tmp_path / "Longer" / "Folder" / "kick.wav"
    other = tmp_path / "kick.wav"
    correct.parent.mkdir(parents=True)
    correct.touch()
    other.touch()
    catalog = SampleCatalog.from_records(
        {
            other.as_posix(): {"name": "kick.wav", "size": 1},
            correct.as_posix(): {"name": "kick.wav", "size": 2},
        }
    )
    assert best(catalog, "kick.wav", pathlib.Path("unrelated/kick.wav"), size=2) == correct


def test_selection_skips_an_offline_candidate_that_would_rank_first(tmp_path: pathlib.Path) -> None:
    missing = tmp_path / "Offline" / "kick.wav"
    available = tmp_path / "kick.wav"
    available.touch()
    catalog = SampleCatalog.from_records({str(path): {"name": path.name} for path in [missing, available]})
    assert ranked(catalog, "kick.wav", missing) == (available,)
    assert best(catalog, "kick.wav", missing) == available
    available.unlink()
    assert best(catalog, "kick.wav", missing) is None


def write_wav(path: pathlib.Path, frames: int) -> None:
    with wave.open(str(path), "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(1000)
        file.writeframes(b"\0\0" * frames)


def test_duration_is_read_once_per_file_revision(tmp_path: pathlib.Path) -> None:
    sample = tmp_path / "kick.wav"
    write_wav(sample, 1000)
    with patch.object(matcher, "mutagen_file", wraps=mutagen_file) as read:
        assert matcher.get_audio_length_seconds(sample) == 1.0
        assert matcher.get_audio_length_seconds(sample) == 1.0
        assert read.call_count == 1
        old_mtime = sample.stat().st_mtime_ns
        write_wav(sample, 2000)
        os.utime(sample, ns=(old_mtime, old_mtime))
        assert matcher.get_audio_length_seconds(sample) == 2.0
        assert read.call_count == 2


def test_duration_is_unknown_for_a_missing_sample(tmp_path: pathlib.Path) -> None:
    assert matcher.get_audio_length_seconds(tmp_path / "missing.wav") is None


def test_duration_ranking_reuses_reads_across_repeated_queries(tmp_path: pathlib.Path) -> None:
    short = tmp_path / "Short" / "kick.wav"
    long = tmp_path / "Long" / "kick.wav"
    for path, frames in [(short, 1000), (long, 2000)]:
        path.parent.mkdir()
        write_wav(path, frames)
    catalog = SampleCatalog.from_records({str(path): {"name": path.name} for path in [short, long]})
    with patch.object(matcher, "mutagen_file", wraps=mutagen_file) as read:
        for _ in range(5):
            assert best(catalog, "kick.wav", short, length=2.0) == long
        assert read.call_count == 2


def test_only_files_the_reference_allows_have_their_length_read(tmp_path: pathlib.Path) -> None:
    kept = [tmp_path / folder / "kick.wav" for folder in ("A", "B")]
    ruled_out = tmp_path / "C" / "kick.wav"
    for path in [*kept, ruled_out]:
        path.parent.mkdir()
        write_wav(path, 1000)
    size = kept[0].stat().st_size
    records: dict[str, dict[str, SampleValue]] = {str(path): {"name": path.name, "size": size} for path in kept}
    records[str(ruled_out)] = {"name": ruled_out.name, "size": size + 1}
    catalog = SampleCatalog.from_records(records)
    with patch.object(matcher, "get_audio_length_seconds", wraps=matcher.get_audio_length_seconds) as measure:
        lookup = find(catalog, Evidence("kick.wav", kept[0], size=size, length=1.0))
    assert lookup.candidates == tuple(kept)
    assert sorted(call.args[0] for call in measure.call_args_list) == kept
