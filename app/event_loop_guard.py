"""Catch blocking I/O on the event loop, everywhere, instead of one route at a time.

The app is served by one event loop. A database query or an object-store
fetch made from an `async def` handler without `asyncio.to_thread` stalls
every other request while it waits; B1 and the print pages (AUDIT §10 A5)
were each that bug, found by reading. This module finds the rest by running.

`check_blocking` is called by the two places that do blocking I/O: the
SQLAlchemy engine (before every statement) and the blob store (before every
upload or download). When it is called on a thread that is running an event
loop, the caller forgot to leave the loop:

* in the test suite (`set_strict(True)`, from `tests/conftest.py`) it raises,
  so a new handler that blocks fails its own test;
* in production it logs one warning per call site and carries on, because a
  slow page is better than a broken one.

Boot-time work that runs on the loop before any request is served (the
migration, the seed) is wrapped in `allow_blocking()`.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

__all__ = [
    "BlockingOnEventLoop",
    "allow_blocking",
    "check_blocking",
    "set_strict",
]

logger = logging.getLogger(__name__)

_strict = False
_allowed: ContextVar[bool] = ContextVar("blocking_allowed", default=False)
_warned: set[str] = set()


class BlockingOnEventLoop(RuntimeError):
    """Blocking I/O was attempted on the event loop's own thread."""


def set_strict(strict: bool) -> None:
    """Raise (tests) instead of logging (production) on a blocking call."""
    global _strict
    _strict = strict


@contextmanager
def allow_blocking() -> Iterator[None]:
    """Permit blocking calls on the loop for the duration (boot work only)."""
    token = _allowed.set(True)
    try:
        yield
    finally:
        _allowed.reset(token)


def check_blocking(what: str) -> None:
    """Complain if `what` is about to block a running event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return  # a worker thread, a script, or a sync test: nothing to block
    if _allowed.get():
        return
    site = _call_site()
    if _strict:
        raise BlockingOnEventLoop(
            f"{what} on the event loop at {site}: wrap it in asyncio.to_thread "
            f"or make the handler a plain `def`."
        )
    if site not in _warned:
        _warned.add(site)
        logger.warning("event_loop.blocking what=%s site=%s", what, site)


def _call_site() -> str:
    """The chain of this app's own frames that led here, outermost first."""
    sites = []
    for frame in traceback.extract_stack()[:-2]:
        name = frame.filename.replace("\\", "/")
        if "/app/" not in name or name.endswith(
            ("event_loop_guard.py", "/app/db.py", "/app/blobs.py")
        ):
            continue
        sites.append(f"{name.rsplit('/app/', 1)[-1]}:{frame.lineno}")
    return " > ".join(sites[-6:]) or "unknown"
