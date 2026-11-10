"""One ceiling on decoding and re-encoding pictures, for the whole process.

Why: converting a full-size page (lossless WebP for the blob store, the
screen-sized WebP copies, the PNG a stored WebP is read back as) holds 20 to 35
MB of native memory for as long as it runs. A book in flight saves up to eight
pictures at once while the reader's browser asks for every page's screen copy,
and on a server with a fraction of a CPU each conversion takes many seconds,
so they overlap. Unbounded, a single book filled a 512 MB instance and the
process was killed mid-book (2026-11-10).

The gate does not slow a book down: transcoding is CPU-bound, and two at a
time already uses all the CPU the server has. It only stops the queue from
being held in memory all at once.

A plain `threading` semaphore, not an asyncio one: every caller is already on
a worker thread (store writes, sync routes), never on the event loop.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

__all__ = ["TRANSCODE_SLOTS", "transcoding"]

#: Conversions that may run at once.
TRANSCODE_SLOTS = 2

_slots = threading.BoundedSemaphore(TRANSCODE_SLOTS)


@contextmanager
def transcoding() -> Iterator[None]:
    """Hold one of the process's transcoding slots for the block.

    Never nest: a block holding a slot must not enter another.
    """
    with _slots:
        yield
