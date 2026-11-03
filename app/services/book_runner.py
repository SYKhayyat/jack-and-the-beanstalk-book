"""The picture-book rendering job.

Flow of one book, in story order:

    portrait (already approved) -> cover -> pages 1..N together -> PDF

The portrait was made and approved before the book started; the runner sends it
into **every** page alongside the original photo — the same double identity
anchor the portrait pipeline uses. That is the mechanism that keeps the
child the same child from page 1 to page 3: no page is ever generated without
both anchors. Both anchors are copied into the book so a later edit can re-use
them after the original draft has expired.

Pages render **concurrently**: one coroutine per page goes through
`asyncio.gather`, each taking its own lease on the shared concurrency gate, so
the provider sees no more parallel calls than the gate allows. The gate, not
this job, is what bounds the spend — and a page that outlives its deadline
hands its slot to the worker (the same slot-transfer pattern
`api._run_gated` uses) so the gate never believes the server is idle while a
billable call is still running.

The same job backs every edit: one page re-drawn, every page that casts a
replaced face, the cover alone, or the missing pages of a failed book. Only the
finished pages are re-read; the rest stay exactly as they were.

Status is written to the `BookStore` as each page moves pending -> making ->
done, so the browser can poll and show every page's state; a late failure still
leaves the finished pages visible.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from time import monotonic
from typing import Any, TypeVar, cast

from app.errors import BookBusy
from app.models.book import BOOK_PAGES, BookPage
from app.models.settings import CartoonSettings
from app.services.book_store import (
    BookRecord,
    BookStatus,
    BookStore,
    CharacterReference,
)
from app.services.concurrency import ConcurrencyGate, GateTimeout, Slot
from app.services.image_providers.base import ImageProvider, ImageRequest
from app.services.pdf import PageArt, build_book_pdf
from app.services.pipeline import UnknownGeneration
from app.services.prompts import build_book_page_prompt, build_cover_prompt, render_book_page
from app.services.storage import GenerationStore

__all__ = ["BookRunner", "CharacterReference"]

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# How often a page re-asks the per-minute brake for one billable token while a
# minute is full. Small enough to resume promptly, large enough not to spin.
_PAGE_CHARGE_RETRY_SECONDS = 0.25

#: The prompt record uses one pseudo-page for the cover.
_COVER_KEY = "cover"


@dataclass(frozen=True, slots=True)
class _Artwork:
    """Everything one render needs, reloaded from the book itself."""

    about: object  # AboutMe, kept loose to avoid an import cycle at runtime
    settings: CartoonSettings
    portrait: bytes
    portrait_media_type: str
    photo: bytes
    photo_media_type: str
    characters: Mapping[str, CharacterReference]
    # True when the photograph is missing and the portrait stands in for it,
    # which happens for books made before the photograph was stored.
    photo_is_portrait: bool = False


class BookRunner:
    """Renders books in the background, pages in parallel, through the gate."""

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
        self._active: dict[str, asyncio.Task[None]] = {}

    async def start(
        self,
        record: BookRecord,
        characters: Mapping[str, CharacterReference] | None = None,
        page_charge: Callable[[], bool] | None = None,
    ) -> None:
        """Kick off rendering. Raises if the draft with the portrait is gone.

        The portrait, source photograph and character faces are copied into the
        book immediately: from here on the book owns its art, independent of
        draft expiry and cleanup, and it can be edited later.
        """
        draft = await asyncio.to_thread(self._store.load_draft, record.generation_id)
        if draft is None:
            raise UnknownGeneration(record.generation_id)
        await asyncio.to_thread(
            self._book_store.save_portrait, record.book_id, draft.cartoon_image
        )
        await asyncio.to_thread(
            self._book_store.save_source_photo, record.book_id, draft.photo_data
        )
        resolved_characters = dict(characters or {})
        await asyncio.to_thread(
            self._book_store.save_characters, record.book_id, resolved_characters
        )
        self._schedule(
            record.book_id,
            self._render(
                record.book_id,
                page_charge=page_charge,
                page_numbers=None,
                draw_cover=True,
                only_missing=False,
                corrections={},
            ),
        )

    async def start_edit(
        self,
        book_id: str,
        *,
        page_numbers: set[int] | None,
        draw_cover: bool,
        only_missing: bool = False,
        corrections: Mapping[int, str] | None = None,
        page_charge: Callable[[], bool] | None = None,
    ) -> None:
        """Mark the edit as rendering, then schedule it; returns before any call.

        The status write happens before the response, so a browser polling
        immediately after the 202 can never see the *previous* `complete`
        document and conclude the edit already finished.

        `page_numbers=None` means every story page. `only_missing` leaves pages
        that already finished untouched (the retry path).
        """
        await asyncio.to_thread(self._mark_rendering, book_id, page_numbers, only_missing)
        self._schedule(
            book_id,
            self._render(
                book_id,
                page_charge=page_charge,
                page_numbers=page_numbers,
                draw_cover=draw_cover,
                only_missing=only_missing,
                corrections=corrections or {},
            ),
        )

    def _mark_rendering(
        self,
        book_id: str,
        page_numbers: set[int] | None,
        only_missing: bool,
    ) -> None:
        """Publish the edit's starting status before the job runs."""
        record = self._book_store.load_record(book_id)
        if record is None:
            return
        titles = tuple(
            render_book_page(
                page, record.settings, prompt_name=record.about.prompt_name
            ).scene.title
            for page in BOOK_PAGES
        )
        existing = self._stored_progress(book_id)
        progress = {
            page.number: "done" if existing.get(page.number) == "done" else "pending"
            for page in BOOK_PAGES
        }
        target = (
            {page.number for page in BOOK_PAGES} if page_numbers is None else set(page_numbers)
        )
        if only_missing:
            target = {number for number in target if progress[number] != "done"}
        for number in target:
            progress[number] = "pending"
        self._book_store.save_status(
            book_id,
            BookStatus.RENDERING,
            self._page_view(book_id, progress, titles),
            title=record.about.book_title,
        )

    def _schedule(self, book_id: str, coro: Coroutine[Any, Any, None]) -> None:
        """Run one job per book; refuse a second concurrent edit of it."""
        current = self._active.get(book_id)
        if current is not None and not current.done():
            raise BookBusy("This book is already being worked on. Wait for it to finish.")
        task = asyncio.create_task(coro)
        self._active[book_id] = task
        self._tasks.add(task)

        def _finished(done: asyncio.Task[None]) -> None:
            self._tasks.discard(done)
            if self._active.get(book_id) is done:
                self._active.pop(book_id, None)

        task.add_done_callback(_finished)

    def rebuild_pdf(self, book_id: str) -> None:
        """Re-assemble the PDF from the pages already on disk (no model calls).

        Used by the cheap About edit: only the PDF text changes, so re-drawing
        an image would be waste. Raises if the book is not complete yet; the
        next normal render will pick the new text up.
        """
        record = self._book_store.load_record(book_id)
        if record is None:
            return
        titles = tuple(
            render_book_page(
                page, record.settings, prompt_name=record.about.prompt_name
            ).scene.title
            for page in BOOK_PAGES
        )
        self._assemble_and_save(record, titles, self._stored_progress(book_id))

    # --- the job ----------------------------------------------------------------

    async def _render(
        self,
        book_id: str,
        *,
        page_charge: Callable[[], bool] | None,
        page_numbers: set[int] | None,
        draw_cover: bool,
        only_missing: bool,
        corrections: Mapping[int, str],
    ) -> None:
        record = await asyncio.to_thread(self._book_store.load_record, book_id)
        if record is None:
            logger.warning("book.edit_missing id=%s", book_id)
            return
        user = record.user_id or "-"
        started = monotonic()
        try:
            artwork = await asyncio.to_thread(self._artwork, record)
            pages = list(BOOK_PAGES)
            total = len(pages)
            prompt_name = record.about.prompt_name
            titles = tuple(
                render_book_page(page, artwork.settings, prompt_name=prompt_name).scene.title
                for page in pages
            )
            existing = await asyncio.to_thread(self._stored_progress, book_id)
            progress: dict[int, str] = {
                page.number: "done" if existing.get(page.number) == "done" else "pending"
                for page in pages
            }
            target = (
                {page.number for page in pages} if page_numbers is None else set(page_numbers)
            )
            if only_missing:
                target = {number for number in target if progress[number] != "done"}
            # A regenerated page is not done any more until its new bytes land.
            for number in target:
                progress[number] = "pending"
            status_lock = asyncio.Lock()

            async def paint() -> None:
                async with status_lock:
                    await asyncio.to_thread(
                        self._book_store.save_status,
                        book_id,
                        BookStatus.RENDERING,
                        self._page_view(book_id, progress, titles),
                        None,
                        title=record.about.book_title,
                    )

            async def mark(page_number: int, state: str) -> None:
                progress[page_number] = state
                await paint()

            async def _render_page(page: BookPage) -> PageArt:
                page_photos = [name for name in page.cast if name in artwork.characters]
                prompt = build_book_page_prompt(
                    page,
                    artwork.settings,
                    page_number=page.number,
                    total_pages=total,
                    prompt_name=prompt_name,
                    supplied_characters=tuple(artwork.characters),
                    character_photos=page_photos,
                    correction=corrections.get(page.number, ""),
                )
                references, media_types = _references(artwork, page)

                await _await_page_charge(page_charge)

                async def on_slot() -> None:
                    await mark(page.number, "making")

                image = await self._on_worker_thread_with_slot(
                    self._page_call(
                        references, media_types, prompt, artwork.settings, "book-page"
                    ),
                    on_slot=on_slot,
                )
                await asyncio.to_thread(
                    self._book_store.save_page, book_id, page.number, image
                )
                await asyncio.to_thread(
                    self._book_store.append_prompt,
                    book_id,
                    {"page": str(page.number), "key": page.key, "prompt": prompt},
                )
                await mark(page.number, "done")
                return PageArt(
                    image=image,
                    page=render_book_page(page, artwork.settings, prompt_name=prompt_name),
                )

            async def run_page(page: BookPage) -> PageArt:
                page_started = monotonic()
                try:
                    art = await _render_page(page)
                except BaseException as exc:
                    logger.warning(
                        "book.page user=%s book=%s page=%d/%d key=%s "
                        "ms=%.0f outcome=failed error=%s",
                        user,
                        book_id,
                        page.number,
                        total,
                        page.key,
                        (monotonic() - page_started) * 1000,
                        type(exc).__name__,
                    )
                    raise
                logger.info(
                    "book.page user=%s book=%s page=%d/%d key=%s bytes=%d "
                    "ms=%.0f outcome=done",
                    user,
                    book_id,
                    page.number,
                    total,
                    page.key,
                    len(art.image),
                    (monotonic() - page_started) * 1000,
                )
                return art

            await paint()
            if draw_cover:
                await self._render_cover(record, artwork, page_charge)
            to_render = [page for page in pages if page.number in target]
            rendered: dict[int, PageArt] = {}
            if to_render:
                results = await asyncio.gather(
                    *(run_page(page) for page in to_render), return_exceptions=True
                )
                fatal = [result for result in results if isinstance(result, BaseException)]
                if fatal:
                    raise fatal[0]
                rendered = {
                    cast("PageArt", result).page.number: cast("PageArt", result)
                    for result in results
                }

            pdf_bytes = await asyncio.to_thread(
                self._assemble_and_save, record, titles, progress, rendered
            )
            logger.info(
                "book.complete user=%s book=%s pages=%d pdf_bytes=%d ms=%.0f outcome=complete",
                user,
                book_id,
                total,
                pdf_bytes,
                (monotonic() - started) * 1000,
            )
        except asyncio.CancelledError:
            self._fail(
                book_id,
                "Rendering was cancelled before the book finished. The pages "
                "already finished were kept — starting a new book is safe, the "
                "same portrait is reused.",
                user=user,
                elapsed_ms=(monotonic() - started) * 1000,
            )
            raise
        except GateTimeout:
            self._fail(
                book_id,
                "The server was too busy to render this book's pages, so it "
                "stopped. The pages already finished were made and paid for — "
                "starting a new book is safe, the same portrait is reused.",
                user=user,
                elapsed_ms=(monotonic() - started) * 1000,
            )
        except TimeoutError:
            self._fail(
                book_id,
                "A page took too long to generate, so the book was stopped. The "
                "pages already finished were kept.",
                user=user,
                elapsed_ms=(monotonic() - started) * 1000,
            )
        except Exception as exc:  # keep the completed pages visible whatever happened
            logger.exception("book.failed user=%s book=%s", user, book_id)
            self._fail(
                book_id,
                f"The book stopped early: {exc}",
                user=user,
                elapsed_ms=(monotonic() - started) * 1000,
            )

    def _assemble_and_save(
        self,
        record: BookRecord,
        titles: Sequence[str],
        progress: Mapping[int, str],
        rendered: Mapping[int, PageArt] | None = None,
    ) -> int:
        """Build the PDF from every page on disk, then publish complete.

        `rendered` carries the pages this job just drew, so a full render does
        not re-format every page a third time just to assemble them. Pages an
        edit did not touch are read back from the store.
        """
        arts: list[PageArt] = []
        fresh = rendered or {}
        for page in BOOK_PAGES:
            if page.number in fresh:
                arts.append(fresh[page.number])
                continue
            image = self._book_store.read_page(record.book_id, page.number)
            if image is None:
                raise ValueError(f"Page {page.number} is missing; cannot assemble the book.")
            arts.append(
                PageArt(
                    image=image,
                    page=render_book_page(
                        page, record.settings, prompt_name=record.about.prompt_name
                    ),
                )
            )
        cover = self._book_store.read_cover(record.book_id)
        pdf_bytes = build_book_pdf(record.about, arts, cover)
        self._book_store.save_pdf(record.book_id, pdf_bytes)
        self._book_store.save_status(
            record.book_id,
            BookStatus.COMPLETE,
            self._page_view(record.book_id, progress, titles),
            title=record.about.book_title,
        )
        return len(pdf_bytes)

    async def _render_cover(
        self,
        record: BookRecord,
        artwork: _Artwork,
        page_charge: Callable[[], bool] | None,
    ) -> bytes:
        """Draw and persist the cover: one billable call, before the pages."""
        prompt = build_cover_prompt(artwork.settings, prompt_name=record.about.prompt_name)
        await _await_page_charge(page_charge)
        image = await self._on_worker_thread_with_slot(
            self._page_call(
                [artwork.portrait, artwork.photo],
                [artwork.portrait_media_type, artwork.photo_media_type],
                prompt,
                artwork.settings,
                "book-cover",
            )
        )
        await asyncio.to_thread(self._book_store.save_cover, record.book_id, image)
        await asyncio.to_thread(
            self._book_store.append_prompt,
            record.book_id,
            {"page": "cover", "key": _COVER_KEY, "prompt": prompt},
        )
        return image

    async def _on_worker_thread_with_slot(
        self, work: Callable[[], _T], *, on_slot: Callable[[], Awaitable[None]] | None = None
    ) -> _T:
        """Blocking `work` on a worker thread while this task holds a gate slot.

        The page timeout binds the slot wait and the work together. If the work
        outlives it, ownership of the slot transfers to the worker: it is still
        running (and possibly still billing), and only it knows when it ends.
        """
        async with self._gate.lease(timeout=self._page_timeout) as slot:
            if on_slot is not None:
                await on_slot()
            hop = asyncio.create_task(asyncio.to_thread(work))
            try:
                return await asyncio.wait_for(asyncio.shield(hop), timeout=self._page_timeout)
            except BaseException:
                if not hop.done():
                    _hand_slot_to_worker(slot, hop)
                raise

    # --- one page ---------------------------------------------------------------

    def _page_call(
        self,
        references: Sequence[bytes],
        media_types: Sequence[str],
        prompt: str,
        settings: CartoonSettings,
        label: str,
    ) -> Callable[[], bytes]:
        """The blocking half of one render (not yet on a thread)."""

        def work() -> bytes:
            return self._provider.generate(
                ImageRequest(
                    prompt=prompt,
                    reference_images=tuple(references),
                    reference_media_types=tuple(media_types),
                    output_format="png",
                    aspect_ratio=str(settings.aspect_ratio),
                    label=label,
                )
            ).data

        return work

    # --- helpers ----------------------------------------------------------------

    def _artwork(self, record: BookRecord) -> _Artwork:
        portrait = self._book_store.read_portrait(record.book_id)
        if portrait is None:
            raise ValueError("This book's stored portrait is gone, so it cannot be redrawn.")
        photo = self._book_store.read_source_photo(record.book_id)
        # Books made before the photograph was stored keep their portrait only.
        # The portrait is the likeness the pages were drawn from, so it stands
        # in as the second anchor rather than the book becoming uneditable.
        if photo is None:
            photo = portrait
            photo_is_portrait = True
        else:
            photo_is_portrait = False
        return _Artwork(
            about=record.about,
            settings=record.settings,
            portrait=portrait,
            portrait_media_type=_media_type(portrait),
            photo=photo,
            photo_media_type=_media_type(photo),
            photo_is_portrait=photo_is_portrait,
            characters=self._book_store.load_characters(record.book_id),
        )

    def _stored_progress(self, book_id: str) -> dict[int, str]:
        status = self._book_store.load_status(book_id) or {}
        progress: dict[int, str] = {}
        raw = status.get("pages", [])
        if isinstance(raw, list):
            for entry in raw:
                if isinstance(entry, dict) and entry.get("status") == "done":
                    try:
                        progress[int(str(entry["number"]))] = "done"
                    except (KeyError, TypeError, ValueError):
                        continue
        return progress

    def _fail(
        self, book_id: str, message: str, *, user: str = "-", elapsed_ms: float = 0.0
    ) -> None:
        status = self._book_store.load_status(book_id) or {"pages": []}
        record = self._book_store.load_record(book_id)
        pages: list[dict[str, object]] = []
        for entry in cast("list[object]", status.get("pages", [])):
            if not isinstance(entry, dict):
                continue
            page_entry = dict(entry)
            if page_entry.get("status") != "done":
                page_entry["status"] = "pending"
                page_entry["url"] = None
            pages.append(page_entry)
        self._book_store.save_status(
            book_id,
            BookStatus.FAILED,
            pages,
            error=message,
            title=record.about.book_title if record is not None else None,
        )
        done = sum(1 for entry in pages if entry.get("status") == "done")
        logger.error(
            "book.failure user=%s book=%s pages_done=%d ms=%.0f outcome=failed message=%s",
            user,
            book_id,
            done,
            elapsed_ms,
            message,
        )

    def _page_view(
        self,
        book_id: str,
        progress: Mapping[int, str],
        titles: Sequence[str],
    ) -> list[dict[str, object]]:
        """The full page list for status, in story order, from live progress."""
        view: list[dict[str, object]] = []
        for page, title in zip(BOOK_PAGES, titles, strict=True):
            state = progress.get(page.number, "pending")
            finished = state == "done"
            view.append(
                {
                    "number": page.number,
                    "key": page.key,
                    "title": title,
                    "url": self._book_store.page_url(book_id, page.number)
                    if finished
                    else None,
                    "status": state,
                }
            )
        return view


async def _await_page_charge(charge: Callable[[], bool] | None) -> None:
    """Wait until one billable page fits in this minute's allowance."""
    if charge is None:
        return
    while not charge():
        await asyncio.sleep(_PAGE_CHARGE_RETRY_SECONDS)


def _references(
    artwork: _Artwork,
    page: BookPage,
) -> tuple[list[bytes], list[str]]:
    """Exactly the reference images for one page, in the order the prompt names."""
    references = [artwork.portrait]
    media_types = [artwork.portrait_media_type]
    if not artwork.photo_is_portrait:
        # The same picture twice is a wasted reference slot.
        references.append(artwork.photo)
        media_types.append(artwork.photo_media_type)
    for name in page.cast:
        character = artwork.characters.get(name)
        if character is None:
            continue
        references.extend([character.portrait, character.photo])
        media_types.extend([character.portrait_media_type, character.photo_media_type])
    return references, media_types


def _media_type(data: bytes) -> str:
    return "image/jpeg" if data[:2] == b"\xff\xd8" else "image/png"


def _hand_slot_to_worker(slot: Slot, hop: asyncio.Task[object]) -> None:
    """Give the slot to work that outlives the awaited request."""
    slot.transfer()
    hop.add_done_callback(lambda _finished: slot.release())
