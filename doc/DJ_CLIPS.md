# Pre-warped clips for DJing

If you know your tracks' tempos and downbeats, from any analyzer or by hand, abletoolz can make them open in Live already warped and on the bar. Drag one in and it plays in time.

It makes two things, either or both:

- **`.alc` clips**: for each track, one clip from the start and one from the drop, in a folder of your User Library that Live's browser shows.
- **`.asd` sidecars**: a file beside each track's audio, so the track opens warped however you bring it in, from the browser, Explorer or Finder.

## The track list

Write one row per track. A spreadsheet saved as CSV works, or a JSON list of objects with the same names:

| Column | What it holds |
| --- | --- |
| `path` | the audio file |
| `bpm` | the track's tempo |
| `downbeat` | seconds to a downbeat, the first beat of any bar |
| `drop` | seconds to the drop, if you want a drop clip; it also decides the bar phase |
| `start` | where the full clip starts; the first downbeat when empty |
| `length` | the track's length; read from the file when empty |

Times are seconds into the file as FFmpeg-based analyzers count them. Other columns are ignored, so an analyzer can add its own.

```
path,bpm,downbeat,drop
D:/Music/Track.wav,174,0.152,66.358
D:/Music/Other.mp3,172,0.4,
```

## Make clips and sidecars

```bash
abletoolz-dj-crates tracks.csv --make alc asd --crate "DnB" --dry-run
abletoolz-dj-crates tracks.csv --make alc asd --crate "DnB"
```

`--make alc` writes the clips into the crate named by `--crate`. They point at your audio where it is, so nothing is copied; with `--audio wav` the crate gets its own WAV copies instead. `--include` and `--mirror` pick tracks and keep your folder layout; `--help` lists the rest.

`--make asd` writes a sidecar beside each track's audio, holding the full clip's grid. Write them before Live loads the files: a clip already in a set keeps its own markers. A sidecar where you saved clip settings in Live with a different grid is left alone, and the report lists it.

MP3s need no extra care. Live's MP3 decoder starts up to about 50 ms later than FFmpeg's, depending on how the file was encoded, and abletoolz reads each file's header to correct for it.

## From Python

```python
from pathlib import Path
from abletoolz.formats.asd import write_grid

audio = Path("D:/Music/Track.wav")
write_grid(Path(f"{audio}.asd"), audio_path=audio, bpm=174.0, anchor_seconds=0.152)
```

`anchor_seconds` is the downbeat that becomes bar 1, in Live's timeline. For an MP3, add `mp3_decoder_offset_seconds(audio)` from `abletoolz.dj_crates`, and pass `channel_count=read_channel_count(audio)`.
