"""How the process is doing: its memory, and whether its event loop keeps up.

Run 2 on the live site (2026-11-11) lost a book to a restart nobody could
explain: health checks took twenty seconds, then the process was gone, and the
logs said nothing about why. These two numbers are what the next log needs to
answer that without guessing:

- **RSS**, the resident memory, against the free plan's 512 MB. Written into
  the book's own log lines (`book.page`, `book.complete`) and the lag line.
- **Loop lag**: how late a timer that should fire every few seconds fires. A
  late timer means something held the event loop — sync work inside a
  coroutine, or a CPU so starved that the loop thread could not run. Every
  request waits behind it, the health check included. `watch_loop` logs it
  only when it is over the threshold, so a healthy server writes nothing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import resource
import sys
from pathlib import Path
from time import monotonic

__all__ = ["LOOP_LAG_INTERVAL_SECONDS", "LOOP_LAG_THRESHOLD_SECONDS", "rss_mb", "watch_loop"]

logger = logging.getLogger(__name__)

#: How often the loop is checked. Cheap: one timer, one subtraction.
LOOP_LAG_INTERVAL_SECONDS = 5.0
#: How late a check must be before it is logged. A quarter-second stall is
#: already a slow page for every visitor at that moment.
LOOP_LAG_THRESHOLD_SECONDS = 0.25

_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def rss_mb() -> float:
    """This process's resident memory now, in MB.

    Read from `/proc/self/statm` on Linux (Render's runtime), which is the
    current figure. Elsewhere the peak from `getrusage` stands in: it is the
    number that matters against a memory limit, and the only one portable.
    """
    try:
        statm = Path("/proc/self/statm").read_text(encoding="ascii")
        return int(statm.split()[1]) * _PAGE_SIZE / 1_048_576
    except (OSError, IndexError, ValueError):
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Kilobytes on Linux, bytes on macOS.
        return peak / (1_048_576 if sys.platform == "darwin" else 1024)


async def watch_loop(
    *,
    interval: float = LOOP_LAG_INTERVAL_SECONDS,
    threshold: float = LOOP_LAG_THRESHOLD_SECONDS,
) -> None:
    """Log `loop.lag` whenever the event loop falls behind; runs until cancelled.

    The measure is the plainest one: sleep for `interval` and see how much
    longer it took. Anything that blocked the loop in between shows up as the
    difference. The task count says how much was in flight at the time.
    """
    while True:
        started = monotonic()
        await asyncio.sleep(interval)
        lag = monotonic() - started - interval
        if lag > threshold:
            logger.warning(
                "loop.lag ms=%.0f rss_mb=%.0f tasks=%d",
                lag * 1000,
                rss_mb(),
                len(asyncio.all_tasks()),
            )
