"""Persistent job store (SQLite).

Jobs are small records; the heavy artifacts stay on disk under ``data/jobs``.
SQLite gives us durability (a restart does not lose the queue) and cheap
concurrent reads while one worker writes.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
EXPIRED = "expired"

TERMINAL = {DONE, FAILED, EXPIRED}

#: A YouTube video id is 11 characters of URL-safe base64.
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def canonical_video_id(url: str) -> str | None:
    """Extract the video id from any YouTube link shape.

    ``https://youtu.be/ID``, ``https://www.youtube.com/watch?v=ID``,
    ``https://www.youtube.com/shorts/ID``, ``.../embed/ID`` and
    ``.../live/ID`` all yield the same id, which is what we use to recognise
    a submission we have already processed.
    """
    if not url:
        return None
    parsed = urlparse(url if "://" in url else "https://" + url)
    host = (parsed.hostname or "").lower()
    if host in {"youtu.be", "www.youtu.be"}:
        candidate = parsed.path.strip("/").split("/")[0]
    elif host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        query = parse_qs(parsed.query or "")
        if "v" in query and query["v"]:
            candidate = query["v"][0]
        else:
            parts = [p for p in parsed.path.split("/") if p]
            if parts and parts[0] in {"shorts", "embed", "live", "v"} and len(parts) > 1:
                candidate = parts[1]
            else:
                candidate = ""
    else:
        return None
    return candidate if VIDEO_ID_RE.match(candidate or "") else None


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    url          TEXT NOT NULL,
    video_id     TEXT,
    title        TEXT,
    status       TEXT NOT NULL,
    stage        TEXT,
    progress     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    artifacts    TEXT,
    position     INTEGER
);
CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS jobs_video_id ON jobs(video_id);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    url: str
    status: str
    video_id: str | None = None
    title: str | None = None
    stage: str | None = None
    progress: int = 0
    error: str | None = None
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    artifacts: dict = field(default_factory=dict)
    position: int | None = None

    @property
    def queued(self) -> bool:
        return self.status == QUEUED

    @property
    def finished(self) -> bool:
        return self.status in TERMINAL

    def public_dict(self) -> dict:
        """A payload that is safe to hand to an anonymous visitor.

        Absolute server paths stay behind; only the archive size is exposed,
        because that is all the UI ever shows.
        """
        payload = self.as_dict()
        size = self.artifacts.get("size")
        payload["artifacts"] = {"size": size} if size else {}
        return payload

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "video_id": self.video_id,
            "title": self.title,
            "status": self.status,
            "stage": self.stage,
            "progress": self.progress,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "artifacts": self.artifacts,
            "position": self.position,
        }


class JobStore:
    """Thread-safe SQLite-backed store for jobs."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add columns that newer versions of ScoreForge introduced."""
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        if "video_id" not in columns:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN video_id TEXT")

    # -- helpers ---------------------------------------------------------- #
    def _row_to_job(self, row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            url=row["url"],
            status=row["status"],
            video_id=row["video_id"] if "video_id" in row.keys() else None,
            title=row["title"],
            stage=row["stage"],
            progress=row["progress"],
            error=row["error"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            artifacts=json.loads(row["artifacts"] or "{}"),
            position=row["position"],
        )

    # -- writes ----------------------------------------------------------- #
    def create(self, url: str, *, video_id: str | None = None) -> Job:
        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id, url, video_id, status, progress, created_at) "
                "VALUES (?, ?, ?, ?, 0, ?)",
                (job_id, url, video_id, QUEUED, _now()),
            )
            self._conn.commit()
        return self.get(job_id)  # type: ignore[return-value]

    def find_by_video_id(self, video_id: str) -> Job | None:
        """The most recent job for a video, whatever its state."""
        if not video_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE video_id = ? ORDER BY rowid DESC LIMIT 1",
                (video_id,),
            ).fetchone()
        return self._row_to_job(row) if row else None

    def update(self, job_id: str, **fields) -> None:
        if not fields:
            return
        allowed = {
            "title", "status", "stage", "progress", "error", "started_at",
            "finished_at", "artifacts", "position", "video_id",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"cannot update unknown fields: {sorted(unknown)}")
        if "artifacts" in fields and not isinstance(fields["artifacts"], str):
            fields["artifacts"] = json.dumps(fields["artifacts"])
        assignments = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [job_id]
        with self._lock:
            self._conn.execute(
                f"UPDATE jobs SET {assignments} WHERE id = ?", values
            )
            self._conn.commit()

    def delete(self, job_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            self._conn.commit()

    # -- reads ------------------------------------------------------------ #
    def get(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._row_to_job(row) if row else None

    def list_jobs(self, *, limit: int = 100) -> list[Job]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def queued_jobs(self) -> list[Job]:
        """Jobs waiting to run, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY created_at ASC",
                (QUEUED,),
            ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def next_queued(self) -> Job | None:
        jobs = self.queued_jobs()
        return jobs[0] if jobs else None

    def requeue_stale(self) -> int:
        """Return jobs that were running when we stopped back to the queue."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET status = ?, started_at = NULL, position = NULL "
                "WHERE status = ?",
                (QUEUED, RUNNING),
            )
            self._conn.commit()
            return cur.rowcount

    def queue_position(self, job_id: str) -> int | None:
        """1-based position in the waiting line, or ``None`` if not waiting.

        The job currently being processed counts as position 1, so a user
        always sees how far away they are.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM jobs WHERE status IN (?, ?) "
                "ORDER BY CASE status WHEN ? THEN 0 ELSE 1 END, rowid ASC",
                (QUEUED, RUNNING, RUNNING),
            ).fetchall()
        for index, row in enumerate(rows, start=1):
            if row["id"] == job_id:
                return index
        return None

    def counts(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
            ).fetchall()
        result = {QUEUED: 0, RUNNING: 0, DONE: 0, FAILED: 0, EXPIRED: 0}
        for row in rows:
            result[row["status"]] = row["n"]
        result["total"] = sum(
            result[s] for s in (QUEUED, RUNNING, DONE, FAILED, EXPIRED)
        )
        return result

    def close(self) -> None:
        with self._lock:
            self._conn.close()
