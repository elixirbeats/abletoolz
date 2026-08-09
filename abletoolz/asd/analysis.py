"""abletoolz.asd.analysis  —  BPM detection and first-beat onset finding.

Requires: librosa, scipy

Designed for full DJ tracks, not isolated stems.  Stems have different
spectral content and onset profiles — run analysis on the source file.

Key design choices:
  - Loads only the first `analysis_duration` seconds to stay fast.
  - Uses a Gaussian BPM prior so genre knowledge (DnB ≈ 174) steers the
    tracker without hard-locking it.
  - Half-time correction: if the tracker lands in the 80-100 BPM range it
    is doubled (librosa commonly halves DnB tempo).
  - First beat is snapped to the nearest strong onset within ±snap_ms ms.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, Unpack


class _AnalyseKwargs(TypedDict, total=False):
    """Optional keyword arguments forwarded from analyse_for_dnb to analyse."""

    bpm_prior: float
    bpm_prior_sigma: float
    analysis_duration: float
    snap_ms: float
    half_time_range: tuple[float, float]


@dataclass
class BeatAnalysis:
    bpm: float
    first_beat_seconds: float
    beat_times: list[float]  # all detected beat times in seconds (first 90 s)
    was_half_time_corrected: bool


def analyse(
    audio_path: Path,
    *,
    bpm_prior: float = 120.0,
    bpm_prior_sigma: float = 15.0,
    analysis_duration: float = 90.0,
    snap_ms: float = 60.0,
    half_time_range: tuple[float, float] = (80.0, 100.0),
) -> BeatAnalysis:
    """Detect BPM and the first beat onset in an audio file.

    Parameters
    ----------
    audio_path : Path
        Any format librosa can open (mp3, wav, flac, aiff, …).
    bpm_prior : float
        Centre of the Gaussian BPM prior.  Set to ~174 for DnB, ~128 for
        house, ~140 for techno, etc.
    bpm_prior_sigma : float
        Width of the prior.  15 BPM is loose enough to handle natural
        variation while still nudging away from half/double-time errors.
    analysis_duration : float
        Seconds of audio to load.  90 s is usually enough for the groove to
        establish.  Increase for tracks with very long intros.
    snap_ms : float
        Maximum distance (ms) to snap the first beat time to a detected onset.
    half_time_range : tuple[float, float]
        If the detected BPM falls inside this range the result is doubled.
        DnB is almost always halved to ~87 BPM by librosa.

    Returns
    -------
    BeatAnalysis

    """
    try:
        import librosa
        import numpy as np
        from scipy.stats import norm as scipy_norm
    except ImportError as e:
        raise ImportError(
            "librosa and scipy are required for beat analysis. Install them with: pip install librosa scipy"
        ) from e

    y, sr = librosa.load(str(audio_path), sr=44100, mono=True, duration=analysis_duration)
    hop = 512
    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)

    prior = scipy_norm(loc=bpm_prior, scale=bpm_prior_sigma)
    tempo_arr, beat_frames = librosa.beat.beat_track(
        onset_envelope=onset_env,
        sr=sr,
        hop_length=hop,
        prior=prior,
        trim=False,
    )
    bpm = float(np.atleast_1d(tempo_arr)[0])

    half_lo, half_hi = half_time_range
    corrected = False
    if half_lo <= bpm <= half_hi:
        bpm *= 2.0
        corrected = True

    beat_times = librosa.frames_to_time(beat_frames, sr=sr, hop_length=hop).tolist()
    first_beat = float(beat_times[0]) if beat_times else 0.0

    # Snap to the nearest strong onset
    onset_times = librosa.onset.onset_detect(y=y, sr=sr, units="time", backtrack=True)
    if len(onset_times) > 0:
        diffs = np.abs(onset_times - first_beat)
        nearest = int(np.argmin(diffs))
        if diffs[nearest] < (snap_ms / 1000.0):
            first_beat = float(onset_times[nearest])

    return BeatAnalysis(
        bpm=bpm,
        first_beat_seconds=first_beat,
        beat_times=beat_times,
        was_half_time_corrected=corrected,
    )


def analyse_for_dnb(audio_path: Path, **kwargs: Unpack[_AnalyseKwargs]) -> BeatAnalysis:
    """Shortcut with DnB-appropriate defaults (174 BPM prior)."""
    kwargs.setdefault("bpm_prior", 174.0)
    return analyse(audio_path, **kwargs)
