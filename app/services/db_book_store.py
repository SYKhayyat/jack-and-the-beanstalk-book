"""The book store: the one implementation of `BookStore` (`book_store.py`).

It runs against Postgres in production and against the local SQLite file
everywhere else, so `BookRunner` and the routes have exactly one store to be
written against, and a completed book survives the death of the process that
made it in every environment.

Implementation notes:

* **One transaction per operation.** Every method opens `engine.begin()` and
  commits or rolls back as a whole. Nothing here keeps a connection across
  an await, and nothing is ever shared between transactions, so the small
  pool is safe with `asyncio.to_thread` — that is the whole threading model.
* **Pages upsert.** Re-saving page 2 replaces page 2 rather than adding a
  second row for it; (book id, page number) is the key.
* **`status_payload` is the exact poll document.** A database-mode poll
  returns what a filesystem-mode poll returns, so no route branches on the
  store implementation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import Connection, delete, null, select, update
from sqlalchemy import insert as sa_insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from app.db import (
    BookCharacterRow,
    BookPageRow,
    BookRow,
    BookStoryCharacterRow,
    BookStoryPageRow,
    PictureVariantRow,
    StoryRow,
)
from app.models.book import AboutMe
from app.models.settings import CartoonSettings
from app.models.story import COVER_NUMBER, StoryEdition
from app.services.book_store import (
    BookRecord,
    BookStatus,
    BookStore,
    BookSummary,
    CharacterReference,
    page_totals,
)
from app.services.media import media_type_for
from app.services.pictures import VARIANT_MEDIA_TYPE, picture_set, slot_page
from app.services.template_store import write_pins

__all__ = ["COVER_PAGE", "DatabaseBookStore", "UnreviewedImages", "unreviewed_images"]

#: The cover's slot in a review report; story pages count from 1.
COVER_PAGE = 0

#: What `load_record` reads of a `books` row. Never the whole row: it carries
#: the portrait, source photo, cover and PDF, each read by its own method.
_RECORD_COLUMNS = (
    BookRow.id,
    BookRow.about,
    BookRow.generation_id,
    BookRow.created_at,
    BookRow.story_key,
    BookRow.title,
    BookRow.user_id,
    BookRow.request_fingerprint,
    BookRow.settings,
)


class UnreviewedImages(RuntimeError):
    """A book pinned to a template tried to complete with an unreviewed image."""


logger = logging.getLogger(__name__)


class DatabaseBookStore(BookStore):
    """The contract, implemented in Postgres (or SQLite, for tests)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        # Pages render concurrently (one worker thread each) and every prompt
        # append is a read-modify-write of the prompts column; without this
        # lock two pages landing together would lose one another's record.
        self._prompts_lock = threading.Lock()

    # --- ownership -------------------------------------------------------------

    def owner_of(self, book_id: str) -> str | None:
        with self._engine.connect() as conn:
            value = conn.execute(
                select(BookRow.user_id).where(BookRow.id == book_id)
            ).scalar_one_or_none()
        return value or None

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
        if not edition.page_ids:
            raise ValueError("a book is pinned to stored revisions; this edition has none")
        title = edition.title_for(about.prompt_name)
        with self._engine.begin() as conn:
            conn.execute(
                sa_insert(BookRow).values(
                    id=book_id,
                    user_id=user_id,
                    status=BookStatus.PENDING,
                    generation_id=generation_id,
                    request_fingerprint=fingerprint,
                    about=about.model_dump(mode="json"),
                    settings=(settings or CartoonSettings()).model_dump(mode="json"),
                    story_key=edition.key,
                    title=title,
                    template_version_id=edition.template_version_id,
                    # The file store has always written the pending document
                    # with the record; do the same here, or a poll landing
                    # before the runner's first status write reads NULL and
                    # answers "unknown book" for a book that already exists.
                    status_payload=self._status_payload(
                        book_id, BookStatus.PENDING, [], None, title=title
                    ),
                    created_at=_now(),
                    updated_at=_now(),
                )
            )
            # The pins are the template store's rule, written in this same
            # transaction so a book never exists without its edition.
            write_pins(conn, book_id, edition.page_ids, edition.character_ids)

    def load_record(self, book_id: str) -> BookRecord | None:
        with self._engine.connect() as conn:
            # Only the request's own columns: the row also holds the portrait,
            # the photo, the cover and the PDF (megabytes, P6), which every
            # page view and edit would otherwise drag out of the database.
            row = conn.execute(
                select(*_RECORD_COLUMNS).where(BookRow.id == book_id)
            ).one_or_none()
        if row is None:
            return None
        about = AboutMe.model_validate(row.about)
        return BookRecord(
            book_id=row.id,
            about=about,
            generation_id=row.generation_id,
            created_at=row.created_at.isoformat() if row.created_at else "",
            story_key=row.story_key,
            title=row.title,
            user_id=row.user_id,
            fingerprint=row.request_fingerprint,
            settings=CartoonSettings.model_validate(row.settings or {}),
        )

    def update_about(self, book_id: str, about: AboutMe, *, title: str) -> None:
        with self._engine.begin() as conn:
            row = conn.execute(
                select(BookRow.status, BookRow.status_payload, BookRow.error).where(
                    BookRow.id == book_id
                )
            ).one_or_none()
            if row is None:
                raise KeyError(f"No record for book {book_id!r}.")
            payload = (
                dict(row.status_payload)
                if row.status_payload is not None
                else self._status_payload(book_id, row.status, [], row.error)
            )
            payload["title"] = title
            conn.execute(
                update(BookRow)
                .where(BookRow.id == book_id)
                .values(
                    about=about.model_dump(mode="json"),
                    title=title,
                    status_payload=payload,
                    updated_at=_now(),
                )
            )

    def list_for_owner(self, user_id: str, *, limit: int = 50) -> list[BookSummary]:
        """One indexed-ish query for the signed-in person's library."""
        if not user_id:
            return []
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(
                    BookRow.id,
                    BookRow.story_key,
                    BookRow.title,
                    BookRow.status,
                    BookRow.status_payload,
                    BookRow.created_at,
                    BookRow.updated_at,
                    BookRow.cover_sha256,
                    BookRow.about,
                    StoryRow.cover_subtitle,
                )
                .outerjoin(StoryRow, StoryRow.key == BookRow.story_key)
                .where(BookRow.user_id == user_id)
                .order_by(BookRow.created_at.desc())
                .limit(limit)
            ).all()
        summaries: list[BookSummary] = []
        for row in rows:
            payload = dict(row.status_payload) if row.status_payload is not None else {}
            pages = payload.get("pages", [])
            raw_pages = pages if isinstance(pages, list) else []
            page_list = [page for page in raw_pages if isinstance(page, dict)]
            has_cover = row.cover_sha256 is not None
            cover_url = self.cover_url(row.id) if has_cover else None
            page_count, done = page_totals(page_list, cover_done=has_cover)
            summaries.append(
                BookSummary(
                    book_id=row.id,
                    story_key=row.story_key,
                    title=row.title,
                    status=row.status,
                    created_at=row.created_at.isoformat() if row.created_at else "",
                    updated_at=row.updated_at.isoformat() if row.updated_at else "",
                    page_count=page_count,
                    pages_done=done,
                    cover_url=cover_url,
                    pdf_url=self.pdf_url(row.id)
                    if row.status == BookStatus.COMPLETE
                    else None,
                    subtitle=_subtitle(row.cover_subtitle, row.about),
                    cover_sha256=row.cover_sha256,
                )
            )
        return summaries

    def find_started(self, *, fingerprint: str, since: datetime) -> str | None:
        """One indexed query, newest match first.

        `created_at` is stored naive UTC (SQLite and Postgres agree on that),
        so the aware window boundary is converted rather than compared
        directly.
        """
        cutoff = since.astimezone(UTC).replace(tzinfo=None)
        with self._engine.connect() as conn:
            return conn.execute(
                select(BookRow.id)
                .where(
                    BookRow.request_fingerprint == fingerprint,
                    BookRow.created_at >= cutoff,
                    BookRow.status.in_(
                        (BookStatus.PENDING, BookStatus.RENDERING, BookStatus.COMPLETE)
                    ),
                )
                .order_by(BookRow.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()

    # --- status ----------------------------------------------------------------

    def _status_payload(
        self,
        book_id: str,
        status: str,
        pages: list[dict[str, object]],
        error: str | None,
        *,
        title: str | None = None,
        cover_url: str | None = None,
        extra: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """The exact document the poll returns, in either store's shape."""
        payload: dict[str, object] = {
            "book_id": book_id,
            "status": status,
            "pages": pages,
            "error": error,
            "pdf_url": self.pdf_url(book_id) if status == BookStatus.COMPLETE else None,
            "cover_url": cover_url,
            "title": title,
            "updated_at": _now().isoformat(),
        }
        if extra:
            payload.update(extra)
        return payload

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
        with self._engine.begin() as conn:
            row = conn.execute(
                select(BookRow.status_payload, BookRow.cover_sha256).where(
                    BookRow.id == book_id
                )
            ).one_or_none()
            previous = (
                dict(row.status_payload)
                if row is not None and row.status_payload is not None
                else {}
            )
            if title is None:
                title = cast("str | None", previous.get("title"))
            if cover_url is None and row is not None and row.cover_sha256 is not None:
                cover_url = self.cover_url(book_id)
            if status == BookStatus.COMPLETE:
                missing = _unreviewed(conn, book_id)
                if missing:
                    raise UnreviewedImages(
                        f"book {book_id} cannot complete: no review for page(s) {missing}"
                    )
            payload = self._status_payload(
                book_id,
                status,
                pages,
                error,
                title=title,
                cover_url=cover_url,
                extra=extra,
            )
            conn.execute(
                update(BookRow)
                .where(BookRow.id == book_id)
                .values(
                    status=status,
                    error=error,
                    status_payload=payload,
                    updated_at=_now(),
                )
            )

    def load_status(self, book_id: str) -> dict[str, object] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(BookRow.status, BookRow.status_payload, BookRow.error).where(
                    BookRow.id == book_id
                )
            ).one_or_none()
            if row is None:
                return None
            if row.status_payload is None:
                # A row that predates this fix (or an insert interrupted before
                # the document was written) still answers in the poll's shape
                # rather than pretending the book does not exist.
                return self._status_payload(book_id, row.status, [], row.error)
            return _with_picture_records(conn, book_id, dict(row.status_payload))

    def load_status_and_owner(
        self, book_id: str
    ) -> tuple[dict[str, object] | None, str | None]:
        """The poll document and its owner in one query.

        Both live on the same `books` row, so a poll costs one round trip
        instead of the three it used to: status for authorisation, owner for
        authorisation, status again for the body.
        """
        with self._engine.connect() as conn:
            row = conn.execute(
                select(
                    BookRow.status, BookRow.status_payload, BookRow.error, BookRow.user_id
                ).where(BookRow.id == book_id)
            ).one_or_none()
            if row is None:
                return None, None
            payload = (
                _with_picture_records(conn, book_id, dict(row.status_payload))
                if row.status_payload is not None
                else self._status_payload(book_id, row.status, [], row.error)
            )
        return payload, row.user_id or None

    # --- artefacts -------------------------------------------------------------

    def save_portrait(self, book_id: str, data: bytes) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(BookRow)
                .where(BookRow.id == book_id)
                .values(portrait=data, portrait_sha256=_sha(data), updated_at=_now())
            )

    def read_portrait(self, book_id: str) -> bytes | None:
        with self._engine.connect() as conn:
            return conn.execute(
                select(BookRow.portrait).where(BookRow.id == book_id)
            ).scalar_one_or_none()

    def save_source_photo(self, book_id: str, data: bytes) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(BookRow)
                .where(BookRow.id == book_id)
                .values(source_photo=data, updated_at=_now())
            )

    def update_identity(
        self,
        book_id: str,
        *,
        portrait: bytes,
        photo: bytes,
        settings: CartoonSettings,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(BookRow)
                .where(BookRow.id == book_id)
                .values(
                    portrait=portrait,
                    portrait_sha256=_sha(portrait),
                    source_photo=photo,
                    settings=settings.model_dump(mode="json"),
                    updated_at=_now(),
                )
            )

    def read_source_photo(self, book_id: str) -> bytes | None:
        with self._engine.connect() as conn:
            return conn.execute(
                select(BookRow.source_photo).where(BookRow.id == book_id)
            ).scalar_one_or_none()

    def save_cover(self, book_id: str, data: bytes) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(BookRow)
                .where(BookRow.id == book_id)
                .values(
                    cover=data, cover_sha256=_sha(data), cover_review=null(), updated_at=_now()
                )
            )

    def record_cover_review(
        self,
        book_id: str,
        review: list[dict[str, Any]],
        *,
        attempt_count: int = 1,
        chosen_attempt: int = 1,
        best_effort: bool = False,
    ) -> None:
        """Store the cover's verdicts and which attempt was kept (T9).

        A new cover clears them, exactly as a re-saved page does: a redraw must
        never ride on its predecessor's verdict.
        """
        if not review:
            raise ValueError("a review records at least one verdict")
        if not 1 <= chosen_attempt <= attempt_count:
            raise ValueError("chosen_attempt must be one of the attempts made")
        with self._engine.begin() as conn:
            result = conn.execute(
                update(BookRow)
                .where(BookRow.id == book_id, BookRow.cover.is_not(None))
                .values(
                    cover_review=review,
                    cover_attempt_count=attempt_count,
                    cover_chosen_attempt=chosen_attempt,
                    cover_best_effort=best_effort,
                )
            )
            if result.rowcount != 1:
                raise LookupError(f"book {book_id} has no cover to review")

    def read_cover(self, book_id: str) -> bytes | None:
        with self._engine.connect() as conn:
            return conn.execute(
                select(BookRow.cover).where(BookRow.id == book_id)
            ).scalar_one_or_none()

    def save_characters(self, book_id: str, characters: dict[str, CharacterReference]) -> None:
        with self._engine.begin() as conn:
            conn.execute(delete(BookCharacterRow).where(BookCharacterRow.book_id == book_id))
            for key, ref in characters.items():
                conn.execute(
                    sa_insert(BookCharacterRow).values(
                        book_id=book_id,
                        cast_key=key,
                        portrait=ref.portrait,
                        portrait_media_type=ref.portrait_media_type,
                        photo=ref.photo,
                        photo_media_type=ref.photo_media_type,
                        **_character_record(ref),
                    )
                )

    def save_character(self, book_id: str, key: str, ref: CharacterReference) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                delete(BookCharacterRow).where(
                    BookCharacterRow.book_id == book_id,
                    BookCharacterRow.cast_key == key,
                )
            )
            conn.execute(
                sa_insert(BookCharacterRow).values(
                    book_id=book_id,
                    cast_key=key,
                    portrait=ref.portrait,
                    portrait_media_type=ref.portrait_media_type,
                    photo=ref.photo,
                    photo_media_type=ref.photo_media_type,
                    **_character_record(ref),
                )
            )

    def load_characters(self, book_id: str) -> dict[str, CharacterReference]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(BookCharacterRow).where(BookCharacterRow.book_id == book_id)
            ).all()
        return {
            row.cast_key: CharacterReference(
                portrait=row.portrait,
                portrait_media_type=row.portrait_media_type,
                photo=row.photo,
                photo_media_type=row.photo_media_type,
                generation_id=row.generation_id or "",
                settings=(
                    CartoonSettings.model_validate(row.settings)
                    if row.settings is not None
                    else None
                ),
            )
            for row in rows
        }

    def save_page(self, book_id: str, page_number: int, data: bytes) -> None:
        """Persist one page's art. Raises on a bad page number, never on disk."""
        if page_number < 1:
            raise ValueError("page_number counts from 1")
        with self._engine.begin() as conn:
            conn.execute(
                delete(BookPageRow).where(
                    BookPageRow.book_id == book_id, BookPageRow.page_number == page_number
                )
            )
            conn.execute(
                sa_insert(BookPageRow).values(
                    book_id=book_id,
                    page_number=page_number,
                    image=data,
                    media_type=media_type_for(data),
                    image_sha256=_sha(data),
                )
            )

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
        """Store a page's verdicts and which attempt was kept (T9).

        Re-saving the page's image (`save_page`) clears all of this, so a
        redrawn page can never ride on its predecessor's verdict.
        """
        if not review:
            raise ValueError("a review records at least one verdict")
        if not 1 <= chosen_attempt <= attempt_count:
            raise ValueError("chosen_attempt must be one of the attempts made")
        with self._engine.begin() as conn:
            result = conn.execute(
                update(BookPageRow)
                .where(BookPageRow.book_id == book_id, BookPageRow.page_number == page_number)
                .values(
                    review=review,
                    attempt_count=attempt_count,
                    chosen_attempt=chosen_attempt,
                    best_effort=best_effort,
                )
            )
            if result.rowcount != 1:
                raise LookupError(f"book {book_id} has no page {page_number} to review")

    def read_page(self, book_id: str, page_number: int) -> bytes | None:
        if page_number < 1:
            return None
        with self._engine.connect() as conn:
            return conn.execute(
                select(BookPageRow.image).where(
                    BookPageRow.book_id == book_id, BookPageRow.page_number == page_number
                )
            ).scalar_one_or_none()

    def read_page_etag(self, book_id: str, page_number: int) -> str | None:
        """The page's stored hash, read without its image (T13).

        The hash is written with the image (`save_page`), so this is the same
        number the browser was told last time. A page whose image predates the
        column returns None and the caller hashes what it reads instead.
        """
        if page_number < 1:
            return None
        with self._engine.connect() as conn:
            return conn.execute(
                select(BookPageRow.image_sha256).where(
                    BookPageRow.book_id == book_id, BookPageRow.page_number == page_number
                )
            ).scalar_one_or_none()

    def picture_sha(self, book_id: str, slot: str) -> str | None:
        with self._engine.connect() as conn:
            if slot in ("cover", "portrait"):
                column = BookRow.cover_sha256 if slot == "cover" else BookRow.portrait_sha256
                return conn.execute(
                    select(column).where(BookRow.id == book_id)
                ).scalar_one_or_none()
        number = slot_page(slot)
        return self.read_page_etag(book_id, number) if number is not None else None

    def read_variant(
        self, book_id: str, slot: str, width: int, source_sha256: str
    ) -> bytes | None:
        with self._engine.connect() as conn:
            return conn.execute(
                select(PictureVariantRow.image).where(
                    PictureVariantRow.book_id == book_id,
                    PictureVariantRow.slot == slot,
                    PictureVariantRow.width == width,
                    PictureVariantRow.source_sha256 == source_sha256,
                )
            ).scalar_one_or_none()

    def save_variant(
        self, book_id: str, slot: str, width: int, source_sha256: str, data: bytes
    ) -> None:
        """Replace the slot's copy at this width.

        Two first views of the same page can race to make it; the loser's
        insert collides on the key, and its copy is the same picture, so the
        collision is the answer rather than an error.
        """
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    delete(PictureVariantRow).where(
                        PictureVariantRow.book_id == book_id,
                        PictureVariantRow.slot == slot,
                        PictureVariantRow.width == width,
                    )
                )
                conn.execute(
                    sa_insert(PictureVariantRow).values(
                        book_id=book_id,
                        slot=slot,
                        width=width,
                        source_sha256=source_sha256,
                        image=data,
                        media_type=VARIANT_MEDIA_TYPE,
                    )
                )
        except IntegrityError:
            logger.info("book.variant_race book=%s slot=%s width=%d", book_id, slot, width)

    def save_prompts(self, book_id: str, prompts: list[dict[str, str]]) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(BookRow)
                .where(BookRow.id == book_id)
                .values(prompts=prompts, updated_at=_now())
            )

    def append_prompt(self, book_id: str, prompt: dict[str, str]) -> None:
        """Add one page's prompt; atomic against other pages finishing at once."""
        with self._prompts_lock:
            self.save_prompts(book_id, (self.load_prompts(book_id) or []) + [prompt])

    def load_prompts(self, book_id: str) -> list[dict[str, str]] | None:
        with self._engine.connect() as conn:
            prompts = conn.execute(
                select(BookRow.prompts).where(BookRow.id == book_id)
            ).scalar_one_or_none()
        if prompts is None:
            return None
        return [dict(entry) for entry in cast("list[object]", prompts)]

    def read_prompts_json(self, book_id: str) -> bytes | None:
        prompts = self.load_prompts(book_id)
        if prompts is None:
            return None
        return json.dumps({"prompts": prompts}, indent=2).encode()

    def save_pdf(self, book_id: str, data: bytes) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(BookRow)
                .where(BookRow.id == book_id)
                .values(pdf=data, updated_at=_now())
            )

    def read_pdf(self, book_id: str) -> bytes | None:
        with self._engine.connect() as conn:
            return conn.execute(
                select(BookRow.pdf).where(BookRow.id == book_id)
            ).scalar_one_or_none()

    # --- lifecycle -------------------------------------------------------------

    def delete(self, book_id: str) -> None:
        with self._engine.begin() as conn:
            _delete_book(conn, book_id)

    def reap_incomplete(self) -> int:
        """Mark books still pending or rendering as failed (a restart orphaned them).

        The asyncio task died with the process; its stale status must not keep
        polling as "pending" forever. A PENDING row is as orphaned as a
        RENDERING one: `save_record` commits before the runner writes its first
        status, so a crash in that window would otherwise leave a book that
        never renders. The finished pages stay in their rows — exactly the
        file store's promise, kept pages visible.
        """
        message = (
            "The server restarted while this book was rendering. Start a new "
            "one — the finished pages shown here were kept."
        )
        reaped = 0
        with self._engine.begin() as conn:
            orphans = conn.execute(
                select(BookRow.id, BookRow.status_payload).where(
                    BookRow.status.in_((BookStatus.PENDING, BookStatus.RENDERING))
                )
            ).all()
            for book_id, payload in orphans:
                pages: list[dict[str, object]] = []
                if payload is not None:
                    raw = cast("dict[str, object]", payload).get("pages", [])
                    pages = [
                        entry for entry in cast("list[object]", raw) if isinstance(entry, dict)
                    ]
                stamp = _now()
                previous = cast("dict[str, object]", payload) if payload is not None else {}
                stamped = {
                    "book_id": book_id,
                    "status": BookStatus.FAILED,
                    "pages": pages,
                    "error": message,
                    "pdf_url": None,
                    "cover_url": previous.get("cover_url"),
                    "title": previous.get("title"),
                    "updated_at": stamp.isoformat(),
                }
                conn.execute(
                    update(BookRow)
                    .where(BookRow.id == book_id)
                    .values(
                        status=BookStatus.FAILED,
                        error=stamped["error"],
                        status_payload=stamped,
                        updated_at=stamp,
                    )
                )
                reaped += 1
                logger.warning("book.reaped id=%s (server restart)", book_id)
        return reaped

    def expire(self, *, older_than_seconds: float) -> int:
        """Delete finished books older than the window; see `BookStore.expire`.

        Age is `updated_at` — refreshed when the book completes and on every
        edit — not `created_at`. A week-old book edited today has just been
        used and must not be swept.

        Page rows go first: they carry the foreign key, and this delete is not
        declared `ON DELETE CASCADE` in the schema. All of it is one
        transaction, so a poll never sees a book whose pages are gone.
        """
        if older_than_seconds <= 0:
            return 0
        cutoff = _now() - timedelta(seconds=older_than_seconds)
        with self._engine.begin() as conn:
            stale = (
                conn.execute(
                    select(BookRow.id).where(
                        BookRow.updated_at < cutoff,
                        BookRow.status.in_((BookStatus.COMPLETE, BookStatus.FAILED)),
                    )
                )
                .scalars()
                .all()
            )
            for book_id in stale:
                _delete_book(conn, book_id)
                logger.info("book.expired id=%s", book_id)
        return len(stale)


