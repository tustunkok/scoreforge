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

The image is published to `ghcr.io/tustunkok/scoreforge` on every push to
`master`:

```bash
docker compose pull && docker compose up -d
```

or build it yourself:

```bash
docker compose build
```

The image bundles ffmpeg and MuseScore 4; model weights are baked in so
restarts are fast. The container listens on port **8766**.

```bash
curl http://localhost:8766/healthz
```

## Running it publicly

The service has no accounts, so the input path is deliberately narrow. Only
two link shapes are accepted:

```
https://youtu.be/ZOaFiTStPt4
https://www.youtube.com/watch?v=ZOaFiTStPt4
```

Everything else - other hosts, `http://`, Shorts, playlists, extra query
parameters, private or live videos - is rejected and the raw input is written
to the `scoreforge.abuse` log (one line per client per minute, so a hostile
client cannot flood the log).

The public build also:

* limits request bodies to 2 KB and refuses cross-site form posts;
* rate-limits submissions per client address and caps the queue depth;
* refuses new work when the data volume is low on space, and deletes results
  after `SCOREFORGE_RESULT_TTL_HOURS`;
* runs as UID 10001 with a read-only root filesystem, no capabilities and
  `no-new-privileges`.

Set `SCOREFORGE_TRUST_PROXY=1` **only** when a reverse proxy sets
`X-Forwarded-For`; otherwise the header is ignored and every client looks like
the proxy. Set `SCOREFORGE_ALLOWED_HOSTS` to your domain so that a poisoned
`Host` header cannot be used against you.

## Licensing

* ScoreForge's own code: **MIT**.
* The notation model [DuET](https://github.com/wei-zeng98/DuET) is MIT for the
  code but its **weights are CC BY-NC 4.0**.
* The audio frontend [Transkun](https://github.com/Yujia-Yan/Transkun) is MIT.

Because of the weights' non-commercial terms, **output is for personal,
non-commercial use only**. See `NOTICE.md`.

## Licence

MIT — see `LICENSE`.
