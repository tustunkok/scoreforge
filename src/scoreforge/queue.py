"""A single-worker job queue.

Design constraints:

* **Strictly serial.** Exactly one job is processed at a time. Transcribing
  is CPU heavy; running two would slow both down and double peak memory.
* **Visible.** Every job reports its stage and percentage, plus its position
  in the queue and an ETA.
* **Restart safe.** Queued jobs live in SQLite; a job that was running when
  the process died is put back at the front of the queue on startup.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .errors import ScoreForgeError
from .jobs import DONE, FAILED, QUEUED, RUNNING, Job, JobStore

log = logging.getLogger("scoreforge.queue")

#: Rough per-minute-of-audio processing times, used until we have real data.
#: Filled in from measured runs; the ETA is only a hint.
DEFAULT_SECONDS_PER_AUDIO_MINUTE = 90.0


@dataclass
class Eta:
    seconds: float | None
    basis: str

    def as_dict(self) -> dict:
        return {"seconds": self.seconds, "basis": self.basis}


class JobQueue:
    """Serial background worker over a :class:`JobStore`."""

    def __init__(
        self,
        store: JobStore,
        work_root: Path,
        *,
        handler=None,
        audio_seconds: float = 0.0,
    ) -> None:
        self.store = store
        self.work_root = Path(work_root)
        self.work_root.mkdir(parents=True, exist_ok=True)
        self._handler = handler
        self._audio_seconds = audio_seconds
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._current: str | None = None
        self._idle_since: float | None = None
        self._idle_total = 0.0
        self._idle_start: float | None = None
        self._measured: list[float] = []  # wall seconds per job
        self._started_at = time.time()

    # -- lifecycle -------------------------------------------------------- #
    def set_handler(self, handler) -> None:
        self._handler = handler

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        requeued = self.store.requeue_stale()
        if requeued:
            log.info("requeued %d interrupted job(s)", requeued)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="scoreforge-worker",
                                        daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    # -- queue operations ------------------------------------------------- #
    def submit(self, url: str) -> Job:
        job = self.store.create(url)
        self._wake.set()
        return job

    def position_of(self, job_id: str) -> int | None:
        return self.store.queue_position(job_id)

    def eta_for(self, job_id: str) -> Eta:
        """Estimate when a job will be finished."""
        job = self.store.get(job_id)
        if job is None:
            return Eta(None, "unknown job")
        if job.status == DONE:
            return Eta(0.0, "finished")
        if job.status == RUNNING:
            return Eta(self._remaining_for_running(job), "in progress")
        if job.status != QUEUED:
            return Eta(None, "not queued")

        ahead = self.store.queue_position(job_id) or 1
        per_job = self._average_job_seconds()
        return Eta(
            per_job * ahead,
            f"~{per_job / 60:.1f} min per job, {ahead} job(s) ahead",
        )

    def _remaining_for_running(self, job: Job) -> float | None:
        started = job.started_at
        if not started:
            return None
        try:
            elapsed = (datetime.now(UTC) - datetime.fromisoformat(started)).total_seconds()
        except Exception:
            elapsed = 0.0
        per_job = self._average_job_seconds()
        return max(0.0, per_job - elapsed)

    def _average_job_seconds(self) -> float:
        if self._measured:
            recent = self._measured[-10:]
            return sum(recent) / len(recent)
        audio_minutes = max(self._audio_seconds / 60.0, 0.5)
        return audio_minutes * DEFAULT_SECONDS_PER_AUDIO_MINUTE

    # -- worker ----------------------------------------------------------- #
    def _loop(self) -> None:
        while not self._stop.is_set():
            job = self.store.next_queued()
            if job is None:
                self._wake.wait(timeout=1.0)
                self._wake.clear()
                continue
            self._run_job(job)

    def _run_job(self, job: Job) -> None:
        self._current = job.id
        started = time.time()
        self.store.update(
            job.id,
            status=RUNNING,
            stage="starting",
            progress=1,
            started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        try:
            if self._handler is None:
                raise ScoreForgeError("No job handler was configured.")
            self._handler(self, job)
        except ScoreForgeError as exc:
            log.warning("job %s failed: %s", job.id, exc)
            self.store.update(
                job.id, status=FAILED, stage="failed", error=str(exc),
                finished_at=datetime.now(UTC).isoformat(timespec="seconds"),
            )
        except Exception as exc:  # pragma: no cover - unexpected
            log.exception("job %s crashed", job.id)
            self.store.update(
                job.id, status=FAILED, stage="failed",
                error=f"Unexpected error: {exc}",
                finished_at=datetime.now(UTC).isoformat(timespec="seconds"),
            )
        else:
            self.store.update(
                job.id, status=DONE, stage="done", progress=100,
                finished_at=datetime.now(UTC).isoformat(timespec="seconds"),
            )
        finally:
            self._measured.append(time.time() - started)
            self._current = None
            self._wake.clear()

    # -- progress reporting (called by the handler) ---------------------- #
    def report(self, job_id: str, stage: str, progress: int, **fields) -> None:
        payload = {"stage": stage, "progress": max(0, min(100, int(progress)))}
        payload.update(fields)
        self.store.update(job_id, **payload)

    def snapshot(self) -> dict:
        """A machine-readable view of the queue, for the web UI."""
        counts = self.store.counts()
        current = self.store.get(self._current) if self._current else None
        return {
            "counts": counts,
            "current": current.as_dict() if current else None,
            "queued": [j.as_dict() for j in self.store.queued_jobs()],
            "average_job_seconds": self._average_job_seconds(),
            "uptime_seconds": time.time() - self._started_at,
        }