def _with_picture_records(
    conn: Connection, book_id: str, payload: dict[str, object]
) -> dict[str, object]:
    """Stamp each finished picture in the poll document from its own row.

    Two things live on the page and book rows rather than in the status
    document, which is rewritten on every progress tick:

    * the quality loop's attempt record — the badge a visitor sees ("try 2 of
      3") is the number the loop stored, never a copy a later status write
      could drop. Pictures with no record carry no attempt fields at all;
    * the picture's hash, as its sized and versioned URLs (`src`, `srcset`,
      `thumb`; see `app.services.pictures`) — so a redrawn page is announced
      under a new address the moment its row changes.
    """
    pages = payload.get("pages")
    if not isinstance(pages, list) or not pages:
        return payload
    records: dict[int, tuple[int, int | None, bool, str | None]] = {
        row.page_number: (
            row.attempt_count,
            row.chosen_attempt,
            row.best_effort,
            row.image_sha256,
        )
        for row in conn.execute(
            select(
                BookPageRow.page_number,
                BookPageRow.attempt_count,
                BookPageRow.chosen_attempt,
                BookPageRow.best_effort,
                BookPageRow.image_sha256,
            ).where(BookPageRow.book_id == book_id)
        )
    }
    cover = conn.execute(
        select(
            BookRow.cover_attempt_count,
            BookRow.cover_chosen_attempt,
            BookRow.cover_best_effort,
            BookRow.cover_sha256,
        ).where(BookRow.id == book_id)
    ).one_or_none()
    if cover is not None:
        records[COVER_NUMBER] = (
            cover.cover_attempt_count,
            cover.cover_chosen_attempt,
            cover.cover_best_effort,
            cover.cover_sha256,
        )
    stamped: list[object] = []
    for entry in pages:
        number = entry.get("number") if isinstance(entry, dict) else None
        record = records.get(number) if isinstance(number, int) else None
        if record is None or entry.get("status") != "done":
            stamped.append(entry)
            continue
        count, chosen, best_effort, sha = record
        fields: dict[str, object] = {}
        url = entry.get("url")
        if isinstance(url, str) and url:
            fields.update(picture_set(url, sha).as_dict())
        if count >= 1:
            fields.update(
                attempts=count,
                chosen_attempt=chosen or count,
                best_effort=bool(best_effort),
            )
        stamped.append({**entry, **fields})
    return {**payload, "pages": stamped}


