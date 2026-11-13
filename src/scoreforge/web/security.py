"""Defensive plumbing for the only public input path: ``POST /submit``.

The service is published without authentication, so the HTTP surface is
guarded here rather than in the route handlers:

* :class:`BodySizeLimit` - reject oversized request bodies before they are
  buffered in memory.
* :class:`CrossOriginGuard` - refuse cross-site form posts (a cheap CSRF
  defence: the app has no cookies, so a token would add nothing).
* :data:`SECURITY_HEADERS` - a restrictive CSP plus the usual hardening
  headers, applied to every response.
* :class:`SlidingWindowLimiter` - per-client submission budget that evicts
  expired buckets instead of growing without bound.
"""

from __future__ import annotations

import threading
import time

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: A YouTube URL is at most ~60 bytes; anything near a kilobyte is abuse.
MAX_BODY_BYTES = 2048

#: Methods that may carry a submission.
STATE_CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: A browser may post a form from our own pages, and nothing else.
SAME_SITE_VALUES = frozenset({"same-origin", "none"})

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self' 'unsafe-inline'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    # NOT "no-referrer": per the Fetch spec a no-referrer request serialises
    # its Origin as the literal string "null", which made the browser's own
    # form post look cross-site to CrossOriginGuard. This policy still strips
    # the path and query from cross-origin referrers.
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def to_bool(value: str | None, *, default: bool = False) -> bool:
    """Read a boolean environment flag."""
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def csv_list(value: str | None) -> list[str]:
    """Read a comma-separated environment list."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


class _BodyTooLarge(Exception):
    """Internal signal: the client streamed more than the allowed body."""


async def _noop_receive() -> Message:  # pragma: no cover - trivial
    return {"type": "http.disconnect"}


class BodySizeLimit:
    """ASGI middleware that refuses bodies larger than ``max_bytes``.

    ``Content-Length`` catches the common case before a single byte is read;
    wrapping ``receive`` also stops chunked uploads that lie about (or omit)
    their length.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                too_large = int(declared) > self.max_bytes
            except ValueError:
                too_large = True
            if too_large:
                await self._too_large(send)
                return

        received = 0
        exceeded = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # Stop the stream and let the app unwind; its own error
                    # response is discarded below in favour of a 413.
                    exceeded = True
                    return {"type": "http.disconnect"}
            return message

        async def watched_send(message: Message) -> None:
            if not exceeded:
                await send(message)

        try:
            await self.app(scope, limited_receive, watched_send)
        except _BodyTooLarge:  # pragma: no cover - defensive
            exceeded = True

        if exceeded:
            await self._too_large(send)

    @staticmethod
    async def _too_large(send: Send) -> None:
        response = PlainTextResponse("Request body too large.", status_code=413)
        await response({"type": "http"}, _noop_receive, send)


class CrossOriginGuard:
    """Reject state-changing requests that a browser labels cross-site.

    ``Sec-Fetch-Site`` is authoritative where present; ``Origin`` is the
    fallback for older browsers. Non-browser clients send neither and pass
    through - they are the rate limiter's problem.
    """

    def __init__(
        self, app: ASGIApp, *, extra_origins: frozenset[str] = frozenset()
    ) -> None:
        self.app = app
        # Behind a reverse proxy the Host header is often the internal name
        # while Origin carries the public one; name the public origins
        # explicitly rather than trusting the Host header.
        self.extra_origins = frozenset(
            origin.lower().rstrip("/") for origin in extra_origins
        )

    def _origin_ok(self, origin: bytes, host: bytes) -> bool:
        if _same_origin(origin, host):
            return True
        text = origin.decode("latin-1").strip().lower().rstrip("/")
        return text in self.extra_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in STATE_CHANGING:
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        site = headers.get(b"sec-fetch-site")
        origin = headers.get(b"origin")
        if site is not None:
            if site.strip().lower() not in {
                value.encode() for value in SAME_SITE_VALUES
            }:
                await _refuse(send, "Cross-site submissions are not allowed.")
                return
        elif origin is not None and not self._origin_ok(
            origin, headers.get(b"host", b"")
        ):
            # A browser may only omit Sec-Fetch-Site if it is old; then the
            # Origin header is the sole signal and "null" is not our page.
            await _refuse(send, "Cross-site submissions are not allowed.")
            return

        await self.app(scope, receive, send)


async def _refuse(send: Send, detail: str) -> None:
    response = JSONResponse({"detail": detail}, status_code=403)
    await response({"type": "http"}, _noop_receive, send)


def _same_origin(origin: bytes, host: bytes) -> bool:
    """Compare an ``Origin`` header against the request ``Host`` header."""
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(origin.decode("latin-1"))
    except Exception:
        return False
    # A literal "null" origin (sandboxed iframe, file://) is not our page.
    return (
        bool(parsed.netloc)
        and parsed.netloc.encode("latin-1").lower() == host.lower()
    )


class SlidingWindowLimiter:
    """In-memory sliding-window limiter, per client key.

    Expired buckets are evicted on every ``allow`` so that a flood of random
    source addresses cannot grow the dictionary without bound.
    """

    def __init__(self, limit: int = 5, window_seconds: float = 3600.0) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            self._evict(now)
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

    def _evict(self, now: float) -> None:
        stale = [
            key for key, hits in self._hits.items()
            if not hits or now - max(hits) >= self.window
        ]
        for key in stale:
            del self._hits[key]


def client_address(request: Request, *, trust_forwarded: bool = False) -> str:
    """The client address to charge submissions to.

    ``X-Forwarded-For`` is only honoured when the deployment declares that a
    trusted proxy sets it; otherwise any client could pick its own bucket by
    sending the header.
    """
    if trust_forwarded:
        forwarded = request.headers.get("x-forwarded-for", "")
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"
