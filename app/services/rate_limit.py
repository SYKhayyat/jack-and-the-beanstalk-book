"""Small per-process admission guard for billable generation endpoints."""

from __future__ import annotations

from collections import deque
from threading import Lock
from time import monotonic

__all__ = ["GenerationRateLimiter"]


class GenerationRateLimiter:
    """Allow a bounded number of billable images per client per minute.

    This is deliberately an abuse brake, not authentication. It keeps an
    accidentally public Render URL from turning one API key into an unlimited
    bill. `tokens` counts billable images, not HTTP requests: one request may
    carry several (candidate portraits, book pages), and all of them cost the
    same per-minute allowance. A multi-worker deployment should move the same
    policy to a shared store or reverse proxy.
    """

    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = {}
        self._lock = Lock()

    def allow(self, client_key: str, tokens: int = 1) -> bool:
        """Consume `tokens` if the client still has room; else allow nothing.

        A request is all-or-nothing: a book of 3 pages never sneaks in with
        2 tokens of room left — it waits for a fresh window.
        """
        if tokens < 1:
            raise ValueError("tokens must be at least 1")
        now = monotonic()
        with self._lock:
            events = self._events.setdefault(client_key, deque())
            cutoff = now - self.window_seconds
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) + tokens > self.limit:
                return False
            for _ in range(tokens):
                events.append(now)
            return True
