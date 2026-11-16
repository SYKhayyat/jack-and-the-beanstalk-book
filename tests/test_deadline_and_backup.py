"""A book's deadline (A1) and the backup call for a slow picture (A2).

Audit 2026-11-15: a book should be done in under a minute. Two things serve
that without risking a hole in the book:

* the **deadline** — a page's retry is only started when it can finish in the
  time the book has left, judged by how long that page's attempts took; the
  best picture so far is kept, and a first attempt always runs;
* the **backup call** — a picture call still running after a while gets an
  identical second call on a free gate slot, and the first good answer wins.
  A backup never queues for a slot, and the call that loses keeps its slot
  until its thread really ends.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from time import monotonic
from typing import Any

import pytest

from app.services.book_runner import BookRunner
from app.services.concurrency import ConcurrencyGate
from app.services.image_providers.base import ProviderError
from tests.test_review_loop import ScriptedReviewer, artwork, grain, runner_with, said

# --- the deadline ----------------------------------------------------------------


@dataclass
class _Drawn:
    data: bytes
    media_type: str = "image/png"
    model: str = "test"


def _failing_page(deadline: float | None) -> tuple[Any, int]:
    """One page whose every picture fails review, against a deadline."""
    runner = runner_with(
        ScriptedReviewer(answers=[said(False, 20, faults=("extra_person",))] * 6)
    )
    draws = 0

    async def draw(prompt: str, mend: Any) -> Any:
        nonlocal draws
        draws += 1
        return _Drawn(grain(seed=draws))

    attempts = asyncio.run(
        runner._draw_until_pass(
            book_id="book",
            page_number=5,
            user="-",
            page_title="the kitchen",
            story_line="the mother sends Jack to market",
            cast_note="Only these people may be in the picture: the child.",
            star_in_picture=True,
            plate=None,
            artwork=artwork(plated=False),
            draw=draw,
            prompt_for=lambda *_: "draw it",
            deadline=deadline,
        )
    )
    return attempts, draws


def test_a_retry_that_cannot_finish_by_the_deadline_is_not_started(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="app.services.book_runner"):
        attempts, draws = _failing_page(deadline=monotonic() - 1)

    assert draws == 1, "the first attempt always runs"
    assert attempts.best_effort is True
    assert attempts.verdicts[-1].get("deadline_stopped") is True
    assert any("why=deadline_stopped" in r.getMessage() for r in caplog.records)


def test_with_time_left_a_failing_page_is_drawn_again() -> None:
    attempts, draws = _failing_page(deadline=monotonic() + 3600)

    assert draws == 3
    assert not any(v.get("deadline_stopped") for v in attempts.verdicts)


def test_no_deadline_never_stops_a_redraw() -> None:
    _attempts, draws = _failing_page(deadline=None)

    assert draws == 3


# --- the backup call -------------------------------------------------------------


class _Calls:
    """Picture calls on worker threads: each blocks until the test lets it go.

    `outcomes[n]` is what call n does once let go: a value to return, or an
    exception to raise. Call n is let go by `release[n].set()`.
    """

    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = outcomes
        self.release = [threading.Event() for _ in outcomes]
        self.started = 0
        self._lock = threading.Lock()

    def __call__(self) -> Any:
        with self._lock:
            n = self.started
            self.started += 1
        self.release[n].wait(timeout=10)
        outcome = self.outcomes[n]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _racer(*, limit: int = 2, backup_after: float = 0.05) -> BookRunner:
    runner = BookRunner.__new__(BookRunner)
    runner._gate = ConcurrencyGate(limit)
    runner._page_timeout = 10
    runner._backup_after = backup_after  # type: ignore[assignment]
    return runner


async def _free_slots(gate: ConcurrencyGate) -> int:
    """How many slots are free now (taken and given back)."""
    taken = []
    while (slot := await gate.try_take()) is not None:
        taken.append(slot)
    for slot in taken:
        slot.release()
    return len(taken)


async def _until(condition: Any) -> None:
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("never happened")


def test_a_slow_call_loses_to_its_backup_and_keeps_its_slot_until_it_ends(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = _Calls("first", "backup")
    runner = _racer()

    async def scenario() -> None:
        race = asyncio.create_task(runner._on_worker_thread_with_slot(calls))
        await _until(lambda: calls.started == 2)
        calls.release[1].set()
        assert await race == "backup"
        # The first call is still on its thread, and still holds its slot.
        assert await _free_slots(runner._gate) == 1
        calls.release[0].set()
        await _until(lambda: runner._gate._semaphore()._value == 2)

    with caplog.at_level(logging.INFO, logger="app.services.book_runner"):
        asyncio.run(scenario())
    lines = [r.getMessage() for r in caplog.records if "book.backup_call" in r.getMessage()]
    assert any("outcome=started" in line for line in lines)
    assert any("outcome=backup_won" in line for line in lines)


def test_a_call_back_before_the_backup_time_needs_no_backup() -> None:
    calls = _Calls("first", "backup")
    calls.release[0].set()
    runner = _racer(backup_after=5)

    assert asyncio.run(runner._on_worker_thread_with_slot(calls)) == "first"
    assert calls.started == 1


def test_no_backup_is_started_when_every_slot_is_busy(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A backup must never take the slot of a picture that has not started."""
    calls = _Calls("first", "backup")
    runner = _racer(limit=1)

    async def scenario() -> str:
        race = asyncio.create_task(runner._on_worker_thread_with_slot(calls))
        await asyncio.sleep(0.2)
        calls.release[0].set()
        return await race

    with caplog.at_level(logging.INFO, logger="app.services.book_runner"):
        assert asyncio.run(scenario()) == "first"
    assert calls.started == 1
    assert any("outcome=skipped" in r.getMessage() for r in caplog.records)


