# Contributing to ScoreForge

Thanks for helping. This project is young, so plain issues and pull
requests are very welcome.

## Development setup

```bash
git clone https://github.com/tustunkok/scoreforge.git
cd scoreforge
uv sync --extra web --extra dev
uv run pytest
```

MuseScore 4 is needed for the PDF/MSCZ output. It is GPL and therefore not
vendored in this repository; install it from
[musescore.org/download](https://musescore.org/download) and set
`SCOREFORGE_MUSESCORE` if it is not in a standard location.

## How the code is arranged

| Module | Responsibility |
|---|---|
| `scoreforge.pipeline` | audio → MIDI → MusicXML → PDF |
| `scoreforge.jobs` | SQLite job records |
| `scoreforge.queue` | the serial worker and the ETA |
| `scoreforge.service` | glue between pipeline, queue and storage |
| `scoreforge.package` | building the downloadable zip |
| `scoreforge.web` | the FastAPI application |

## Tests

We use test-driven development. Please add a test with every change:

* write the failing test first,
* then the smallest change that makes it pass,
* keep the test at a public seam (the service API, the HTTP routes, the
  packaging function) rather than poking at internals.

```bash
uv run pytest -q
uv run ruff check .
```

## Commit messages

We use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(web): show the queue position on the job page
fix(queue): count the running job towards the position
docs(readme): explain the Docker setup
```

## A note on models

Do not commit model weights. They are large and carry their own licences
(see `NOTICE.md`). The Docker build downloads them into the image; local
runs fetch them into the Hugging Face cache on first use.

## Licensing of contributions

By contributing you agree that your work is licensed under the MIT licence
of this project.