def _delete_book(conn: Connection, book_id: str) -> None:
    """A book and everything that is only its own. The story revisions stay."""
    conn.execute(delete(BookCharacterRow).where(BookCharacterRow.book_id == book_id))
    conn.execute(delete(PictureVariantRow).where(PictureVariantRow.book_id == book_id))
    conn.execute(delete(BookPageRow).where(BookPageRow.book_id == book_id))
    conn.execute(delete(BookStoryPageRow).where(BookStoryPageRow.book_id == book_id))
    conn.execute(delete(BookStoryCharacterRow).where(BookStoryCharacterRow.book_id == book_id))
    conn.execute(delete(BookRow).where(BookRow.id == book_id))


def _unreviewed(conn: Connection, book_id: str) -> list[int]:
    """Images of a book that have no review; 0 is the cover.

    Every book made since every picture is reviewed must have a verdict on
    each one. Books made before that (`review_required` false) are exempt:
    nothing looked at their pictures and nothing ever will.
    """
    book = conn.execute(
        select(
            BookRow.review_required, BookRow.cover.is_not(None), BookRow.cover_review
        ).where(BookRow.id == book_id)
    ).one_or_none()
    if book is None or not book[0]:
        return []
    missing: list[int] = []
    if book[1] and not book[2]:
        missing.append(COVER_PAGE)
    pages = conn.execute(
        select(BookPageRow.page_number, BookPageRow.review).where(
            BookPageRow.book_id == book_id
        )
    ).all()
    missing.extend(int(row.page_number) for row in pages if not row.review)
    return sorted(missing)


