"""A concurrency gate for generation requests.

Image models are slow and rate-limited. Without a ceiling, a burst of traffic
becomes a burst of billable requests and 429s. With one, requests queue and the
spend is bounded.

Two details that are easy to get wrong and expensive to get wrong:

**Per-loop semaphores.** A plain `asyncio.Semaphore` binds to the first event
loop that awaits it and raises if a different loop uses it next. Tests create a
fresh loop per `TestClient`, so this gate keeps one semaphore per running loop.

**Slot lifetime outlives the HTTP response.** When a generation times out, the
client gets its 504 immediately but the worker thread is still running. If the
slot were released at that moment, the gate would believe the server is idle
while it is actually still busy — and the next request would be admitted on top
of it. So a timeout hands the slot to the worker, which releases it when it
genuinely finishes. See `lease()`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

__all__ = ["ConcurrencyGate", "GateTimeout", "Slot"]

logger = logging.getLogger(__name__)


class GateTimeout(TimeoutError):
    """A request waited too long for a generation slot and never got one.

    Distinct from a generation that started and then ran long: the client is
    told to back off, and nothing has been sent to the image model at all.
    """


class Slot:
    """A held generation slot, released exactly once."""

    def __init__(self, semaphore: asyncio.Semaphore) -> None:
        self._semaphore = semaphore
        self._released = False
        self._transferred = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._semaphore.release()

    def transfer(self) -> None:
        """Hand release responsibility to work that outlives the request."""
        self._transferred = True


class ConcurrencyGate:
    """Bounds how many generations run at once, per event loop."""

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self._limit = limit
        self._semaphores: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}

    @property
    def limit(self) -> int:
        return self._limit

    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        semaphore = self._semaphores.get(loop)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self._limit)
            self._semaphores[loop] = semaphore
        return semaphore

    @asynccontextmanager
    async def acquire(self, *, timeout: float | None = None) -> AsyncIterator[Slot]:
        """Hold a generation slot for the duration of the block.

        The slot is released when the block exits, including on error.

        Raises:
            GateTimeout: if `timeout` elapses before a slot frees up.
        """
        slot = await self._take(timeout=timeout)
        try:
            yield slot
        finally:
            slot.release()

    @asynccontextmanager
    async def lease(self, *, timeout: float | None = None) -> AsyncIterator[Slot]:
        """Take a slot whose release the caller controls.

        The slot is released when the block completes normally. If the body
        raises, it is deliberately *not* released here: that is the signal that
        the caller has taken ownership, because the work it started is still
        running and the server is still busy.

        Args:
            timeout: How long to wait for a slot before raising `GateTimeout`.
                Callers pass their remaining wall-clock budget, so a queued
                request cannot spend the whole allowance waiting before the
                generation it queued for even begins.
        """
        slot = await self._take(timeout=timeout)
        try:
            yield slot
        except BaseException:
            # Ordinary provider failures and cancellations must not permanently
            # consume a slot.  A caller may explicitly transfer ownership when
            # its worker thread is still running after the request times out.
            if not slot._transferred:
                slot.release()
            raise
        else:
            slot.release()

    async def _take(self, *, timeout: float | None = None) -> Slot:
        semaphore = self._semaphore()
        if timeout is None:
            await semaphore.acquire()
            return Slot(semaphore)

        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=timeout)
        except TimeoutError as exc:
            logger.warning(
                "generation.gate_timeout limit=%d waited=%.0fs", self._limit, timeout
            )
            raise GateTimeout(
                f"All {self._limit} generation slots were busy for longer than "
                f"{timeout:.0f}s. Please retry in a moment."
            ) from exc
        return Slot(semaphore)
