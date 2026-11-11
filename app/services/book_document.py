"""A book's finished artefacts: the PDF, its status view, and its fee.

Separate from the render loop on purpose. These three things are the document
rules — what bytes make the PDF, what the poll document says about each page,
and when the one document fee is taken — and none of them needs to know about
page scheduling, slot handoff or provider calls. Keeping them here means the
money and the document can be read, and tested, without following a render.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from functools import partial

from app.models.book import AboutPhotoChoice, BookPage
from app.models.story import COVER_NUMBER
from app.services.book_store import BookRecord, BookStatus, BookStore
from app.services.pdf import PageArt, build_book_pdf
from app.services.wallet import Wallet

__all__ = ["COVER_KEY", "COVER_NUMBER", "BookDocument"]

logger = logging.getLogger(__name__)

#: The prompt record uses one pseudo-page for the cover.
COVER_KEY = "cover"

#: The phase the poll document reports while the PDF is being assembled.
_PHASE_COMPLETE = "complete"


class BookDocument:
    """Builds the PDF and publishes the document state a reader polls."""

    def __init__(self, *, book_store: BookStore, wallet: Wallet | None = None) -> None:
        self._book_store = book_store
        # None when payments are off: the document is still assembled and the
        # reader still sees it; nothing is charged.
        self._wallet = wallet

    def assemble(
        self,
        record: BookRecord,
        rendered_pages: Sequence[BookPage],
        progress: Mapping[int, str],
        rendered: Mapping[int, BookPage] | None = None,
        *,
        subtitle: str = "",
    ) -> int:
        """Build the PDF from every page on disk, then publish complete.

        `subtitle` is the line under the cover title ("starring Ava"), from
        the book's edition. `rendered` carries the page text this job just
        formatted, so a full render does not format every page a third time
        just to assemble them. Every picture is read back from the store as
        its page is drawn, never held for the whole book.

        A page with no picture on disk is not an error: it becomes a marked
        page in the book, so one picture that could not be drawn does not
        throw away the pages that could (B2). "Finish this book" draws only
        what is missing.
        """
        fresh = rendered or {}
        arts = [
            PageArt(
                image=partial(self._page_image, record.book_id, page.number),
                page=fresh.get(page.number, page),
            )
            for page in rendered_pages
        ]
        cover = self._book_store.read_cover(record.book_id)
        # The child's approved portrait is framed on the About page.
        portrait = self._book_store.read_portrait(record.book_id)
        # The original photo is read only when the About page will show it.
        photo = (
            self._book_store.read_source_photo(record.book_id)
            if record.about.about_photo != AboutPhotoChoice.CARTOON
            else None
        )
        pdf_bytes = build_book_pdf(
            record.title,
            record.about,
            arts,
            cover,
            subtitle=subtitle,
            portrait=portrait,
            photo=photo,
        )
        self._book_store.save_pdf(record.book_id, pdf_bytes)
        # The document exists now, so this is the moment its fee belongs to.
        # Charging is idempotent per book id: an edit that rebuilds the PDF
        # does not charge for the same document twice.
        self._charge(record)
        self._book_store.save_status(
            record.book_id,
            BookStatus.COMPLETE,
            self.page_view(record.book_id, progress, rendered_pages),
            title=record.title,
            extra={
                "phase": _PHASE_COMPLETE,
                "total_pages": len(rendered_pages) + 1,
                # Nothing is being drawn any more, and the stage says so rather
                # than leaving the last one ("reviewing page 12") behind (T12).
                "stage": _PHASE_COMPLETE,
                "stage_page": None,
                "stage_attempt": 1,
            },
        )
        return len(pdf_bytes)

    def _page_image(self, book_id: str, page_number: int) -> bytes | None:
        """The page's picture, or None when it was never drawn (B2)."""
        return self._book_store.read_page(book_id, page_number)

    def stored_progress(self, book_id: str) -> dict[int, str]:
        """Which pages the stored status already calls done."""
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

    def page_view(
        self,
        book_id: str,
        progress: Mapping[int, str],
        rendered_pages: Sequence[BookPage],
    ) -> list[dict[str, object]]:
        """The full page list for status, in story order, from live progress.

        The cover comes first and is counted like any other page: the book is
        the cover plus its story pages, and the reader should see that.

        No story text here: the document is rewritten on every stage change and
        polled every 1.5 s, so it carries only what changes — the page's number,
        title, status, picture URLs and version (S4). The words live in the
        story's pages and are rendered into the book page itself.
        """
        cover_state = progress.get(COVER_NUMBER, "pending")
        cover_done = cover_state == "done"
        view: list[dict[str, object]] = [
            {
                "number": COVER_NUMBER,
                "key": COVER_KEY,
                "title": "Cover",
                "url": self._book_store.cover_url(book_id) if cover_done else None,
                "status": cover_state,
            }
        ]
        for page in rendered_pages:
            state = progress.get(page.number, "pending")
            finished = state == "done"
            view.append(
                {
                    "number": page.number,
                    "key": page.key,
                    "title": page.scene.title,
                    "url": self._book_store.page_url(book_id, page.number)
                    if finished
                    else None,
                    "status": state,
                }
            )
        return view

    def _charge(self, record: BookRecord) -> None:
        """Take the one document fee, now that the PDF is on disk.

        `require_funds=False`: the start endpoint already refused a book this
        account could not afford, so a balance that moved mid-render must not
        throw away a finished document. A ledger failure is logged, never
        allowed to turn a finished book into a failed one.
        """
        if self._wallet is None or not record.user_id:
            return
        try:
            self._wallet.charge_document(
                record.user_id,
                book_id=record.book_id,
                title=record.title,
                require_funds=False,
            )
        except Exception:
            logger.warning(
                "book.charge_failed user=%s book=%s",
                record.user_id,
                record.book_id,
                exc_info=True,
            )
