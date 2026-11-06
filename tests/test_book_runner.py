"""The book runner's own job lifecycle, tested without HTTP.

The routes are covered in `test_book_api.py`; this file exercises the job
directly so a cancellation can be delivered deterministically, mid-page.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine

from app.models import CartoonSettings
from app.models.book import BOOK_PAGES, AboutMe
from app.services import book_document as book_document_module
from app.services import book_runner as book_runner_module
from app.services.book_runner import BookRunner
from app.services.book_store import BookRecord, BookStatus
from app.services.concurrency import ConcurrencyGate, GateTimeout
from app.services.db_book_store import DatabaseBookStore
from app.services.image_providers.base import GeneratedImage, ImageRequest
from app.services.pipeline import GenerationPipeline
from app.services.storage import GenerationStore
from tests.helpers import RecordingProvider, make_photo_bytes


class _BlockingProvider(RecordingProvider):
    """Blocks a page call until released, so a cancel lands while work is live."""

    name = "blocking"

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def generate(self, request: ImageRequest) -> GeneratedImage:
        self.entered.set()
        self.release.wait(timeout=10)
        return super().generate(request)


def _persisted_draft(store: GenerationStore) -> str:
    """A real draft on disk: the runner refuses to start without one."""
    draft = GenerationPipeline(RecordingProvider(), store).cartoonify(
        photo_data=make_photo_bytes(),
        photo_media_type="image/png",
        settings=CartoonSettings(),
    )
    return draft.generation_id


def test_titles_are_rendered_per_page_not_per_status_write(
    tmp_path: Path, engine: Engine, monkeypatch
) -> None:
    """#12: `_page_view` runs on every transition, so it must not re-render.

    Rebuilding the page list twice per page formatted every title each time —
    roughly 700 catalogue renders for a nineteen-page book whose titles never
    change. One pass for the whole book is all the status document needs.
    """
    store = GenerationStore(tmp_path / "drafts")
    book_store = DatabaseBookStore(engine)
    generation_id = _persisted_draft(store)
    runner = BookRunner(
        provider=RecordingProvider(),
        store=store,
        book_store=book_store,
        gate=ConcurrencyGate(2),
        page_timeout_seconds=60,
    )
    record = BookRecord(
        book_id="title-count-book",
        about=AboutMe(prompt_name="Jack", about_name="Jack"),
        generation_id=generation_id,
        created_at="",
    )
    book_store.save_record(
        book_id=record.book_id,
        user_id=None,
        about=record.about,
        generation_id=generation_id,
    )

    rendered: list[str] = []
    real = book_runner_module.render_book_page

    def counting(page, settings, *, prompt_name=None):
        rendered.append(page.key)
        return real(page, settings, prompt_name=prompt_name)

    monkeypatch.setattr(book_runner_module, "render_book_page", counting)

    async def scenario() -> None:
        await runner.start(record)
        await asyncio.gather(*runner._tasks)

    asyncio.run(scenario())

    status = book_store.load_status(record.book_id)
    assert status is not None and status["status"] == BookStatus.COMPLETE
    # One pass for the titles, one for each page's PDF art. Before the fix the
    # same work happened on all ~2N status writes as well.
    assert len(rendered) <= 2 * len(BOOK_PAGES), len(rendered)


def test_a_cancelled_page_leaves_its_slot_to_the_running_worker(
    tmp_path: Path, engine: Engine
) -> None:
    """#23: cancelling the wait must not free a slot while a call is in flight.

    The worker thread cannot be interrupted, and it may still be billing, so a
    cancelled page must hand its slot over exactly as a timed-out one does.
    """
    store = GenerationStore(tmp_path / "drafts")
    book_store = DatabaseBookStore(engine)
    generation_id = _persisted_draft(store)
    provider = _BlockingProvider()
    gate = ConcurrencyGate(1)
    runner = BookRunner(
        provider=provider,
        store=store,
        book_store=book_store,
        gate=gate,
        page_timeout_seconds=30,
    )
    record = BookRecord(
        book_id="cancelled-page",
        about=AboutMe(prompt_name="Jack", about_name="Jack"),
        generation_id=generation_id,
        created_at="",
    )
    book_store.save_record(
        book_id=record.book_id,
        user_id=None,
        about=record.about,
        generation_id=generation_id,
    )

    async def scenario() -> None:
        await runner.start(record)
        assert await asyncio.to_thread(provider.entered.wait, 5.0), "no page started"
        for task in list(runner._tasks):
            task.cancel()
        await asyncio.gather(*runner._tasks, return_exceptions=True)

        # The page's thread is still inside the provider call, so the single
        # slot is still taken: a new generation must queue, not run beside it.
        with pytest.raises(GateTimeout):
            async with gate.lease(timeout=0.2):
                pass

        provider.release.set()
        deadline = asyncio.get_running_loop().time() + 5.0
        while True:
            try:
                async with gate.lease(timeout=0.05):
                    return
            except GateTimeout:
                if asyncio.get_running_loop().time() > deadline:
                    raise
                await asyncio.sleep(0.02)

    asyncio.run(scenario())


def test_a_cancelled_render_is_recorded_as_failed(tmp_path: Path, engine: Engine) -> None:
    store = GenerationStore(tmp_path / "drafts")
    book_store = DatabaseBookStore(engine)
    generation_id = _persisted_draft(store)
    provider = _BlockingProvider()
    runner = BookRunner(
        provider=provider,
        store=store,
        book_store=book_store,
        gate=ConcurrencyGate(2),
        page_timeout_seconds=10,
    )
    record = BookRecord(
        book_id="cancelled-book",
        about=AboutMe(prompt_name="Jack", about_name="Jack"),
        generation_id=generation_id,
        created_at="",
    )
    book_store.save_record(
        book_id=record.book_id,
        user_id=None,
        about=record.about,
        generation_id=generation_id,
    )

    async def scenario() -> None:
        await runner.start(record)
        assert await asyncio.to_thread(provider.entered.wait, 5.0), "no page started"
        for task in list(runner._tasks):
            task.cancel()
        await asyncio.gather(*runner._tasks, return_exceptions=True)

    asyncio.run(scenario())
    provider.release.set()  # let the abandoned worker thread finish and exit

    status = book_store.load_status(record.book_id)
    assert status is not None
    assert status["status"] == BookStatus.FAILED
    assert "cancelled" in str(status["error"])


def test_the_book_counts_its_cover_and_reports_the_pdf_phase(
    tmp_path: Path, engine: Engine, monkeypatch
) -> None:
    """A book is the cover plus the story pages, and assembly is its own phase.

    The progress line used to say "19 pages", as if the cover did not exist,
    and the last picture landing looked like the end of the job even while the
    PDF was still being put together. Both are properties of the poll document
    the runner writes, so they are pinned here.
    """
    store = GenerationStore(tmp_path / "drafts")
    book_store = DatabaseBookStore(engine)
    generation_id = _persisted_draft(store)
    runner = BookRunner(
        provider=RecordingProvider(),
        store=store,
        book_store=book_store,
        gate=ConcurrencyGate(2),
        page_timeout_seconds=60,
    )
    record = BookRecord(
        book_id="phase-book",
        about=AboutMe(prompt_name="Jack", about_name="Jack"),
        generation_id=generation_id,
        created_at="",
    )
    book_store.save_record(
        book_id=record.book_id,
        user_id=None,
        about=record.about,
        generation_id=generation_id,
    )

    assembling = threading.Event()
    release = threading.Event()
    real_build = book_document_module.build_book_pdf

    def blocking_build(*args, **kwargs):
        assembling.set()
        release.wait(timeout=15)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(book_document_module, "build_book_pdf", blocking_build)

    async def scenario() -> dict[str, object]:
        await runner.start(record)
        assert await asyncio.to_thread(assembling.wait, 10.0), "PDF assembly never started"
        mid = book_store.load_status(record.book_id) or {}
        release.set()
        await asyncio.gather(*runner._tasks)
        return mid

    mid = asyncio.run(scenario())
    final = book_store.load_status(record.book_id) or {}

    # The cover is page zero, drawn and counted like the story pages.
    assert mid["pages"][0]["key"] == "cover"
    assert mid["pages"][0]["status"] == "done"
    assert mid["pages"][0]["url"]
    # Every story page is finished, and the document says the PDF is not.
    assert all(page["status"] == "done" for page in mid["pages"])
    assert mid["phase"] == "assembling"
    assert mid["total_pages"] == len(BOOK_PAGES) + 1 == 21

    assert final["status"] == BookStatus.COMPLETE
    assert final["phase"] == "complete"
    assert len(final["pages"]) == len(BOOK_PAGES) + 1
