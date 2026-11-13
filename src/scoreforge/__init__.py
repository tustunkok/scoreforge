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
    MuseScoreUnusableError,
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

#: The single source of truth for the released version.
#:
#: Releases follow `Semantic Versioning <https://semver.org/>`_. The value is
#: kept in step with ``pyproject.toml`` by ``uv version --bump`` and a test
#: (``tests/test_version.py``) fails the build if the two ever drift apart.
__version__ = "1.0.2"

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
    "MuseScoreUnusableError",
    "TimeoutError_",
    "StorageFullError",
    "__version__",
]


def main(argv: list[str] | None = None) -> int:
    """Console entry point (``scoreforge``)."""
    from .cli import main as cli_main

    return cli_main(argv)
