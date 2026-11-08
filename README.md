# ScoreForge

Turn a piano recording into engraved sheet music.

ScoreForge listens to a public YouTube video of a piano performance and
produces an engraved score: **PDF**, **MusicXML**, **MuseScore project**,
**MIDI** and page images — packaged in a single zip.

> This is a machine-generated *first draft*. Note durations, hand assignment
> and pedalling are inferred from the audio and are often imperfect.

## How it works

```
YouTube URL
   │  yt-dlp
   ▼
 audio (44.1 kHz mono)
   │  Transkun V2        (audio  → performance MIDI)
   ▼
 performance MIDI
   │  DuET               (MIDI   → MusicXML + score MIDI)
   ▼
 MusicXML
   │  MuseScore 4        (notation → PDF / MSCZ / PNG pages)
   ▼
 scoreforge-<title>.zip
```

## Install

```bash
git clone https://github.com/tustunkok/scoreforge.git
cd scoreforge
uv sync --extra web
```

MuseScore 4 must be installed separately (it is GPL and not redistributed
here). Point `SCOREFORGE_MUSESCORE` at the executable if it is not in a
standard location.

Check your environment:

```bash
uv run scoreforge doctor
```

## Use

Convert one video:

```bash
uv run scoreforge convert https://youtu.be/VIDEO_ID -o out
```

Run the web application:

```bash
uv run scoreforge serve --host 0.0.0.0 --port 8000
```

The web app queues submissions and processes **one job at a time**; the job
page shows the stage, the queue position and an ETA.

Submitting the same video twice while the result is still alive returns the
existing job instead of transcribing it again - links that differ only in
their shape (`youtu.be/ID`, `watch?v=ID`, `/shorts/ID`) are recognised as
the same video.

## Logging

Everything from `DEBUG` to `CRITICAL` is logged to stdout. Choose the level
with `SCOREFORGE_LOG_LEVEL`:

```bash
SCOREFORGE_LOG_LEVEL=DEBUG uv run scoreforge serve
```

Set `SCOREFORGE_LOG_FILE` to also write to a rotating log file.

## Docker

```bash
docker compose up -d
```

The image bundles ffmpeg and MuseScore 4; model weights are cached in a
volume so restarts are fast.

## Licensing

* ScoreForge's own code: **MIT**.
* The notation model [DuET](https://github.com/wei-zeng98/DuET) is MIT for the
  code but its **weights are CC BY-NC 4.0**.
* The audio frontend [Transkun](https://github.com/Yujia-Yan/Transkun) is MIT.

Because of the weights' non-commercial terms, **output is for personal,
non-commercial use only**. See `NOTICE.md`.

## Licence

MIT — see `LICENSE`.