def unreviewed_images(engine: Engine) -> list[tuple[str, int]]:
    """Every (book, page) in a completed book whose image was never reviewed.

    T4.5's proof query: on a consistent database this is empty. Page 0 means
    the cover. One query per kind, so it stays cheap enough for a health check.
    """
    with engine.connect() as conn:
        must_be_reviewed = (
            BookRow.review_required.is_(True),
            BookRow.status == BookStatus.COMPLETE,
        )
        covers = conn.execute(
            select(BookRow.id).where(
                *must_be_reviewed,
                BookRow.cover.is_not(None),
                BookRow.cover_review.is_(None),
            )
        ).scalars()
        found = [(str(book_id), COVER_PAGE) for book_id in covers]
        pages = conn.execute(
            select(BookPageRow.book_id, BookPageRow.page_number)
            .join(BookRow, BookRow.id == BookPageRow.book_id)
            .where(*must_be_reviewed, BookPageRow.review.is_(None))
        ).all()
        found.extend((str(book_id), int(number)) for book_id, number in pages)
    return sorted(found)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _subtitle(template: str | None, about: Mapping[str, Any] | None) -> str:
    """The story's cover subtitle for this book's star ("starring Ava")."""
    if not template or about is None:
        return ""
    return template.format(name=AboutMe.model_validate(about).prompt_name)


def _character_record(ref: CharacterReference) -> dict[str, Any]:
    """The non-image half of a cast member's row: where it came from, its look."""
    return {
        "generation_id": ref.generation_id or None,
        "settings": ref.settings.model_dump(mode="json") if ref.settings is not None else None,
    }
