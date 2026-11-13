"""The seam: "the public input path cannot be used to hurt the service".

Covers the defences around ``POST /submit``: body size, cross-site posts,
client keying, admission control, hardening headers and rejection logging.
"""

from __future__ import annotations

import logging
import time

import pytest
from fastapi.testclient import TestClient

from scoreforge import Settings
from scoreforge.abuse import AbuseLog
from scoreforge.service import ScoreForgeService
from scoreforge.web import app as web_app
from scoreforge.web.app import create_app, reset_service
from scoreforge.web.security import (
    MAX_BODY_BYTES,
    SECURITY_HEADERS,
    SlidingWindowLimiter,
    client_address,
)

VALID = "https://youtu.be/ZOaFiTStPt4"


@pytest.fixture
def client(tmp_path, monkeypatch):
    svc = ScoreForgeService(Settings(data_dir=tmp_path / "data"))

    def handler(queue, job):
        queue.report(job.id, "transcribe", 50)
        queue.report(job.id, "done", 100)

    svc.queue.set_handler(handler)
    # Each test starts with a fresh budget and an empty queue.
    monkeypatch.setattr(
        web_app, "limiter", SlidingWindowLimiter(limit=5, window_seconds=3600)
    )
    monkeypatch.setattr(web_app, "MAX_QUEUE_DEPTH", 20)

    app = create_app(svc)
    with TestClient(app) as test_client:
        yield test_client
    svc.stop()
    reset_service()


# -- request size ---------------------------------------------------------- #
def test_oversized_content_length_is_refused(client):
    response = client.post(
        "/submit",
        content=b"url=" + b"A" * (MAX_BODY_BYTES + 1),
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "content-length": str(MAX_BODY_BYTES + 1),
        },
    )

    assert response.status_code == 413


