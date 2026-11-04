# Pre-warped clips for DJing

If your tracks already have a tempo, a first downbeat and a drop cue from any analyzer, abletoolz can turn them into clips that land in Live already warped and on the bar. Drag one in and it plays in time, starting at the top of the track or right on the drop.

There are two ways to carry that grid:

- **`.alc` clip files**, made by `abletoolz-dj-crates`: one clip from the top and one from the drop for each track, in a folder of your User Library that Live's browser shows.
- **`.asd` sidecars** beside the audio files, so the track itself opens warped however you bring it in. These are written from Python for now.

## Build a crate of `.alc` clips

`abletoolz-dj-crates` reads a tab-separated file, one track per row. Cue Finder writes it directly; from another tool, write the columns yourself:

| Column | What it holds |
| --- | --- |
| `path`, `filename` | the audio file, and its name for the clip |
| `bpm` | the track's constant tempo |
| `beat_offset_s` | seconds to the first downbeat |
| `drop_s` | seconds to the drop; empty for no drop clip |
| `start_s`, `duration_s` | where the full clip starts, and the track length |
| `samplerate`, `mp3_tag`, `mp3_lame_ext`, `mp3_encoder_delay` | for an MP3, its sample rate, first frame tag (`Info`, `Xing` or empty), whether it has a LAME gapless block, and that block's encoder delay; empty for WAV and FLAC |

The file also needs the columns `title_key`, `start_beat`, `start_bar`, `drop_beat`, `drop_bar`, `bars_start_to_drop`, `confidence`, `needs_review`, `verdict` and `human_reviewed`. They only filter and annotate, so `0`, `False` and empty text are fine. A row:

```
path	filename	title_key	duration_s	bpm	beat_offset_s	start_s	start_beat	start_bar	drop_s	drop_beat	drop_bar	bars_start_to_drop	confidence	needs_review	verdict	human_reviewed	mp3_tag	mp3_lame_ext	mp3_encoder_delay	samplerate
D:/Music/Track.wav	Track.wav	track	312.5	174	0.152	0.152	0	0	66.358	192	48	48	1	False		True				
```

Check what it will write, then write it:

```bash
abletoolz-dj-crates picks.tsv --crate "DnB" --dry-run
abletoolz-dj-crates picks.tsv --crate "DnB"
```

The clips point at your audio where it is, so nothing is copied. With `--audio wav` the crate gets its own WAV copies instead. `--include`, `--reviewed-only` and `--mirror` pick tracks and keep your folder layout; `--help` lists the rest.

## Write `.asd` sidecars

```python
from pathlib import Path
from abletoolz.formats.asd import write_grid

audio = Path("D:/Music/Track.wav")
write_grid(Path(f"{audio}.asd"), audio_path=audio, bpm=174.0, anchor_seconds=0.152)
```

`anchor_seconds` is the downbeat that becomes bar 1. Write the sidecar before Live loads the file: a clip already in a set keeps its own markers.

## MP3 timing

Live's MP3 decoder starts up to about 50 ms later than FFmpeg's, depending on how the file was encoded. The crate builder corrects for it from the four MP3 columns, so the times in your file should be the ones an FFmpeg-based analyzer reports, as Cue Finder's are.

`write_grid` takes times already in Live's timeline. For an MP3, add the same correction yourself:

```python
from abletoolz.dj_crates import mp3_decoder_offset_seconds

offset = mp3_decoder_offset_seconds(audio, sample_rate=44100)  # this file's sample rate
write_grid(Path(f"{audio}.asd"), audio_path=audio, bpm=174.0, anchor_seconds=0.152 + offset)
```
