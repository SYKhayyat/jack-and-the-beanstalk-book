"""Hand memory a finished piece of work used back to the operating system.

The free plan has 512 MiB. A book render or a print file leaves the process
far larger than it rests at: a PDF's font tables are some hundred thousand
small objects held in reference cycles, so they outlive the PDF until a full
collection, and glibc keeps the heap pages they sat on even once they are
freed. The instance measured 340 MiB resident after drawing a book, and the
print order that followed (another 250 MiB at its peak) was killed for memory
(2026-11-16 00:42Z).

`release_memory` is called after such work has finished and its result has
been published, never on a path somebody is waiting on: a full collection of
a large heap is a few hundred milliseconds on a tenth of a CPU.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import gc
import logging
from functools import cache

__all__ = ["release_memory"]

logger = logging.getLogger(__name__)


def release_memory() -> None:
    """Collect cyclic garbage, then return free heap pages to the OS.

    Never raises: on a C library without `malloc_trim` (musl, macOS) only the
    collection runs.
    """
    gc.collect()
    trim = _malloc_trim()
    if trim is None:
        return
    try:
        trim(0)
    except OSError:
        logger.debug("memory.trim_failed", exc_info=True)


@cache
def _malloc_trim():  # type: ignore[no-untyped-def]
    """glibc's `malloc_trim`, or None where the C library has none."""
    name = ctypes.util.find_library("c")
    if name is None:
        return None
    try:
        return ctypes.CDLL(name).malloc_trim
    except (OSError, AttributeError):
        return None
