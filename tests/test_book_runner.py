"""The book runner's own job lifecycle, tested without HTTP.

The routes are covered in `test_book_api.py`; this file exercises the job
directly so a cancellation can be delivered deterministically, mid-page.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from app.models import CartoonSettings
from app.models.book import AboutMe
from app.services.book_runner import BookRunner
from app.services.book_store import BookRecord, BookStatus, FileBookStore
from app.services.concurrency import ConcurrencyGate
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


def test_a_cancelled_render_is_recorded_as_failed(tmp_path: Path) -> None:
    store = GenerationStore(tmp_path / "drafts")
    book_store = FileBookStore(tmp_path / "books")
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
        runner.start(record)
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
