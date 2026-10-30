"""The picture-book rendering job.

Flow of one book, in story order:

    portrait (already approved) -> pages 1..N together -> PDF

The portrait was made and approved before the book started; the runner sends it
into **every** page alongside the original photo — the same double identity
anchor the portrait pipeline uses. That is the mechanism that keeps the
child the same child from page 1 to page 3: no page is ever generated without
both anchors.

Pages render **concurrently**: one coroutine per page goes through
`asyncio.gather`, each taking its own lease on the shared concurrency gate, so
the provider sees no more parallel calls than the gate allows. The gate, not
this job, is what bounds the spend — and a page that outlives its deadline
hands its slot to the worker (the same slot-transfer pattern
`api._run_gated` uses) so the gate never believes the server is idle while a
billable call is still running.

Status is written to the `BookStore` as each page moves pending -> making ->
done, so the browser can poll and show every page's state; a late failure still
leaves the finished pages visible.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from time import monotonic
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

__all__ = ["BookRunner", "CharacterReference"]

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# How often a page re-asks the per-minute brake for one billable token while a
# minute is full. Small enough to resume promptly, large enough not to spin.
_PAGE_CHARGE_RETRY_SECONDS = 0.25


@dataclass(frozen=True, slots=True)
class CharacterReference:
    """A cast member the uploader gave a face to.

    Two images, mirroring the child's double anchor: the approved cartoon
    portrait (style-matched to the book) and the original photograph behind
    it. The portrait and photo take the same place per page as the child's.
    """

    portrait: bytes
    portrait_media_type: str
    photo: bytes
    photo_media_type: str


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

    async def start(
        self,
        record: BookRecord,
        characters: Mapping[str, CharacterReference] | None = None,
        page_charge: Callable[[], bool] | None = None,
    ) -> None:
        """Kick off rendering. Raises if the draft with the portrait is gone.

        `characters` maps cast keys to the reference images the uploader
        supplied for them (validated by the route against the book's
        photoable cast). It is held by the job, not re-read per page: the set
        of photographed characters is fixed for the book.

        `page_charge` is the per-minute brake's per-page admission: it returns
        True when one billable page was charged and False when the minute is
        full. A full minute makes the book take longer, never fail — a book is
        one product, and its page count must not be able to refuse its own
        start. The route supplies it so this class stays ignorant of HTTP
        client keys.

        This is async because both the draft read and the portrait write are
        blocking I/O — against a database, each is a network round trip with a
        multi-megabyte payload. They run on worker threads, so an in-flight
        book start cannot stall the event loop and the progress polls it is
        serving. The render task itself is created on the caller's loop and
        outlives the POST /api/book/start response.
        """
        draft = await asyncio.to_thread(self._store.load_draft, record.generation_id)
        if draft is None:
            raise UnknownGeneration(record.generation_id)
        # Copy the portrait into the book immediately: from here on the book
        # owns its art, independent of draft expiry and cleanup.
        await asyncio.to_thread(
            self._book_store.save_portrait, record.book_id, draft.cartoon_image
        )

        task = asyncio.create_task(
            self._render(record, draft, dict(characters or {}), page_charge)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- the job ----------------------------------------------------------------

    async def _render(
        self,
        record: BookRecord,
        draft: Draft,
        characters: Mapping[str, CharacterReference],
        page_charge: Callable[[], bool] | None,
    ) -> None:
        book_id = record.book_id
        user = record.user_id or "-"
        started = monotonic()
        try:
            pages = list(BOOK_PAGES)
            total = len(pages)
            prompt_name = record.about.prompt_name
            # The live progress map, page number -> pending | making | done.
            # Pages render concurrently, so every status write takes the lock
            # and snapshots the whole map: two pages finishing at once can
            # never publish a document each missing the other's page.
            progress: dict[int, str] = {page.number: "pending" for page in pages}
            status_lock = asyncio.Lock()
            # Rendered once for the whole book. The titles depend only on the
            # settings and the prompt name, and `paint` runs twice per page, so
            # formatting them inside `_page_view` meant ~40 full-catalogue
            # re-renders per book to publish documents that differed only in
            # one page's status.
            titles = tuple(
                render_book_page(page, draft.settings, prompt_name=prompt_name).scene.title
                for page in pages
            )

            async def paint() -> None:
                async with status_lock:
                    await asyncio.to_thread(
                        self._book_store.save_status,
                        book_id,
                        BookStatus.RENDERING,
                        self._page_view(book_id, progress, titles),
                    )

            async def mark(page_number: int, state: str) -> None:
                progress[page_number] = state
                await paint()

            async def _render_page(page: BookPage) -> PageArt:
                page_photos = [name for name in page.cast if name in characters]
                prompt = build_book_page_prompt(
                    page,
                    draft.settings,
                    page_number=page.number,
                    total_pages=total,
                    prompt_name=prompt_name,
                    supplied_characters=tuple(characters),
                    character_photos=page_photos,
                )
                references, media_types = _references(draft, page, characters)
                # Take this page's share of the per-minute brake before the
                # gate: a page waiting for allowance is not yet being drawn,
                # so it must not hold a slot or claim "making". A full minute
                # delays the book; it never fails it.
                await _await_page_charge(page_charge)

                async def on_slot() -> None:
                    # "making" the moment the page owns a slot, and not before:
                    # a page still queued behind the gate's limit is waiting,
                    # not being drawn, and the visitor is told the difference.
                    await mark(page.number, "making")

                image = await self._on_worker_thread_with_slot(
                    self._page_call(references, media_types, prompt, draft.settings),
                    on_slot=on_slot,
                )
                # Craft first, then publish: the payload may only advertise a
                # done page once its bytes are actually in the store.
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
                    page=render_book_page(page, draft.settings, prompt_name=prompt_name),
                )

            async def run_page(page: BookPage) -> PageArt:
                # One line per page, whatever the outcome: book id, owner, page
                # number, bytes and how long it took. `done` means the bytes are
                # in the store; `failed` is logged here and then re-raised so
                # the book-level failure keeps the pages that did finish.
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
            results = await asyncio.gather(
                *(run_page(page) for page in pages), return_exceptions=True
            )
            fatal = [result for result in results if isinstance(result, BaseException)]
            if fatal:
                # gather(return_exceptions=True) lets every page settle, so the
                # pages that did finish are on disk and stay visible; the first
                # failure in story order is the one worth reporting.
                raise fatal[0]

            arts = [cast("PageArt", result) for result in results]
            pdf_bytes = await asyncio.to_thread(build_book_pdf, record.about, arts)
            await asyncio.to_thread(self._book_store.save_pdf, book_id, pdf_bytes)
            await asyncio.to_thread(
                self._book_store.save_status,
                book_id,
                BookStatus.COMPLETE,
                self._page_view(book_id, progress, titles),
            )
            logger.info(
                "book.complete user=%s book=%s pages=%d pdf_bytes=%d ms=%.0f outcome=complete",
                user,
                book_id,
                total,
                len(pdf_bytes),
                (monotonic() - started) * 1000,
            )
        except asyncio.CancelledError:
            # A cancelled task is still an orphaned book: the process may be
            # shutting down, or a caller may have cancelled the job. Record the
            # failure before propagating, so a poll never keeps saying
            # "rendering" for work that is no longer running.
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

    # --- one page ---------------------------------------------------------------

    def _page_call(
        self,
        references: Sequence[bytes],
        media_types: Sequence[str],
        prompt: str,
        settings: CartoonSettings,
    ) -> Callable[[], bytes]:
        """The blocking half of one page render (not yet on a thread)."""

        def work() -> bytes:
            return self._provider.generate(
                ImageRequest(
                    prompt=prompt,
                    # The child's double anchor on every single page, plus this
                    # page's own photographed cast — never the whole book cast.
                    reference_images=tuple(references),
                    reference_media_types=tuple(media_types),
                    output_format="png",
                    aspect_ratio=str(settings.aspect_ratio),
                    label="book-page",
                )
            ).data

        return work

    async def _on_worker_thread_with_slot(
        self, work: Callable[[], _T], *, on_slot: Callable[[], Awaitable[None]] | None = None
    ) -> _T:
        """Blocking `work` on a worker thread while this task holds a gate slot.

        The page timeout binds the slot wait and the work together. If the work
        outlives it, ownership of the slot transfers to the worker: it is still
        running (and possibly still billing), and only it knows when it ends.

        `on_slot` runs once the slot is in hand, before the billable work
        starts — the point at which the page is genuinely being drawn.
        """
        async with self._gate.lease(timeout=self._page_timeout) as slot:
            if on_slot is not None:
                await on_slot()
            hop = asyncio.create_task(asyncio.to_thread(work))
            try:
                return await asyncio.wait_for(asyncio.shield(hop), timeout=self._page_timeout)
            except TimeoutError:
                _hand_slot_to_worker(slot, hop)
                raise

    # --- helpers ----------------------------------------------------------------

    def _fail(
        self, book_id: str, message: str, *, user: str = "-", elapsed_ms: float = 0.0
    ) -> None:
        status = self._book_store.load_status(book_id) or {"pages": []}
        pages: list[dict[str, object]] = []
        for entry in cast("list[object]", status.get("pages", [])):
            if not isinstance(entry, dict):
                continue
            page_entry = dict(entry)
            if page_entry.get("status") != "done":
                # A page that was mid-flight when the book stopped is neither
                # done nor carrying bytes; report it as pending so no tile and
                # no url claims progress that never completed.
                page_entry["status"] = "pending"
                page_entry["url"] = None
            pages.append(page_entry)
        self._book_store.save_status(book_id, BookStatus.FAILED, pages, error=message)
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
        """The full page list for status, in story order, from live progress.

        Every page carries one of `pending`, `making` or `done`; only a done
        page has a url, because only a done page has bytes behind it.

        `titles` is computed once per book by `_render`, not here: this runs on
        every page transition, and re-rendering the catalogue each time was the
        reason a nineteen-page book formatted its titles hundreds of times.
        """
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
    """Wait until one billable page fits in this minute's allowance.

    The brake is an abuse guard, not a per-book budget: a book may be larger
    than a single minute's allowance, so a denied charge means *later*, not
    *never*. Retry at a small interval; the concurrency gate still bounds how
    many calls are actually in flight.
    """
    if charge is None:
        return
    while not charge():
        await asyncio.sleep(_PAGE_CHARGE_RETRY_SECONDS)


def _references(
    draft: Draft,
    page: BookPage,
    characters: Mapping[str, CharacterReference],
) -> tuple[list[bytes], list[str]]:
    """Exactly the reference images for one page, in the order the prompt names.

    The child's two anchors first (always), then each of the page's own cast
    members that has a photo — portrait then photograph — in the page's cast
    order. A page never receives the book's whole cast, and a cast member
    without a photo contributes no image (the cast sheet's prose defines them).
    """
    references = [draft.cartoon_image, draft.photo_data]
    media_types = [draft.cartoon_media_type, draft.photo_media_type]
    for name in page.cast:
        character = characters.get(name)
        if character is None:
            continue
        references.extend([character.portrait, character.photo])
        media_types.extend([character.portrait_media_type, character.photo_media_type])
    return references, media_types


def _hand_slot_to_worker(slot: Slot, hop: asyncio.Task[object]) -> None:
    """Give the slot to work that outlives the awaited deadline.

    The page request may have timed out, but its worker thread is still
    generating (and possibly billing). The gate must show busy until that
    worker really finishes, so the worker releases the slot when it is done.
    """
    slot.transfer()
    hop.add_done_callback(lambda _finished: slot.release())
