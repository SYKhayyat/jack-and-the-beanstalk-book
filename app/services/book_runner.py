"""The picture-book rendering job.

Flow of one book, in story order:

    portrait (already approved) -> page 1 -> page 2 -> page 3 -> ... -> PDF

The portrait was made and approved before the book started; the runner sends it
into **every** page alongside the original photo — the same double identity
anchor the single-scene pipeline uses. That is the mechanism that keeps the
child the same child from page 1 to page 3: no page is ever generated without
both anchors.

The runner runs on the app's event loop (the concurrency gate is loop-bound) and
pushes the blocking provider calls to worker threads, with the same slot-transfer
pattern `api._run_gated` uses for interactive generations: when a page render
outlives its deadline, the slot stays with the worker until it genuinely
finishes, so the gate never believes the server is idle while it is not.

Status is written to the `BookStore` after every page, so the browser can poll
and show pages as they land; a late failure still leaves the finished pages
visible.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TypeVar, cast

from app.models.book import BOOK_PAGES, BookPage
from app.models.settings import CartoonSettings
from app.services.book_store import BookRecord, BookStatus, BookStore
from app.services.concurrency import ConcurrencyGate, GateTimeout, Slot
from app.services.image_providers.base import ImageProvider, ImageRequest
from app.services.pdf import PageArt, build_book_pdf
from app.services.pipeline import UnknownGeneration
from app.services.prompts import build_book_page_prompt, render_book_page
from app.services.storage import Draft, GenerationStore

__all__ = ["BookRunner"]

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class BookRunner:
    """Renders books in the background, one page at a time, through the gate."""

    def __init__(
        self,
        *,
        provider: ImageProvider,
        store: GenerationStore,
        book_store: BookStore,
        gate: ConcurrencyGate,
        page_timeout_seconds: int,
    ) -> None:
        self._provider = provider
        self._store = store
        self._book_store = book_store
        self._gate = gate
        self._page_timeout = page_timeout_seconds
        self._tasks: set[asyncio.Task[None]] = set()

    def start(self, record: BookRecord) -> None:
        """Kick off rendering. Raises if the draft with the portrait is gone.

        Runs on the request's loop; the task outlives the POST /api/book/start
        response, so it is not cancelled when that response is sent.
        """
        draft = self._store.load_draft(record.generation_id)
        if draft is None:
            raise UnknownGeneration(record.generation_id)
        # Copy the portrait into the book immediately: from here on the book
        # owns its art, independent of draft expiry and cleanup.
        self._book_store.save_portrait(record.book_id, draft.cartoon_image)

        task = asyncio.create_task(self._render(record))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- the job ----------------------------------------------------------------

    async def _render(self, record: BookRecord) -> None:
        book_id = record.book_id
        try:
            draft = self._store.load_draft(record.generation_id)
            if draft is None:  # expired between start() and here
                raise UnknownGeneration(record.generation_id)

            total = len(BOOK_PAGES)
            arts: list[PageArt] = []
            prompts: list[dict[str, str]] = []

            for done, page in enumerate(BOOK_PAGES, start=1):
                # `done - 1` pages are actually on disk right now; this page is
                # in flight, so it must not yet show as done — otherwise a
                # mid-page failure would publish a stale "done" claim.
                self._book_store.save_status(
                    book_id,
                    BookStatus.RENDERING,
                    self._page_view(book_id, done - 1, draft.settings),
                )
                image, prompt = await self._render_page(
                    draft, page, page_number=page.number, total_pages=total
                )
                arts.append(PageArt(image=image, page=render_book_page(page, draft.settings)))
                prompts.append({"page": str(page.number), "key": page.key, "prompt": prompt})
                self._book_store.save_page(book_id, page.number, image)
                logger.info(
                    "book.page id=%s page=%d/%d key=%s bytes=%d",
                    book_id,
                    page.number,
                    total,
                    page.key,
                    len(image),
                )

            self._book_store.save_prompts(book_id, prompts)
            pdf_bytes = build_book_pdf(record.about, arts)
            self._book_store.save_pdf(book_id, pdf_bytes)
            self._book_store.save_status(
                book_id, BookStatus.COMPLETE, self._page_view(book_id, total, draft.settings)
            )
            logger.info("book.complete id=%s pdf_bytes=%d", book_id, len(pdf_bytes))
        except UnknownGeneration as exc:
            self._fail(book_id, str(exc))
        except GateTimeout:
            self._fail(
                book_id,
                "The server was too busy to start this book's pages. Nothing was "
                "charged for them — try again in a moment.",
            )
        except TimeoutError:
            self._fail(
                book_id,
                "A page took too long to generate, so the book was stopped. The "
                "pages already finished were kept.",
            )
        except Exception as exc:  # keep the completed pages visible whatever happened
            logger.exception("book.failed id=%s", book_id)
            self._fail(book_id, f"The book stopped early: {exc}")

    # --- one page ---------------------------------------------------------------

    async def _render_page(
        self, draft: Draft, page: BookPage, *, page_number: int, total_pages: int
    ) -> tuple[bytes, str]:
        """One page: prompt pinned first (cheap), then the billable call."""
        prompt = build_book_page_prompt(
            page, draft.settings, page_number=page_number, total_pages=total_pages
        )
        image = await self._on_worker_thread_with_slot(self._page_call(draft, prompt))
        return image, prompt

    def _page_call(self, draft: Draft, prompt: str) -> Callable[[], bytes]:
        """The blocking half of one page render (not yet on a thread)."""

        def work() -> bytes:
            return self._provider.generate(
                ImageRequest(
                    prompt=prompt,
                    # Both identity anchors on every single page: the approved
                    # portrait and the original photo behind it.
                    reference_images=(draft.cartoon_image, draft.photo_data),
                    reference_media_types=("image/png", draft.photo_media_type),
                    output_format="png",
                    aspect_ratio=str(draft.settings.aspect_ratio),
                    label="book-page",
                )
            ).data

        return work

    async def _on_worker_thread_with_slot(self, work: Callable[[], _T]) -> _T:
        """Blocking `work` on a worker thread while this task holds a gate slot.

        The page timeout binds the slot wait and the work together. If the work
        outlives it, ownership of the slot transfers to the worker: it is still
        running (and possibly still billing), and only it knows when it ends.
        """
        async with self._gate.lease(timeout=self._page_timeout) as slot:
            hop = asyncio.create_task(asyncio.to_thread(work))
            try:
                return await asyncio.wait_for(asyncio.shield(hop), timeout=self._page_timeout)
            except TimeoutError:
                _hand_slot_to_worker(slot, hop)
                raise

    # --- helpers ----------------------------------------------------------------

    def _fail(self, book_id: str, message: str) -> None:
        status = self._book_store.load_status(book_id) or {"pages": []}
        pages: list[dict[str, object]] = [  # defensive: a failed page keeps its record
            entry
            for entry in cast("list[object]", status.get("pages", []))
            if isinstance(entry, dict)
        ]
        self._book_store.save_status(book_id, BookStatus.FAILED, pages, error=message)
        done = sum(1 for entry in pages if entry.get("status") == "done")
        logger.error("book.failure id=%s pages_done=%d message=%s", book_id, done, message)

    def _page_view(
        self, book_id: str, done_count: int, settings: CartoonSettings
    ) -> list[dict[str, object]]:
        """The full page list for status: done pages, then the rest pending."""
        view: list[dict[str, object]] = []
        for page in BOOK_PAGES:
            finished = page.number <= done_count
            view.append(
                {
                    "number": page.number,
                    "key": page.key,
                    "title": render_book_page(page, settings).scene.title,
                    "url": self._book_store.page_url(book_id, page.number)
                    if finished
                    else None,
                    "status": "done" if finished else "pending",
                }
            )
        return view


def _hand_slot_to_worker(slot: Slot, hop: asyncio.Task[object]) -> None:
    """Give the slot to work that outlives the awaited deadline.

    The page request may have timed out, but its worker thread is still
    generating (and possibly billing). The gate must show busy until that
    worker really finishes, so the worker releases the slot when it is done.
    """
    slot.transfer()
    hop.add_done_callback(lambda _finished: slot.release())
