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
        # The last time aged-out clients were swept, so the map does not grow
        # with every address ever seen.
        self._last_sweep = 0.0

    @property
    def tracked_clients(self) -> int:
        """How many client keys are currently remembered (for tests and logs)."""
        with self._lock:
            return len(self._events)

    def allow(self, client_key: str, tokens: int = 1) -> bool:
        """Consume `tokens` if the client still has room; else allow nothing.

        A request is all-or-nothing: a book of 3 pages never sneaks in with
        2 tokens of room left — it waits for a fresh window.
        """
        if tokens < 1:
            raise ValueError("tokens must be at least 1")
        now = monotonic()
        with self._lock:
            cutoff = now - self.window_seconds
            # Sweep before touching this client: a client whose window has just
            # aged out must not be dropped after its new charge is appended.
            self._sweep(now, cutoff, current=client_key)
            events = self._events.setdefault(client_key, deque())
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) + tokens > self.limit:
                return False
            for _ in range(tokens):
                events.append(now)
            return True

    def _sweep(self, now: float, cutoff: float, *, current: str) -> None:
        """Forget clients whose whole window has aged out.

        Keys come from a request address, which an anonymous caller controls
        (not least through `X-Forwarded-For`), so the map must not accumulate
        one deque per address ever seen. Sweeping at most once per window keeps
        this O(clients) occasionally and O(1) on the ordinary call.
        """
        if now - self._last_sweep < self.window_seconds:
            return
        self._last_sweep = now
        stale = [
            key
            for key, stamps in self._events.items()
            if key != current and (not stamps or stamps[-1] <= cutoff)
        ]
        for key in stale:
            del self._events[key]