def test_a_failed_call_waits_for_the_other_one() -> None:
    calls = _Calls(ProviderError("boom"), "backup")
    runner = _racer()

    async def scenario() -> str:
        race = asyncio.create_task(runner._on_worker_thread_with_slot(calls))
        await _until(lambda: calls.started == 2)
        calls.release[0].set()
        await asyncio.sleep(0.05)
        assert not race.done(), "the backup is still drawing: it is the answer"
        calls.release[1].set()
        return await race

    assert asyncio.run(scenario()) == "backup"


def test_when_both_fail_the_first_calls_error_is_raised() -> None:
    calls = _Calls(ProviderError("first"), ProviderError("second"))
    runner = _racer()

    async def scenario() -> None:
        race = asyncio.create_task(runner._on_worker_thread_with_slot(calls))
        await _until(lambda: calls.started == 2)
        calls.release[1].set()
        calls.release[0].set()
        with pytest.raises(ProviderError, match="first"):
            await race
        await _until(lambda: runner._gate._semaphore()._value == 2)

    asyncio.run(scenario())


def test_a_backup_of_zero_never_races() -> None:
    calls = _Calls("first", "backup")
    runner = _racer(backup_after=0)

    async def scenario() -> str:
        race = asyncio.create_task(runner._on_worker_thread_with_slot(calls))
        await asyncio.sleep(0.15)
        calls.release[0].set()
        return await race

    assert asyncio.run(scenario()) == "first"
    assert calls.started == 1


# --- try_take --------------------------------------------------------------------


def test_try_take_never_waits() -> None:
    async def scenario() -> None:
        gate = ConcurrencyGate(1)
        slot = await gate.try_take()
        assert slot is not None
        assert await gate.try_take() is None
        slot.release()
        assert await gate.try_take() is not None

    asyncio.run(scenario())


# --- the upload size caps (A4) -----------------------------------------------------


def test_the_plate_and_the_other_references_have_their_own_size_caps() -> None:
    import io

    from PIL import Image

    from app.services.image_providers.openai_provider import _as_upload

    buffer = io.BytesIO()
    Image.new("RGB", (2000, 1000), "red").save(buffer, format="PNG")
    big = buffer.getvalue()

    plate = Image.open(_as_upload(big, "image/png", 0, (800, 300)))
    face = Image.open(_as_upload(big, "image/png", 1, (800, 300)))

    assert max(plate.size) == 800
    assert max(face.size) == 300
