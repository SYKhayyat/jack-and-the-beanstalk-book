"""A blocking sliding-window pacer for an upstream images-per-minute limit.

OpenAI caps image generation at a fixed number of images per minute. The
concurrency gate bounds how many calls are in flight, not how many start per
minute, so a burst of fast pages can exceed the cap and be rejected with 429.
The pacer makes each call wait for room instead, so nothing is rejected.

Provider calls already run in worker threads, so `acquire` simply blocks.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from threading import Lock

__all__ = ["ImagePacer"]


class ImagePacer:
    """Allow at most `limit` images to start in any `window_seconds` span."""

    def __init__(
        self,
        limit: int,
        window_seconds: float = 60.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self.limit = limit
        self.window_seconds = window_seconds
        self._clock = clock
        self._sleep = sleep
        self._starts: deque[float] = deque()
        self._lock = Lock()

    def acquire(self, images: int = 1) -> float:
        """Block until `images` may start; return the seconds spent waiting."""
        if images < 1:
            raise ValueError("images must be at least 1")
        # A single call bigger than the whole window can never fit; let it
        # through alone rather than wait forever.
        images = min(images, self.limit)
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                cutoff = now - self.window_seconds
                while self._starts and self._starts[0] <= cutoff:
                    self._starts.popleft()
                if len(self._starts) + images <= self.limit:
                    self._starts.extend([now] * images)
                    return waited
                # Wait until enough of the oldest starts have aged out.
                free_at = self._starts[len(self._starts) + images - self.limit - 1]
                delay = max(free_at + self.window_seconds - now, 0.01)
            self._sleep(delay)
            waited += delay
