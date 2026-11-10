"""FastAPI application: paste a link, wait in the queue, download the score.

Routes
------
``GET  /``                 submit form + queue overview
``POST /submit``           enqueue a job, redirect to the job page
``GET  /job/{id}``         status, queue position, ETA, preview
``GET  /download/{id}``    the zip archive
``GET  /preview/{id}``     the preview image
``GET  /api/jobs``         JSON status of the queue
``GET  /api/job/{id}``     JSON status of one job
``GET  /healthz``          liveness probe
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..abuse import rejections
from ..errors import InvalidSourceError, ScoreForgeError
from ..logging_setup import get_logger, setup_logging
from ..service import ScoreForgeService, Settings
from .security import (
    SECURITY_HEADERS,
    BodySizeLimit,
    CrossOriginGuard,
    SlidingWindowLimiter,
    client_address,
    csv_list,
    to_bool,
)

log = get_logger("web")

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

_service: ScoreForgeService | None = None


def get_service() -> ScoreForgeService:
    global _service
    if _service is None:
        _service = ScoreForgeService(Settings.from_env())
        _service.start()
    return _service


def reset_service() -> None:
    """Drop the cached service (used by tests)."""
    global _service
    if _service is not None:
        _service.stop()
    _service = None


# --------------------------------------------------------------------------- #
# rate limiting and admission control
# --------------------------------------------------------------------------- #
limiter = SlidingWindowLimiter(
    limit=int(os.environ.get("SCOREFORGE_RATE_LIMIT", "5")),
    window_seconds=float(os.environ.get("SCOREFORGE_RATE_WINDOW", "3600")),
)

#: How many submissions may wait at once. The worker is strictly serial, so a
#: deep queue would keep everyone waiting for hours; refuse instead.
MAX_QUEUE_DEPTH = int(os.environ.get("SCOREFORGE_MAX_QUEUE_DEPTH", "20"))

#: Only honour X-Forwarded-For when a trusted proxy sets it.
TRUST_PROXY = to_bool(os.environ.get("SCOREFORGE_TRUST_PROXY"))

#: Host header allow-list; empty means "accept any host" (local development).
ALLOWED_HOSTS = csv_list(os.environ.get("SCOREFORGE_ALLOWED_HOSTS"))


# --------------------------------------------------------------------------- #
# app
# --------------------------------------------------------------------------- #
def create_app(service: ScoreForgeService | None = None) -> FastAPI:
    setup_logging()
    app = FastAPI(
        title="ScoreForge",
        description="Turn a piano recording into engraved sheet music.",
        version="0.1.0",
    )
    if service is not None:
        global _service
        _service = service
        _service.start()

    # Added last-to-first by Starlette: the body limit runs before anything
    # reads the request, then the origin check, then the route handlers.
    app.add_middleware(CrossOriginGuard)
    app.add_middleware(BodySizeLimit)
    if ALLOWED_HOSTS:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

    static_dir = HERE / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.on_event("shutdown")
    def _shutdown() -> None:
        svc = _service
        if svc is not None:
            svc.stop()

    @app.middleware("http")
    async def _log_requests(request: Request, call_next):
        """Log every request with its status and duration."""
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception(
                "%s %s failed", request.method, request.url.path
            )
            raise
        duration_ms = (time.perf_counter() - started) * 1000
        log.info(
            "%s %s -> %d (%.0f ms)",
            request.method, request.url.path, response.status_code, duration_ms,
        )
        return response

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        """Apply the hardening headers to every response."""
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, svc: Annotated[ScoreForgeService, Depends(get_service)]):
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "queue": svc.queue_snapshot(),
                "jobs": svc.list_jobs(limit=20),
                "error": None,
            },
        )

    def _index_error(request: Request, svc: ScoreForgeService, message: str, status: int):
        """Re-render the submit form with a user-facing error."""
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "queue": svc.queue_snapshot(),
                "jobs": svc.list_jobs(limit=20),
                "error": message,
            },
            status_code=status,
        )

    @app.post("/submit")
    def submit(
        request: Request,
        url: Annotated[str, Form()],
        svc: Annotated[ScoreForgeService, Depends(get_service)],
    ):
        client = client_address(request, trust_forwarded=TRUST_PROXY)
        user_agent = request.headers.get("user-agent", "")

        if not limiter.allow(client):
            rejections.record(
                client=client, reason="rate-limited", value=url, user_agent=user_agent
            )
            return _index_error(
                request,
                svc,
                "Too many submissions from this address. Please try again later.",
                429,
            )

        if svc.queue_snapshot()["counts"]["queued"] >= MAX_QUEUE_DEPTH:
            rejections.record(
                client=client, reason="queue-full", value=url, user_agent=user_agent
            )
            return _index_error(
                request,
                svc,
                "The queue is full right now. Please try again in a little while.",
                503,
            )

        try:
            job = svc.submit(url)
        except InvalidSourceError as exc:
            rejections.record(
                client=client, reason="invalid-source", value=url,
                user_agent=user_agent,
            )
            return _index_error(request, svc, exc.message, 400)
        except ScoreForgeError as exc:
            rejections.record(
                client=client, reason="rejected", value=url, user_agent=user_agent
            )
            return _index_error(request, svc, str(exc), 400)
        return RedirectResponse(f"/job/{job.id}", status_code=303)

    @app.get("/job/{job_id}", response_class=HTMLResponse)
    def job_page(
        request: Request,
        job_id: str,
        svc: Annotated[ScoreForgeService, Depends(get_service)],
    ):
        status = svc.status(job_id)
        if status.get("error") == "not_found":
            raise HTTPException(status_code=404, detail="Job not found")
        return templates.TemplateResponse(
            request, "job.html", {"job": status, "queue": svc.queue_snapshot()}
        )

    @app.get("/download/{job_id}")
    def download(
        job_id: str,
        svc: Annotated[ScoreForgeService, Depends(get_service)],
    ):
        path = svc.artifact_path(job_id, "scoreforge.zip")
        if path is None:
            path = _find_archive(svc, job_id)
        if path is None:
            raise HTTPException(status_code=404, detail="No file available")
        return FileResponse(path, filename=path.name, media_type="application/zip")

    @app.get("/preview/{job_id}")
    def preview(job_id: str, svc: Annotated[ScoreForgeService, Depends(get_service)]):
        path = svc.artifact_path(job_id, "preview.jpg")
        if path is None:
            raise HTTPException(status_code=404, detail="No preview available")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/jobs")
    def api_jobs(svc: Annotated[ScoreForgeService, Depends(get_service)]):
        return JSONResponse(svc.queue_snapshot())

    @app.get("/api/job/{job_id}")
    def api_job(job_id: str, svc: Annotated[ScoreForgeService, Depends(get_service)]):
        status = svc.status(job_id)
        if status.get("error") == "not_found":
            raise HTTPException(status_code=404, detail="Job not found")
        return JSONResponse(status)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app


def _find_archive(svc: ScoreForgeService, job_id: str) -> Path | None:
    job = svc.get(job_id)
    if job is None or not job.artifacts:
        return None
    zip_path = job.artifacts.get("zip")
    if zip_path and Path(zip_path).exists():
        return Path(zip_path)
    candidates = sorted(svc.job_dir(job_id).glob("*.zip"))
    return candidates[0] if candidates else None


def main(argv: list[str] | None = None) -> int:
    """Console entry point (``scoreforge-web``)."""
    import uvicorn

    uvicorn.run(
        create_app(),
        host=os.environ.get("SCOREFORGE_HOST", "0.0.0.0"),
        port=int(os.environ.get("SCOREFORGE_PORT", "8000")),
    )
    return 0
