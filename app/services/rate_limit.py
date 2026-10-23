"""Small per-process admission guard for billable generation endpoints."""

from __future__ import annotations

from collections import deque
from threading import Lock
from time import monotonic

__all__ = ["GenerationRateLimiter"]


class GenerationRateLimiter:
    """Allow a bounded number of generation requests per client per minute.

    This is deliberately an abuse brake, not authentication. It keeps an
    accidentally public Render URL from turning one API key into an unlimited
    bill. A multi-worker deployment should move the same policy to a shared
    store or reverse proxy.
    """

    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = {}
        self._lock = Lock()

    def allow(self, client_key: str) -> bool:
        now = monotonic()
        with self._lock:
            events = self._events.setdefault(client_key, deque())
            cutoff = now - self.window_seconds
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.limit:
                return False
            events.append(now)
            return True
