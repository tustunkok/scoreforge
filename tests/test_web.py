"""The seam: "a visitor submits a link and reaches their score".

HTTP-level behaviour only: form -> redirect -> status page -> download.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from scoreforge import Settings
from scoreforge.service import ScoreForgeService
from scoreforge.web.app import create_app, reset_service


@pytest.fixture
def client(tmp_path):
    svc = ScoreForgeService(Settings(data_dir=tmp_path / "data"))

    def handler(queue, job):
        queue.report(job.id, "transcribe", 50)
        queue.report(job.id, "done", 100)

    svc.queue.set_handler(handler)
    app = create_app(svc)
    with TestClient(app) as test_client:
        yield test_client
    svc.stop()
    reset_service()


def test_home_page_shows_the_form_and_the_queue(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "Transcribe" in response.text
    assert "Queue" in response.text


def test_submitting_a_link_lands_on_the_job_page(client):
    response = client.post(
        "/submit", data={"url": "https://youtu.be/dQw4w9WgXcQ"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/job/")


def test_submitting_a_bad_link_shows_an_error(client):
    response = client.post(
        "/submit", data={"url": "https://example.com/video"},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "YouTube" in response.text


def test_job_page_shows_the_progress_and_the_queue(client):
    response = client.post(
        "/submit", data={"url": "https://youtu.be/aaaaaaaaaaa"}, follow_redirects=True
    )

    assert response.status_code == 200
    # The queue panel is always visible, and the job carries a status.
    assert "waiting" in response.text
    assert 'class="status' in response.text
    # Whichever state the worker reached, the page explains it.
    assert (
        "position" in response.text
        or "being transcribed" in response.text
        or "Download the score" in response.text
        or "alert" in response.text
    )


def test_job_status_is_available_as_json(client):
    response = client.post(
        "/submit", data={"url": "https://youtu.be/aaaaaaaaaaa"}, follow_redirects=False
    )
    job_id = response.headers["location"].rsplit("/", 1)[-1]

    api = client.get(f"/api/job/{job_id}")

    assert api.status_code == 200
    payload = api.json()
    assert payload["id"] == job_id
    assert payload["status"] in {"queued", "running", "done"}


def test_unknown_job_is_a_404(client):
    assert client.get("/job/doesnotexist").status_code == 404
    assert client.get("/api/job/doesnotexist").status_code == 404


def test_queue_endpoint_reports_counts(client):
    response = client.get("/api/jobs")

    assert response.status_code == 200
    assert "counts" in response.json()


def test_healthz_is_ok(client):
    assert client.get("/healthz").json() == {"status": "ok"}
