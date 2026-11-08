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

import logging
import os
import threading
import time
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..errors import InvalidSourceError, ScoreForgeError
from ..service import ScoreForgeService, Settings

log = logging.getLogger("scoreforge.web")

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
# rate limiting
# --------------------------------------------------------------------------- #
class SlidingWindowLimiter:
    """In-memory sliding-window rate limiter, per client address."""

    def __init__(self, limit: int = 5, window_seconds: float = 3600.0) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            if len(hits) >= self.limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True

    def remaining(self, key: str) -> int:
        now = time.time()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            return max(0, self.limit - len(hits))


limiter = SlidingWindowLimiter(
    limit=int(os.environ.get("SCOREFORGE_RATE_LIMIT", "5")),
    window_seconds=float(os.environ.get("SCOREFORGE_RATE_WINDOW", "3600")),
)


# --------------------------------------------------------------------------- #
# app
# --------------------------------------------------------------------------- #
def create_app(service: ScoreForgeService | None = None) -> FastAPI:
    app = FastAPI(
        title="ScoreForge",
        description="Turn a piano recording into engraved sheet music.",
        version="0.1.0",
    )
    if service is not None:
        global _service
        _service = service
        _service.start()

    static_dir = HERE / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.on_event("shutdown")
    def _shutdown() -> None:
        svc = _service
        if svc is not None:
            svc.stop()

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

    @app.post("/submit")
    def submit(
        request: Request,
        url: Annotated[str, Form()],
        svc: Annotated[ScoreForgeService, Depends(get_service)],
    ):
        client = request.client.host if request.client else "unknown"
        if not limiter.allow(client):
            return templates.TemplateResponse(
                request,
                "index.html",
                {
                    "queue": svc.queue_snapshot(),
                    "jobs": svc.list_jobs(limit=20),
                    "error": (
                        "Too many submissions from this address. "
                        "Please try again later."
                    ),
                },
                status_code=429,
            )
        try:
            job = svc.submit(url)
        except InvalidSourceError as exc:
            return templates.TemplateResponse(
                request,
                "index.html",
                {
                    "queue": svc.queue_snapshot(),
                    "jobs": svc.list_jobs(limit=20),
                    "error": exc.message,
                },
                status_code=400,
            )
        except ScoreForgeError as exc:
            return templates.TemplateResponse(
                request,
                "index.html",
                {
                    "queue": svc.queue_snapshot(),
                    "jobs": svc.list_jobs(limit=20),
                    "error": str(exc),
                },
                status_code=400,
            )
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
