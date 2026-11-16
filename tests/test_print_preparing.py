"""The background print build: one per book, one at a time, failures kept."""

from __future__ import annotations

import asyncio
import hashlib
import threading

import pytest

from app.services.print import preparing
from app.services.print.base import PrintError
from app.services.print.preparing import PrintPreparer

PDF = b"%PDF-approved"
SHA = hashlib.sha256(PDF).hexdigest()


@pytest.fixture(autouse=True)
def _no_trim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preparing, "release_memory", lambda: None)


async def _until_idle(preparer: PrintPreparer) -> None:
    for _ in range(500):
        if not preparer._tasks:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("still building")


def test_a_running_build_is_shown_and_a_second_start_joins_it() -> None:
    async def scenario() -> None:
        preparer = PrintPreparer()
        gate = threading.Event()
        calls: list[int] = []

        def build() -> None:
            calls.append(1)
            gate.wait(5)

        assert preparer.start("b1", pdf=PDF, product_key="k", build=build)
        state = preparer.state("b1", SHA)
        assert state is not None and state.running
        assert not preparer.start("b1", pdf=PDF, product_key="k", build=build)
        gate.set()
        await _until_idle(preparer)
        assert calls == [1]
        # Done: the stored approval speaks for the book from now on.
        assert preparer.state("b1", SHA) is None

    asyncio.run(scenario())


def test_a_refusal_is_kept_for_that_version_only() -> None:
    async def scenario() -> None:
        preparer = PrintPreparer()

        def build() -> None:
            raise PrintError("too many pages for this binding")

        preparer.start("b1", pdf=PDF, product_key="k", build=build)
        await _until_idle(preparer)
        state = preparer.state("b1", SHA)
        assert state is not None and not state.running
        assert state.error == "too many pages for this binding"
        # An edited book is a different PDF: the old failure is not its own.
        assert preparer.state("b1", hashlib.sha256(b"edited").hexdigest()) is None

    asyncio.run(scenario())


def test_an_unexpected_error_is_shown_plainly() -> None:
    async def scenario() -> None:
        preparer = PrintPreparer()

        def build() -> None:
            raise RuntimeError("disk on fire")

        preparer.start("b1", pdf=PDF, product_key="k", build=build)
        await _until_idle(preparer)
        state = preparer.state("b1", SHA)
        assert state is not None
        assert "disk" not in state.error and "try again" in state.error

    asyncio.run(scenario())


def test_builds_for_two_books_run_one_after_the_other() -> None:
    async def scenario() -> None:
        preparer = PrintPreparer()
        lock = threading.Lock()
        running = 0
        most = 0

        def build() -> None:
            nonlocal running, most
            with lock:
                running += 1
                most = max(most, running)
            threading.Event().wait(0.05)
            with lock:
                running -= 1

        preparer.start("b1", pdf=PDF, product_key="k", build=build)
        preparer.start("b2", pdf=PDF, product_key="k", build=build)
        await _until_idle(preparer)
        assert most == 1

    asyncio.run(scenario())


def test_close_stops_a_waiting_build() -> None:
    async def scenario() -> None:
        preparer = PrintPreparer()
        gate = threading.Event()
        preparer.start("b1", pdf=PDF, product_key="k", build=lambda: gate.wait(5))
        preparer.start("b2", pdf=PDF, product_key="k", build=lambda: None)
        await asyncio.sleep(0.05)
        await asyncio.wait_for(asyncio.gather(preparer.close(), _release(gate)), 5)
        assert preparer.state("b2", SHA) is None

    async def _release(gate: threading.Event) -> None:
        await asyncio.sleep(0.05)
        gate.set()

    asyncio.run(scenario())
