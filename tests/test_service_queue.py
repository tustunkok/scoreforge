"""The seam: "a user submits a link and watches a queue form".

Exercises the public service API - submit, status, queue position, ETA and
expiry - with the transcription itself stubbed out (no models, no network).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scoreforge import Settings
from scoreforge.service import ScoreForgeService


class FakePipeline:
    """Stands in for the real transcription pipeline."""

    def __init__(self) -> None:
        self.calls = 0
        self.release = None

    def __call__(self, queue, job):
        self.calls += 1
        queue.report(job.id, "transcribe", 40)
        if self.release is not None:
            self.release.wait(timeout=20)
        queue.report(job.id, "engrave", 80)
        queue.report(job.id, "done", 100)


@pytest.fixture
def service(tmp_path):
    svc = ScoreForgeService(Settings(data_dir=tmp_path / "data"))
    svc.queue.set_handler(FakePipeline())
    svc.start()
    yield svc
    svc.stop()


def test_submitting_a_link_creates_a_queued_job(service):
    job = service.submit("https://youtu.be/dQw4w9WgXcQ")

    assert job.status == "queued"
    assert service.get(job.id) is not None


def test_submitting_a_non_youtube_link_is_refused(service):
    from scoreforge import InvalidSourceError

    with pytest.raises(InvalidSourceError):
        service.submit("https://example.com/video")


def test_queue_reports_the_position_of_each_waiting_job(service):
    import threading

    # Hold the worker so the queue actually builds up.
    service.queue._handler.release = threading.Event()

    first = service.submit("https://youtu.be/aaaaaaaaaaa")
    second = service.submit("https://youtu.be/bbbbbbbbbbb")
    third = service.submit("https://youtu.be/ccccccccccc")

    assert service.status(first.id)["queue_position"] == 1
    assert service.status(second.id)["queue_position"] == 2
    assert service.status(third.id)["queue_position"] == 3

    service.queue._handler.release.set()


def test_eta_is_estimated_for_waiting_jobs(service):
    job = service.submit("https://youtu.be/aaaaaaaaaaa")

    status = service.status(job.id)
    assert status["eta"]["seconds"] is not None
    assert status["eta"]["seconds"] > 0


def test_jobs_run_one_after_another(service):
    for vid in ("aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"):
        service.submit(f"https://youtu.be/{vid}")

    deadline = datetime.now(UTC) + timedelta(seconds=20)
    while datetime.now(UTC) < deadline:
        counts = service.queue_snapshot()["counts"]
        if counts["done"] == 3:
            break
        import time

        time.sleep(0.05)

    counts = service.queue_snapshot()["counts"]
    assert counts["done"] == 3
    assert counts["queued"] == 0
    assert counts["running"] == 0


def test_finished_jobs_report_an_eta_of_zero(service):
    job = service.submit("https://youtu.be/aaaaaaaaaaa")
    _wait_for(service, job.id, "done")

    assert service.status(job.id)["eta"]["seconds"] == 0.0


def test_artifacts_expire_after_the_ttl(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", result_ttl_hours=24)
    svc = ScoreForgeService(settings)
    svc.queue.set_handler(FakePipeline())
    svc.start()
    try:
        job = svc.submit("https://youtu.be/aaaaaaaaaaa")
        _wait_for(svc, job.id, "done")

        # Pretend the job finished a day and a half ago.
        old = (datetime.now(UTC) - timedelta(hours=36)).isoformat(timespec="seconds")
        svc.store.update(job.id, finished_at=old)

        removed = svc.expire_old_jobs()
        assert removed == 1
        assert svc.get(job.id).status == "expired"
    finally:
        svc.stop()


def _wait_for(service, job_id, status, timeout=20):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.get(job_id)
        if job and job.status == status:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never reached {status}")


def test_the_same_link_twice_reuses_the_first_job(service):
    """Idempotency: the same video must not be transcribed twice."""
    first = service.submit("https://youtu.be/dQw4w9WgXcQ")
    second = service.submit("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    assert second.id == first.id


def test_a_different_video_gets_its_own_job(service):
    first = service.submit("https://youtu.be/dQw4w9WgXcQ")
    second = service.submit("https://youtu.be/oHg5SJYRHA0")

    assert second.id != first.id


def test_an_expired_job_does_not_block_a_new_submission(service):
    import threading

    service.queue._handler.release = threading.Event()
    first = service.submit("https://youtu.be/dQw4w9WgXcQ")
    service.queue._handler.release.set()
    _wait_for(service, first.id, "done")

    service.store.update(first.id, status="expired")

    second = service.submit("https://youtu.be/dQw4w9WgXcQ")
    assert second.id != first.id
