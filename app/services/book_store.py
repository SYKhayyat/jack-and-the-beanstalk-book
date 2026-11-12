"""Book persistence, behind one interface.

`BookStore` is the contract; the one implementation is `DatabaseBookStore`
(`db_book_store.py`), against Postgres in production and against a local SQLite
file everywhere else. There is no second store: a book, its pages, its portrait
and its PDF live in the database, so a book survives the death of the process
that made it in every environment.

The filesystem store this module used to carry was removed on purpose (T3):
two implementations meant two sets of answers to the same question, and a
deployment could drift onto the wrong one without any test noticing.

Ownership: a book made while signed in with Google is filed under that
person's `sub`. `owner_of` answers the sign-in question the media routes ask
— another account fetching the id gets a 404, not a 200 and not a redirect.
An *unowned* book (local dev, a password-only deployment) stays open to
whoever has a session, which is one ("us") by construction.

The filesystem implementation rewrites the status file after every page
**atomically** (temporary file + ``os.replace``): a poll racing a write reads
either the old or the new file, never a torn one, and a write interrupted by
a crash cannot leave a finished book unreadable. The database implementation
gets the same property from its transactions. On Render without a database
the disk is ephemeral and a restart loses books — `resume_incomplete` picks the
ones that were mid-render up again, or marks them failed so they do not poll as
"pending" forever.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.models.book import AboutMe
from app.models.settings import CartoonSettings
from app.models.story import StoryEdition
from app.services.pictures import slot_page

__all__ = [
    "INTERRUPTED_MESSAGE",
    "BookRecord",
    "BookStatus",
    "BookStore",
    "BookSummary",
    "CharacterReference",
    "ResumedBook",
    "page_totals",
]

logger = logging.getLogger(__name__)


#: What the owner of a book a restart interrupted, and could not resume, reads.
INTERRUPTED_MESSAGE = (
    "The server restarted while this book was rendering. The finished pages "
    "shown here were kept: press \u201cFinish this book\u201d to draw the rest, "
    "without paying for these again."
)


class BookStatus:
    """The states a book job moves through. Kept as strings in storage."""

    PENDING = "pending"
    RENDERING = "rendering"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CharacterReference:
    """A cast member the uploader gave a face to.

    Two images, mirroring the child's double anchor: the approved cartoon
    portrait (style-matched to the book) and the original photograph behind it.
    """

    portrait: bytes
    portrait_media_type: str
    photo: bytes
    photo_media_type: str
    #: The portrait draft the face came from (a `portrait_attempts` id), so the
    #: operator can follow it back to every attempt and chip. "" when unknown.
    generation_id: str = ""
    #: The look this member's portrait was drawn with — their own gender, age
    #: and style, not the star's. None for rows saved before it was kept.
    settings: CartoonSettings | None = None


@dataclass(frozen=True, slots=True)
class BookSummary:
    """One card in the library: enough to list and link, no blobs."""

    book_id: str
    story_key: str
    title: str
    status: str
    created_at: str
    updated_at: str
    page_count: int
    pages_done: int
    cover_url: str | None
    pdf_url: str | None
    #: The line under the title for a tale that names its star there
    #: ("starring Ava"); "" when the title already carries the name.
    subtitle: str = ""
    #: The cover's content hash, for its sized and versioned URLs.
    cover_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class ResumedBook:
    """A book a restart interrupted that is to be rendered again."""

    book_id: str
    #: When the interrupted job started (naive UTC), so the pictures it paid
    #: for and lost can be counted.
    job_started_at: datetime
    #: Which resume this is: 1 for the first.
    resume: int


@dataclass(frozen=True, slots=True)
class BookRecord:
    """The stored request for one book, reloaded by the runner."""

    book_id: str
    about: AboutMe
    generation_id: str
    created_at: str
    #: Which story the book is; its pinned edition is read by book id.
    story_key: str
    #: The resolved cover title ("Noa and the Wolf").
    title: str
    #: The Google `sub` of the person it belongs to, or None for an unowned
    #: book (local development, password-only deployments).
    user_id: str | None = None
    #: A hash of the request that started this book, so an identical retry can
    #: find it instead of billing a second one. None on books written before
    #: this field existed.
    fingerprint: str | None = None
    #: The settings the portrait and every page were made with. Persisted so a
    #: page (or the cover) can be regenerated long after the draft expires.
    settings: CartoonSettings = field(default_factory=CartoonSettings)


class BookStore:
    """The persistence contract for picture books.

    Every method is either a read or an atomic write; nothing here knows
    about HTTP, sessions, or the image provider.
    """

    def owner_of(self, book_id: str) -> str | None:
        """The `sub` the book belongs to, or None when nobody owns it."""
        raise NotImplementedError

    # --- the request -----------------------------------------------------------

    def save_record(
        self,
        *,
        book_id: str,
        user_id: str | None,
        about: AboutMe,
        generation_id: str,
        edition: StoryEdition,
        fingerprint: str | None = None,
        settings: CartoonSettings | None = None,
    ) -> None:
        """Persist the book's request and pin the edition it is made from.

        Written once, before rendering starts, in one transaction: the book
        row, its title, its template version (when the edition has one) and
        the exact page and bible revisions. A book never exists unpinned.
        """
        raise NotImplementedError

    def update_about(self, book_id: str, about: AboutMe, *, title: str) -> None:
        """Replace the About fields and the title they name (PDF text only)."""
        raise NotImplementedError

    def list_for_owner(self, user_id: str, *, limit: int = 50) -> list[BookSummary]:
        """A signed-in person's books, newest first.

        An empty `user_id` is password/open mode, where there is no person to
        own a library; it returns nothing by design.
        """
        raise NotImplementedError

    def find_started(self, *, fingerprint: str, since: datetime) -> str | None:
        """The id of a book already started by this exact request, or None.

        This is what makes `POST /api/book/start` idempotent. One book is
        twenty billable images, so a double-submitted form, a second tab or a
        client retrying after a dropped response must find the book that is
        already rendering rather than start a second one.

        A match is a book whose fingerprint equals `fingerprint`, that started
        at or after `since`, and that is still worth answering with: pending,
        rendering or complete. A *failed* book is deliberately not a match —
        after a failure the visitor asking again is asking for a new attempt,
        and refusing it would leave them stuck with a dead book.
        """
        raise NotImplementedError

    def load_record(self, book_id: str) -> BookRecord | None:
        """Reload a book's request, or None if it is gone or malformed."""
        raise NotImplementedError

    # --- status ----------------------------------------------------------------

    def save_status(
        self,
        book_id: str,
        status: str,
        pages: list[dict[str, object]],
        error: str | None = None,
        *,
        title: str | None = None,
        cover_url: str | None = None,
        extra: Mapping[str, object] | None = None,
    ) -> None:
        """Persist job status. Called after every page so clients can poll it.

        `extra` carries the fields the poll document needs beyond the status
        itself — the phase the job is in ("pages", "assembling", "complete")
        and the total page count. They live in the document rather than in
        the status string so that a client can tell "the last picture is done
        but the PDF is still being put together" from "the book is finished".
        """
        raise NotImplementedError

    def load_status(self, book_id: str) -> dict[str, object] | None:
        """The payload the status route returns, or None when the book is gone."""
        raise NotImplementedError

    def load_status_and_owner(
        self, book_id: str
    ) -> tuple[dict[str, object] | None, str | None]:
        """The poll document and its owner together, in one read.

        Authorisation and payload used to be two (and, with the status re-read
        in the route, three) store calls per poll. A browser tab polls every
        1.5 seconds, so in database mode that was three Postgres round trips
        for a document the server had already fetched.

        Returns:
            (payload, owner): payload is None when the book is gone; owner is
            None for an unowned book.
        """
        raise NotImplementedError

    # --- artefacts -------------------------------------------------------------

    def save_portrait(self, book_id: str, data: bytes) -> None:
        raise NotImplementedError

    def read_portrait(self, book_id: str) -> bytes | None:
        raise NotImplementedError

    def save_source_photo(self, book_id: str, data: bytes) -> None:
        """Keep the original photograph: the second identity anchor for edits."""
        raise NotImplementedError

    def update_identity(
        self,
        book_id: str,
        *,
        portrait: bytes,
        photo: bytes,
        settings: CartoonSettings,
    ) -> None:
        """Replace the star's portrait, photo and settings (a face edit)."""
        raise NotImplementedError

    def read_source_photo(self, book_id: str) -> bytes | None:
        raise NotImplementedError

    def save_cover(
        self,
        book_id: str,
        data: bytes,
        *,
        review: list[dict[str, Any]] | None = None,
        attempt_count: int = 1,
        chosen_attempt: int = 1,
        best_effort: bool = False,
    ) -> None:
        """Store the cover's art, and its verdict with it when one is given.

        The two land in one transaction. A cover saved without a verdict, as a
        redraw briefly is, has none: `record_cover_review` fills it later, and a
        book cannot complete until it does (B13).
        """
        raise NotImplementedError

    def read_cover(self, book_id: str) -> bytes | None:
        raise NotImplementedError

    def save_characters(self, book_id: str, characters: dict[str, CharacterReference]) -> None:
        """Replace the photographed cast for a book."""
        raise NotImplementedError

    def load_characters(self, book_id: str) -> dict[str, CharacterReference]:
        """Every cast member with a face, or an empty map."""
        raise NotImplementedError

    def save_character(self, book_id: str, key: str, ref: CharacterReference) -> None:
        """Add or replace one photographed cast member."""
        raise NotImplementedError

    def save_page(
        self,
        book_id: str,
        page_number: int,
        data: bytes,
        *,
        review: list[dict[str, Any]] | None = None,
        attempt_count: int = 1,
        chosen_attempt: int = 1,
        best_effort: bool = False,
    ) -> None:
        """Persist one page's art, and its verdict with it when one is given.

        The two land in one transaction, so there is never a moment where the
        picture exists and its verdict does not: a book refuses to complete
        with an unreviewed picture, and a write that failed between the two
        used to leave exactly that (B13). Raises on a bad page number, never on
        disk.
        """
        raise NotImplementedError

    def record_page_review(
        self,
        book_id: str,
        page_number: int,
        *,
        review: list[dict[str, Any]],
        attempt_count: int,
        chosen_attempt: int,
        best_effort: bool = False,
    ) -> None:
        """Store one page's verdicts and which attempt was kept (T9).

        A template-pinned book cannot complete until every one of its images
        has one, which is what stops an unreviewed picture reaching a reader.
        """
        raise NotImplementedError

    def record_cover_review(
        self,
        book_id: str,
        review: list[dict[str, Any]],
        *,
        attempt_count: int = 1,
        chosen_attempt: int = 1,
        best_effort: bool = False,
    ) -> None:
        """Store the cover's verdicts and which attempt was kept (T9)."""
        raise NotImplementedError

    def unreviewed_pages(self, book_id: str) -> set[int]:
        """Pictures of a book that have none, 0 for the cover (B13).

        A page counts only once its art exists; the answer is what makes a
        stuck book recoverable — Finish redraws them rather than leaving the
        book incomplete with no way to finish it.
        """
        raise NotImplementedError

    def read_page(self, book_id: str, page_number: int) -> bytes | None:
        raise NotImplementedError

    def read_page_etag(self, book_id: str, page_number: int) -> str | None:
        """A page's content hash, or None when the page has no art yet (T13).

        Read without the image, so a browser revalidating a page costs one small
        query instead of shipping a megabyte it already has.
        """
        raise NotImplementedError

    def saved_page_numbers(self, book_id: str) -> set[int]:
        """The pictures already finished: stored and reviewed (0 is the cover).

        The authority on what a resumed render may skip: a page's picture is
        saved, then its review recorded, then its status written, so a crash
        between those leaves finished art the status does not know about yet.
        Reading the rows (never the images) is what stops a resume paying to
        draw it again. A picture stored without its review is not finished —
        the book could not complete with it — and is drawn again.
        """
        raise NotImplementedError

    def begin_job(self, book_id: str) -> None:
        """Note that a render of this book starts now, as a new job.

        Starts the window a restart may resume it inside and clears its count
        of resumes: a job the owner asked for is a fresh start, a resume is not
        (and does not call this).
        """
        raise NotImplementedError

    # --- pictures on screen (P1, P2) -------------------------------------------
    # A "slot" names one of a book's pictures: "cover", "portrait" or "page-<n>"
    # (`app.services.pictures.page_slot`).

    def picture_sha(self, book_id: str, slot: str) -> str | None:
        """The stored hash of the picture in `slot`, read without its bytes.

        None when the picture does not exist yet (or the slot is not one).
        """
        raise NotImplementedError

    def read_picture(self, book_id: str, slot: str) -> bytes | None:
        """The full-size picture in `slot`, as stored."""
        if slot == "cover":
            return self.read_cover(book_id)
        if slot == "portrait":
            return self.read_portrait(book_id)
        number = slot_page(slot)
        return self.read_page(book_id, number) if number is not None else None

    def read_variant(
        self, book_id: str, slot: str, width: int, source_sha256: str
    ) -> bytes | None:
        """The screen-sized copy of `slot` at `width`, if one was made from
        exactly this picture; a copy of a since-redrawn picture is None."""
        raise NotImplementedError

    def save_variant(
        self, book_id: str, slot: str, width: int, source_sha256: str, data: bytes
    ) -> None:
        """Keep a screen-sized copy, replacing any older one for that slot and width."""
        raise NotImplementedError

    def save_prompts(self, book_id: str, prompts: list[dict[str, str]]) -> None:
        """The prompt per page: the record of what was actually sent."""
        raise NotImplementedError

    def append_prompt(self, book_id: str, prompt: dict[str, str]) -> None:
        """Add one page's prompt progressively, so a failed book keeps the
        record of the pages that were really sent."""
        raise NotImplementedError

    def load_prompts(self, book_id: str) -> list[dict[str, str]] | None:
        raise NotImplementedError

    def read_prompts_json(self, book_id: str) -> bytes | None:
        """The prompt record as it is served over ``/book-media/…/prompts.json``."""
        raise NotImplementedError

    def save_pdf(self, book_id: str, data: bytes) -> None:
        raise NotImplementedError

    def read_pdf(self, book_id: str) -> bytes | None:
        raise NotImplementedError

    # --- URLs (identical in every implementation) ------------------------------

    def pdf_url(self, book_id: str) -> str:
        return f"/book-media/{book_id}/pdf"

    def portrait_url(self, book_id: str) -> str:
        return f"/book-media/{book_id}/portrait"

    def cover_url(self, book_id: str) -> str:
        return f"/book-media/{book_id}/cover"

    def page_url(self, book_id: str, page_number: int) -> str:
        return f"/book-media/{book_id}/pages/{page_number}"

    # --- lifecycle -------------------------------------------------------------

    def delete(self, book_id: str) -> None:
        raise NotImplementedError

    def claim_job(self, book_id: str, owner: str, *, ttl_seconds: float) -> bool:
        """Take the book's job lease, if no live worker holds it.

        The one rule that keeps a book from being rendered twice: the claim is
        a conditional write in the store, not a flag in the process, so a second
        instance — or a restart — cannot start drawing the same pages. Returns
        whether this caller now holds the lease; a lease already held by the
        same `owner` is renewed rather than refused, so a resume can re-claim
        what the boot path reserved for it.
        """
        raise NotImplementedError

    def renew_job(self, book_id: str, owner: str, *, ttl_seconds: float) -> bool:
        """Extend this worker's lease. False when the book is no longer ours.

        The heartbeat's answer: a False means another worker took the book (or
        the lease ran out and the boot path resumed it), so the job must stop
        rather than keep paying for pictures nobody will use.
        """
        raise NotImplementedError

    def release_job(self, book_id: str, owner: str) -> None:
        """Give the lease up: the job finished, failed, or was cancelled."""
        raise NotImplementedError

    def resume_incomplete(
        self, *, owner: str, ttl_seconds: float, max_resumes: int, window_seconds: float
    ) -> list[ResumedBook]:
        """Pick up the books a restart interrupted, or fail them for good.

        A book is resumed when it was still pending or rendering, has its
        portrait (everything a render draws from), has not been resumed
        `max_resumes` times already, its job started within `window_seconds`,
        and its lease is free or expired — a live worker's book is left alone
        (B9). Its resume is counted, the lease is taken for `owner`, and it is
        returned for the caller to schedule; its finished pages will be
        skipped. Every other such book, whose worker is gone, is marked failed,
        its finished pages kept, and its owner told to finish it.

        Returns:
            The books to render again, in the order found.
        """
        raise NotImplementedError

    def expire(self, *, older_than_seconds: float) -> int:
        """Delete books that finished longer ago than the retention window.

        Books hold a portrait, nineteen page images and a PDF of a child —
        several megabytes each, and nothing a visitor asked to keep forever.
        Without this, a deployment's storage grows for as long as it runs and
        the pictures of children in it are retained indefinitely.

        A book that is still pending or rendering is never expired, whatever
        its age: it is live work, and retention is measured from completion.
        A directory with no readable record at all is removed once it is old
        enough, because nothing will ever read it again.

        Returns:
            How many books were removed.
        """
        raise NotImplementedError


def page_totals(pages: list[dict[str, object]], *, cover_done: bool) -> tuple[int, int]:
    """The library's page count and finished count, cover included.

    Status written before the cover was counted (it is page zero now) lists
    only the nineteen story pages; those documents are still readable, so the
    cover is added back here. Either way the reader is told "out of 20", which
    matches the progress line while the book is being drawn.
    """
    done = sum(1 for page in pages if page.get("status") == "done")
    if any(page.get("number") == 0 for page in pages):
        return len(pages), done
    return len(pages) + 1, done + (1 if cover_done else 0)
