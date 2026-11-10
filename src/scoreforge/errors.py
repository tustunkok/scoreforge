"""Typed, user-facing errors.

Every failure mode the web layer needs to explain to a human gets its own
exception with a short, friendly message. The CLI turns these into exit
codes; the web app turns them into the `error` field of a job.
"""

from __future__ import annotations


class ScoreForgeError(Exception):
    """Base class for all expected, user-facing failures."""

    #: short machine-readable code, used by the web API
    code = "error"

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.detail:
            return f"{self.message} ({self.detail})"
        return self.message


class InvalidSourceError(ScoreForgeError):
    """The submitted URL is not a usable public video link."""

    code = "invalid_source"


class SourceUnavailableError(ScoreForgeError):
    """The video could not be fetched (private, removed, geo-blocked, ...)."""

    code = "source_unavailable"


class SourceTooLongError(ScoreForgeError):
    """The video is longer than the allowed limit."""

    code = "source_too_long"


class DownloadError(ScoreForgeError):
    """Downloading the audio failed."""

    code = "download_failed"


class TranscriptionError(ScoreForgeError):
    """The audio -> MIDI or MIDI -> notation stage failed."""

    code = "transcription_failed"


class EngravingError(ScoreForgeError):
    """MuseScore could not render the score."""

    code = "engraving_failed"


class MuseScoreNotFoundError(ScoreForgeError):
    """MuseScore 4 is not installed where we expected it."""

    code = "musescore_not_found"


class TimeoutError_(ScoreForgeError):
    """A stage exceeded its time budget."""

    code = "timeout"


class StorageFullError(ScoreForgeError):
    """The server cannot accept another job right now."""

    code = "storage_full"
