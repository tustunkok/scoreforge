"""The seam: "the server cannot be filled up, and paths stay server-side".

Covers the result TTL sweeper, the disk/job-count admission gate, and the
public payloads that keep absolute paths out of anonymous responses.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scoreforge import Settings, StorageFullError
from scoreforge.errors import ScoreForgeError
from scoreforge.jobs import Job
from scoreforge.service import SWEEP_INTERVAL_SECONDS, ScoreForgeService

VALID = "https://youtu.be/ZOaFiTStPt4"


@pytest.fixture
def service(tmp_path):
    svc = ScoreForgeService(Settings(data_dir=tmp_path / "data"))
    yield svc
    svc.queue.stop()


def _make_job(svc, *, age_days: float = 3, title: str = "A test video") -> Job:
    """Insert a finished job with artifacts and an arbitrary age."""
    job = svc.store.create(VALID, video_id="ZOaFiTStPt4")
    svc.job_dir(job.id).mkdir(parents=True, exist_ok=True)
    archive = svc.job_dir(job.id) / "scoreforge.zip"
    archive.write_bytes(b"zip")
    svc.store.update(
        job.id,
        title=title,
        status="done",
        stage="done",
        progress=100,
        finished_at=(datetime.now(UTC) - timedelta(days=age_days)).isoformat(),
        artifacts={"archive": str(archive), "size": 4096},
    )
    return svc.store.get(job.id)


# -- public payloads ------------------------------------------------------- #
def test_public_payloads_hide_server_paths(service):
    job = _make_job(service)

    public = job.public_dict()

    assert public["artifacts"] == {"size": 4096}
    assert "scoreforge.zip" not in repr(public)
    # The admin/CLI view keeps the full record.
    assert job.as_dict()["artifacts"]["archive"].endswith("scoreforge.zip")


def test_status_endpoint_payload_has_no_absolute_paths(service):
    job = _make_job(service)

    payload = service.status(job.id)

    assert "artifacts" in payload
    assert str(service.settings.data_dir) not in repr(payload)


def test_queue_snapshot_hides_server_paths(service):
    _make_job(service)

    snapshot = service.queue_snapshot()

    assert str(service.settings.data_dir) not in repr(snapshot)


# -- expiry ---------------------------------------------------------------- #
def test_expiring_an_old_job_deletes_its_artifacts(service):
    job = _make_job(service)

    removed = service.expire_old_jobs()

    assert removed == 1
    assert not service.job_dir(job.id).exists()
    assert service.store.get(job.id).status == "expired"


def test_a_fresh_job_is_kept(service):
    job = _make_job(service, age_days=0, title="fresh")

    assert service.expire_old_jobs() == 0
    assert service.store.get(job.id).status == "done"


def test_the_sweeper_runs_on_its_own(monkeypatch, tmp_path):
    svc = ScoreForgeService(
        Settings(data_dir=tmp_path / "data", sweep_interval_seconds=0.05)
    )
    calls: list[int] = []
    monkeypatch.setattr(
        svc, "expire_old_jobs", lambda **kwargs: calls.append(1) or 0
    )

    svc.start()
    try:
        import time

        deadline = time.monotonic() + 5
        while not calls and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        svc.stop()

    assert calls, "the sweeper never ran"


def test_the_default_sweep_interval_is_generous():
    assert SWEEP_INTERVAL_SECONDS >= 60


# -- admission ------------------------------------------------------------- #
def test_a_full_disk_refuses_new_work(service, monkeypatch):
    monkeypatch.setattr(
        service.settings, "min_free_bytes", 1 << 62, raising=False
    )

    ok, reason = service.can_accept()

    assert ok is False
    assert "disk" in reason


def test_too_many_jobs_on_disk_refuses_new_work(service, monkeypatch):
    _make_job(service)
    monkeypatch.setattr(service.settings, "max_jobs_on_disk", 1, raising=False)

    ok, reason = service.can_accept()

    assert ok is False
    assert "scores" in reason


def test_submit_raises_a_typed_error_when_storage_is_full(service, monkeypatch):
    monkeypatch.setattr(
        service.settings, "min_free_bytes", 1 << 62, raising=False
    )

    with pytest.raises(StorageFullError) as caught:
        service.submit(VALID)

    assert isinstance(caught.value, ScoreForgeError)
    assert caught.value.code == "storage_full"
    # Nothing was queued.
    assert service.queue_snapshot()["counts"]["queued"] == 0


# -- web wiring ------------------------------------------------------------ #
def test_the_web_app_answers_503_when_storage_is_full(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from scoreforge.web import app as web_app
    from scoreforge.web.app import create_app, reset_service
    from scoreforge.web.security import SlidingWindowLimiter

    svc = ScoreForgeService(Settings(data_dir=tmp_path / "data"))
    monkeypatch.setattr(
        web_app, "limiter", SlidingWindowLimiter(limit=10, window_seconds=60)
    )
    monkeypatch.setattr(svc, "can_accept", lambda: (False, "No room."))

    with TestClient(create_app(svc)) as client:
        response = client.post(
            "/submit", data={"url": VALID}, follow_redirects=False
        )

    svc.stop()
    reset_service()
    assert response.status_code == 503
    assert "No room." in response.text


def test_jobs_dir_count_ignores_plain_files(service):
    jobs = Path(service.settings.jobs_dir)
    jobs.mkdir(parents=True, exist_ok=True)
    (jobs / "stray.txt").write_text("x")
    (jobs / "a-dir").mkdir()

    assert service._artifact_job_count() == 1
