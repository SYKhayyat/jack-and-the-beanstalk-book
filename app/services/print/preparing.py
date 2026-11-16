"""Build a book's print files in the background, after the owner approves it.

The print interior and cover take most of a minute on the free plan's tenth
of a CPU. That used to happen inside the approve request, so the owner waited
on a spinning button and a proxy could time the request out. Now approving
starts the build and the print page shows "making the print files" until the
approval is stored (or until it says why it could not be made).

What is kept here lives only as long as the process, like the build itself:
a restart mid-build loses both, the page shows the approve button again, and
pressing it starts over. Nothing is charged before an approval exists.

Only one build runs at a time in the process. Each one holds a few hundred
megabytes at its peak, and two together would not fit the plan's 512 MiB.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass

from app.services.memory import release_memory
from app.services.print.base import PrintError

__all__ = ["Preparation", "PrintPreparer"]

logger = logging.getLogger(__name__)

#: Shown when a build fails for a reason that is ours, not the book's.
_UNEXPECTED = "The print files could not be made just now. Please try again."


@dataclass(frozen=True)
class Preparation:
    """A build that is running, or the last one that failed, for one book."""

    #: The web PDF the owner approved: a state for another version is stale.
    pdf_sha256: str
    product_key: str
    started_at: float
    #: Empty while the build runs; the reason once it has failed.
    error: str = ""

    @property
    def running(self) -> bool:
        return not self.error


class PrintPreparer:
    """One background print build per book, one at a time per process."""

    def __init__(self) -> None:
        self._states: dict[str, Preparation] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # Created on first use, so it binds to the loop that runs the builds.
        self._one_at_a_time: asyncio.Semaphore | None = None

    def state(self, book_id: str, pdf_sha256: str) -> Preparation | None:
        """The build for this version of the book, running or failed; else None."""
        found = self._states.get(book_id)
        if found is None or found.pdf_sha256 != pdf_sha256:
            return None
        return found

    def start(
        self,
        book_id: str,
        *,
        pdf: bytes,
        product_key: str,
        build: Callable[[], object],
    ) -> bool:
        """Start building in the background; False if one is already running.

        `build` runs in a worker thread and stores the approval itself
        (`PrintService.approve`). A `PrintError` or `ValueError` it raises is
        the reason shown to the owner; anything else is logged and shown as a
        generic failure.
        """
        running = self._tasks.get(book_id)
        if running is not None and not running.done():
            return False
        self._states[book_id] = Preparation(
            pdf_sha256=hashlib.sha256(pdf).hexdigest(),
            product_key=product_key,
            started_at=time.time(),
        )
        task = asyncio.create_task(self._run(book_id, build))
        self._tasks[book_id] = task
        return True

    async def _run(self, book_id: str, build: Callable[[], object]) -> None:
        if self._one_at_a_time is None:
            self._one_at_a_time = asyncio.Semaphore(1)
        started = time.monotonic()
        error = ""
        try:
            async with self._one_at_a_time:
                await asyncio.to_thread(build)
        except asyncio.CancelledError:
            self._states.pop(book_id, None)
            raise
        except (PrintError, ValueError) as exc:
            error = str(exc) or _UNEXPECTED
            logger.info("print.prepare_refused book=%s reason=%s", book_id, error)
        except Exception:
            error = _UNEXPECTED
            logger.exception("print.prepare_failed book=%s", book_id)
        finally:
            self._tasks.pop(book_id, None)
        state = self._states.get(book_id)
        if error and state is not None:
            self._states[book_id] = Preparation(
                pdf_sha256=state.pdf_sha256,
                product_key=state.product_key,
                started_at=state.started_at,
                error=error,
            )
        else:
            # Done: the stored approval is the page's state from now on.
            self._states.pop(book_id, None)
            logger.info(
                "print.prepared book=%s seconds=%.1f", book_id, time.monotonic() - started
            )
        # The interior and cover were the largest things this process held.
        await asyncio.to_thread(release_memory)

    async def close(self) -> None:
        """Stop the builds still running, for a shutdown."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