def test_oversized_chunked_body_is_refused(client):
    def chunks():
        for _ in range(8):
            yield b"url=" + b"A" * 1024

    response = client.post(
        "/submit",
        content=chunks(),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 413


def test_a_normal_submission_still_fits(client):
    response = client.post(
        "/submit", data={"url": VALID}, follow_redirects=False
    )

    assert response.status_code == 303


# -- cross-site posts ------------------------------------------------------ #
@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_cross_site_submissions_are_refused(client, site):
    response = client.post(
        "/submit",
        data={"url": VALID},
        headers={"sec-fetch-site": site},
        follow_redirects=False,
    )

    assert response.status_code == 403


@pytest.mark.parametrize("site", ["same-origin", "none"])
def test_same_origin_submissions_are_allowed(client, site):
    response = client.post(
        "/submit",
        data={"url": VALID},
        headers={"sec-fetch-site": site},
        follow_redirects=False,
    )

    assert response.status_code == 303


def test_a_browser_form_post_with_our_referrer_policy_is_allowed(client):
    """Regression: `Referrer-Policy: no-referrer` makes Chrome send Origin: null.

    Under that policy a same-origin form submission serialises its Origin as
    the literal string "null" (Fetch spec), so the guard refused our own form
    with "Cross-site submissions are not allowed." The policy must therefore
    not be "no-referrer", and the fetch metadata alone must be enough.
    """
    response = client.post(
        "/submit",
        data={"url": VALID},
        headers={"origin": "null", "sec-fetch-site": "same-origin"},
        follow_redirects=False,
    )

    assert response.status_code == 303


def test_the_referrer_policy_does_not_null_the_origin(client):
    response = client.get("/")

    assert response.headers["referrer-policy"] != "no-referrer"


def test_a_null_origin_without_fetch_metadata_is_refused(client):
    """A sandboxed iframe or a file:// page posts Origin: null too."""
    response = client.post(
        "/submit",
        data={"url": VALID},
        headers={"origin": "null"},
        follow_redirects=False,
    )

    assert response.status_code == 403


def test_foreign_origin_without_fetch_metadata_is_refused(client):
    response = client.post(
        "/submit",
        data={"url": VALID},
        headers={"origin": "https://evil.example"},
        follow_redirects=False,
    )

    assert response.status_code == 403


def test_a_configured_public_origin_is_accepted_behind_a_proxy(tmp_path, monkeypatch):
    """Origin carries the public URL while Host may be the internal one."""
    svc = ScoreForgeService(Settings(data_dir=tmp_path / "proxy"))
    monkeypatch.setenv("SCOREFORGE_PUBLIC_ORIGIN", "https://scores.example.com")
    monkeypatch.setattr(
        web_app, "limiter", SlidingWindowLimiter(limit=5, window_seconds=3600)
    )
    proxy_client = TestClient(
        create_app(svc), base_url="http://internal:8000"
    )
    svc.start = lambda: None
    response = proxy_client.post(
        "/submit",
        data={"url": VALID},
        headers={"origin": "https://scores.example.com"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    svc.stop()
    reset_service()


def test_own_origin_without_fetch_metadata_is_allowed(client):
    response = client.post(
        "/submit",
        data={"url": VALID},
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )

    assert response.status_code == 303


# -- admission control ----------------------------------------------------- #
def test_a_full_queue_is_refused(client, monkeypatch):
    monkeypatch.setattr(web_app, "MAX_QUEUE_DEPTH", 0)

    response = client.post("/submit", data={"url": VALID}, follow_redirects=False)

    assert response.status_code == 503
    assert "queue is full" in response.text.lower()


def test_repeated_submissions_are_rate_limited(client, monkeypatch):
    monkeypatch.setattr(
        web_app, "limiter", SlidingWindowLimiter(limit=1, window_seconds=3600)
    )

    first = client.post("/submit", data={"url": VALID}, follow_redirects=False)
    second = client.post("/submit", data={"url": VALID}, follow_redirects=False)

    assert first.status_code == 303
    assert second.status_code == 429


# -- headers --------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["/", "/healthz"])
def test_hardening_headers_are_present(client, path):
    response = client.get(path)

    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


def test_refusals_are_still_hardened(client):
    """A 413/403 must not be the one response without security headers."""
    big = client.post(
        "/submit",
        content=b"url=" + b"A" * (MAX_BODY_BYTES + 1),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    cross = client.post(
        "/submit",
        data={"url": VALID},
        headers={"sec-fetch-site": "cross-site"},
        follow_redirects=False,
    )

    for response in (big, cross):
        for name, value in SECURITY_HEADERS.items():
            assert response.headers[name] == value


# -- client keying --------------------------------------------------------- #
def test_forwarded_for_is_ignored_by_default(client, monkeypatch):
    limiter = SlidingWindowLimiter(limit=1)
    monkeypatch.setattr(web_app, "limiter", limiter)

    client.post("/submit", data={"url": VALID}, follow_redirects=False)
    # A spoofed header must not buy a second bucket.
    client.post(
        "/submit", data={"url": VALID},
        headers={"x-forwarded-for": "203.0.113.7"},
        follow_redirects=False,
    )

    assert limiter.remaining("testclient") == 0


def test_forwarded_for_is_honoured_when_the_proxy_is_trusted(client, monkeypatch):
    limiter = SlidingWindowLimiter(limit=1)
    monkeypatch.setattr(web_app, "limiter", limiter)
    monkeypatch.setattr(web_app, "TRUST_PROXY", True)

    client.post(
        "/submit", data={"url": VALID},
        headers={"x-forwarded-for": "203.0.113.7"},
        follow_redirects=False,
    )

    assert limiter.remaining("203.0.113.7") == 0


def test_client_address_falls_back_to_the_socket(tmp_path):
    from starlette.requests import Request

    scope = {
        "type": "http",
        "client": ("127.0.0.1", 5000),
        "headers": [(b"x-forwarded-for", b"203.0.113.7")],
    }
    request = Request(scope)

    assert client_address(request) == "127.0.0.1"
    assert client_address(request, trust_forwarded=True) == "203.0.113.7"


# -- limiter bookkeeping --------------------------------------------------- #
def test_expired_buckets_are_evicted():
    limiter = SlidingWindowLimiter(limit=2, window_seconds=0.05)
    limiter.allow("one")
    limiter.allow("two")

    time.sleep(0.1)
    limiter.allow("three")

    assert set(limiter._hits) == {"three"}


@pytest.fixture
def abuse_records():
    """Capture ``scoreforge.abuse`` records.

    ``setup_logging`` installs its own handlers and disables propagation from
    ``scoreforge``, so the test attaches a handler below it rather than at the
    root logger.
    """
    parent = logging.getLogger("scoreforge")
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Collect()
    previous = parent.propagate
    parent.addHandler(handler)
    parent.propagate = False
    try:
        yield records
    finally:
        parent.removeHandler(handler)
        parent.propagate = previous


def test_rejections_are_aggregated_into_one_line(abuse_records):
    log = AbuseLog(window_seconds=0.05)

    log.record(client="198.51.100.4", reason="invalid-source", value="https://x")
    log.record(client="198.51.100.4", reason="invalid-source", value="https://y")

    messages = [record.getMessage() for record in abuse_records]
    assert len(messages) == 1
    assert log.pending() == 1

    time.sleep(0.1)
    log.record(client="198.51.100.4", reason="invalid-source", value="https://z")

    messages = [record.getMessage() for record in abuse_records]
    assert len(messages) == 3
    assert "suppressed 1 further rejected submission" in messages[1]
    assert "rejected submission" in messages[2] and "https://z" in messages[2]


def test_rejection_values_cannot_forge_log_lines(abuse_records):
    log = AbuseLog(window_seconds=0.0)

    log.record(client="198.51.100.5", reason="invalid-source", value="a\nb\r\nc")

    assert len(abuse_records) == 1
    assert chr(10) not in abuse_records[0].getMessage()
