"""Aggregated logging for rejected submissions.

Every rejected input must be visible in the service log, but logging each
rejection verbatim turns the log itself into a denial-of-service target:
one client can emit thousands of lines a minute. :class:`AbuseLog` keeps a
sliding window per client (and per reason) and emits one summary line per
window instead of one line per attempt.

The emitted records carry the client address, the user agent, the reason
and a truncated, ``repr``-escaped copy of the submitted value, so control
characters cannot forge extra log lines.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .logging_setup import get_logger

log = get_logger("abuse")

#: A submitted value longer than this is truncated in the log.
MAX_LOGGED_VALUE = 200


def _clip(value: str) -> str:
    value = value or ""
    if len(value) <= MAX_LOGGED_VALUE:
        return value
    return value[:MAX_LOGGED_VALUE] + "…"


@dataclass
class _Window:
    first_seen: float
    count: int = 0
    announced: bool = False
    folded: int = 0
    reasons: set[str] = field(default_factory=set)
    last_value: str = ""
    last_user_agent: str = ""


class AbuseLog:
    """Rate-limited rejection log, keyed by client and reason."""

    def __init__(self, window_seconds: float = 60.0, max_clients: int = 1024) -> None:
        self.window = window_seconds
        self.max_clients = max_clients
        self._windows: dict[str, _Window] = {}
        self._lock = threading.Lock()

    def record(
        self,
        *,
        client: str,
        reason: str,
        value: str = "",
        user_agent: str = "",
    ) -> None:
        now = time.time()
        key = client or "unknown"
        with self._lock:
            self._evict(now)
            window = self._windows.get(key)
            if window is None:
                window = _Window(first_seen=now)
                self._windows[key] = window
            opening = now - window.first_seen >= self.window
            if opening:
                if window.count:
                    self._flush(key, window, int(self.window))
                window.first_seen = now
                window.count = 0
                window.announced = False
                window.folded = 0
                window.reasons.clear()

            window.count += 1
            window.reasons.add(reason or "invalid")
            window.last_value = _clip(value)
            window.last_user_agent = _clip(user_agent)

            if window.announced:
                # The first rejection of this window was already logged;
                # everything else is folded into the next summary.
                window.folded += 1
                return
            window.announced = True
            value_snapshot = window.last_value
            agent = window.last_user_agent

        log.warning(
            "rejected submission from %s: %r (reason=%s, user-agent=%r)",
            key,
            value_snapshot,
            reason or "invalid",
            agent,
        )

    @staticmethod
    def _flush(key: str, window: _Window, window_seconds: int) -> None:
        """Emit the summary for a window that is rolling over."""
        dropped = window.folded
        if dropped <= 0:
            return
        log.warning(
            "suppressed %d further rejected submission(s) from %s "
            "in the last %ds (reason=%s, last=%r)",
            dropped,
            key,
            window_seconds,
            "; ".join(sorted(window.reasons)),
            window.last_value,
        )

    def _evict(self, now: float) -> None:
        stale = [
            key for key, window in self._windows.items()
            if now - window.first_seen >= self.window and window.count
        ]
        for key in stale:
            window = self._windows.pop(key)
            # Do not lose the folded rejections of a window that is closing.
            self._flush(key, window, int(self.window))
        if len(self._windows) > self.max_clients:
            oldest = sorted(
                self._windows, key=lambda k: self._windows[k].first_seen
            )
            for key in oldest[: len(self._windows) - self.max_clients]:
                del self._windows[key]

    def pending(self) -> int:
        """How many rejections are buffered and not yet summarised."""
        with self._lock:
            return sum(window.folded for window in self._windows.values())


#: Process-wide rejection log.
rejections = AbuseLog()
