"""One lock per key, held only while someone wants it.

Why: a reader's browser opening a book asks for every page's screen copy at
once, and two tabs (or a retry) ask for the same one twice. Without this, each
request that misses the stored copy decodes the full-size page and encodes its
own WebP of it: the same work and the same 20-odd MB, done twice at once. With
it, the second request waits for the first and then reads the copy it saved.

Keys are dropped as soon as nobody holds or waits on them, so the table never
grows with the number of pictures served. Thread-based, like every caller (the
sync picture routes run on the threadpool).
"""

from __future__ import annotations

import threading
from collections.abc import Hashable, Iterator
from contextlib import contextmanager

__all__ = ["KeyedLocks"]


class KeyedLocks:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[Hashable, tuple[threading.Lock, int]] = {}

    @contextmanager
    def hold(self, key: Hashable) -> Iterator[None]:
        """Run the block while no other block holds the same key."""
        with self._guard:
            lock, users = self._locks.get(key, (threading.Lock(), 0))
            self._locks[key] = (lock, users + 1)
        try:
            with lock:
                yield
        finally:
            with self._guard:
                lock, users = self._locks[key]
                if users == 1:
                    del self._locks[key]
                else:
                    self._locks[key] = (lock, users - 1)

    def __len__(self) -> int:
        with self._guard:
            return len(self._locks)
