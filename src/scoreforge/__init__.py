"""ScoreForge - turn a piano recording into engraved sheet music.

The public API is deliberately small::

    from scoreforge import ScoreForgeService, Settings

    service = ScoreForgeService(Settings(data_dir=Path("data")))
    service.start()
    job = service.submit("https://youtu.be/...")
    print(service.status(job.id))
"""

from __future__ import annotations

from .errors import (
    DownloadError,
    EngravingError,
    InvalidSourceError,
    MuseScoreNotFoundError,
    ScoreForgeError,
    SourceTooLongError,
    SourceUnavailableError,
    StorageFullError,
    TimeoutError_,
    TranscriptionError,
)
from .jobs import Job, JobStore
from .package import build_package, slugify
from .pipeline import DEFAULT_MODEL, run_pipeline, validate_source
from .queue import JobQueue
from .service import ScoreForgeService, Settings

__version__ = "0.1.0"

__all__ = [
    "ScoreForgeService",
    "Settings",
    "Job",
    "JobStore",
    "JobQueue",
    "run_pipeline",
    "validate_source",
    "build_package",
    "slugify",
    "DEFAULT_MODEL",
    "ScoreForgeError",
    "InvalidSourceError",
    "SourceUnavailableError",
    "SourceTooLongError",
    "DownloadError",
    "TranscriptionError",
    "EngravingError",
    "MuseScoreNotFoundError",
    "TimeoutError_",
    "StorageFullError",
    "__version__",
]


def main(argv: list[str] | None = None) -> int:
    """Console entry point (``scoreforge``)."""
    from .cli import main as cli_main

    return cli_main(argv)
