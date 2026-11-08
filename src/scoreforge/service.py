"""The service layer: one place that ties the pipeline, the queue and the
packaging together. Both the CLI and the web app use it.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .errors import ScoreForgeError
from .jobs import DONE, EXPIRED, FAILED, Job, JobStore
from .package import build_package
from .pipeline import DEFAULT_MODEL, run_pipeline
from .queue import JobQueue

log = logging.getLogger("scoreforge.service")

#: How long a finished job's artifacts are kept (hours).
RESULT_TTL_HOURS = 24


@dataclass
class Settings:
    """Runtime settings, normally read from the environment."""

    data_dir: Path = Path("data")
    device: str = "cpu"
    model: str = DEFAULT_MODEL
    musescore: str | None = None
    keep_audio: bool = False
    result_ttl_hours: int = RESULT_TTL_HOURS
    max_duration_seconds: int = 30 * 60

    @classmethod
    def from_env(cls) -> Settings:
        import os

        data_dir = Path(os.environ.get("SCOREFORGE_DATA_DIR", "data"))
        return cls(
            data_dir=data_dir,
            device=os.environ.get("SCOREFORGE_DEVICE", "cpu"),
            model=os.environ.get("SCOREFORGE_MODEL", DEFAULT_MODEL),
            musescore=os.environ.get("SCOREFORGE_MUSESCORE") or None,
            keep_audio=os.environ.get("SCOREFORGE_KEEP_AUDIO", "0") == "1",
            result_ttl_hours=int(
                os.environ.get("SCOREFORGE_RESULT_TTL_HOURS", str(RESULT_TTL_HOURS))
            ),
        )

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "scoreforge.db"


class ScoreForgeService:
    """Owns the store, the queue and the artifacts on disk."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings.from_env()
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = JobStore(self.settings.db_path)
        self.queue = JobQueue(self.store, self.settings.jobs_dir)
        self.queue.set_handler(self._handle)

    # -- lifecycle -------------------------------------------------------- #
    def start(self) -> None:
        self.queue.start()

    def stop(self) -> None:
        self.queue.stop()

    # -- public API ------------------------------------------------------- #
    def submit(self, url: str) -> Job:
        """Validate and queue a job. Returns the queued job."""
        from .pipeline import validate_source

        url = validate_source(url)
        return self.queue.submit(url)

    def get(self, job_id: str) -> Job | None:
        return self.store.get(job_id)

    def list_jobs(self, limit: int = 50) -> list[Job]:
        return self.store.list_jobs(limit=limit)

    def status(self, job_id: str) -> dict:
        """Full status payload for the UI: stage, position, ETA."""
        job = self.store.get(job_id)
        if job is None:
            return {"error": "not_found"}
        eta = self.queue.eta_for(job_id)
        payload = job.as_dict()
        payload["eta"] = eta.as_dict()
        payload["queue_position"] = self.store.queue_position(job_id)
        return payload

    def artifact_path(self, job_id: str, name: str) -> Path | None:
        job = self.store.get(job_id)
        if job is None or job.status != DONE:
            return None
        path = self.job_dir(job_id) / name
        return path if path.exists() else None

    def job_dir(self, job_id: str) -> Path:
        return self.settings.jobs_dir / job_id

    # -- worker handler --------------------------------------------------- #
    def _handle(self, queue: JobQueue, job: Job) -> None:
        out_root = self.job_dir(job.id)
        out_root.mkdir(parents=True, exist_ok=True)

        def progress(stage: str, pct: int) -> None:
            queue.report(job.id, stage, pct)

        meta = run_pipeline(
            job.url,
            out_root,
            device=self.settings.device,
            musescore=self.settings.musescore,
            keep_audio=self.settings.keep_audio,
            model=self.settings.model,
            progress=progress,
        )
        progress("package", 92)
        archive = build_package(
            Path(meta["score_dir"]),
            out_root,
            title=meta["title"],
            source=meta["source"],
            pages=[Path(p) for p in meta.get("pages", [])],
        )
        self.store.update(
            job.id,
            title=meta["title"],
            artifacts={
                "zip": str(archive),
                "pdf": meta.get("pdf"),
                "musicxml": meta.get("musicxml"),
                "mscz": meta.get("mscz"),
                "pages": meta.get("pages", []),
                "size": archive.stat().st_size,
            },
            progress=100,
        )

    # -- housekeeping ----------------------------------------------------- #
    def expire_old_jobs(self, *, now: datetime | None = None) -> int:
        """Delete artifacts and mark jobs expired once the TTL has passed."""
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(hours=self.settings.result_ttl_hours)
        removed = 0
        for job in self.store.list_jobs(limit=10000):
            if job.status not in (DONE, FAILED):
                continue
            stamp = job.finished_at or job.created_at
            try:
                finished = datetime.fromisoformat(stamp)
            except Exception:
                continue
            if finished < cutoff:
                shutil.rmtree(self.job_dir(job.id), ignore_errors=True)
                self.store.update(job.id, status=EXPIRED, artifacts={})
                removed += 1
        return removed

    def queue_snapshot(self) -> dict:
        return self.queue.snapshot()


__all__ = ["ScoreForgeService", "Settings", "ScoreForgeError"]
