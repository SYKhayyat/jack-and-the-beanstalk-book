"""The book store against `DATABASE_URL`.

Same contract as `FileBookStore` (`book_store.py`), so `BookRunner` and the
routes cannot tell the difference; what changes is the durability promise:
a completed book survives the death of the process that made it.

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

import json
import logging
import threading
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import delete, select, update
from sqlalchemy import insert as sa_insert
from sqlalchemy.engine import Engine

from app.db import BookPageRow, BookRow
from app.models.book import AboutMe
from app.services.book_store import BookRecord, BookStatus, BookStore

__all__ = ["DatabaseBookStore"]

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
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa_insert(BookRow).values(
                    id=book_id,
                    user_id=user_id,
                    status=BookStatus.PENDING,
                    generation_id=generation_id,
                    about=about.model_dump(mode="json"),
                    # The file store has always written the pending document
                    # with the record; do the same here, or a poll landing
                    # before the runner's first status write reads NULL and
                    # answers "unknown book" for a book that already exists.
                    status_payload=self._status_payload(book_id, BookStatus.PENDING, [], None),
                    created_at=_now(),
                    updated_at=_now(),
                )
            )

    def load_record(self, book_id: str) -> BookRecord | None:
        with self._engine.connect() as conn:
            row = conn.execute(select(BookRow).where(BookRow.id == book_id)).one_or_none()
        if row is None:
            return None
        about = AboutMe.model_validate(row.about)
        return BookRecord(
            book_id=row.id,
            about=about,
            generation_id=row.generation_id,
            created_at=row.created_at.isoformat() if row.created_at else "",
            user_id=row.user_id,
        )

    # --- status ----------------------------------------------------------------

    def _status_payload(
        self,
        book_id: str,
        status: str,
        pages: list[dict[str, object]],
        error: str | None,
    ) -> dict[str, object]:
        """The exact document the poll returns, in either store's shape."""
        return {
            "book_id": book_id,
            "status": status,
            "pages": pages,
            "error": error,
            "pdf_url": self.pdf_url(book_id) if status == BookStatus.COMPLETE else None,
            "updated_at": _now().isoformat(),
        }

    def save_status(
        self,
        book_id: str,
        status: str,
        pages: list[dict[str, object]],
        error: str | None = None,
    ) -> None:
        payload = self._status_payload(book_id, status, pages, error)
        with self._engine.begin() as conn:
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
        if row.status_payload is not None:
            return dict(row.status_payload)
        # A row that predates this fix (or an insert interrupted before the
        # document was written) still answers in the poll's shape rather than
        # pretending the book does not exist.
        return self._status_payload(book_id, row.status, [], row.error)

    # --- artefacts -------------------------------------------------------------

    def save_portrait(self, book_id: str, data: bytes) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(BookRow)
                .where(BookRow.id == book_id)
                .values(portrait=data, updated_at=_now())
            )

    def read_portrait(self, book_id: str) -> bytes | None:
        with self._engine.connect() as conn:
            return conn.execute(
                select(BookRow.portrait).where(BookRow.id == book_id)
            ).scalar_one_or_none()

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
                    media_type=_media_type(data),
                )
            )

    def read_page(self, book_id: str, page_number: int) -> bytes | None:
        if page_number < 1:
            return None
        with self._engine.connect() as conn:
            return conn.execute(
                select(BookPageRow.image).where(
                    BookPageRow.book_id == book_id, BookPageRow.page_number == page_number
                )
            ).scalar_one_or_none()

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
            conn.execute(delete(BookPageRow).where(BookPageRow.book_id == book_id))
            conn.execute(delete(BookRow).where(BookRow.id == book_id))

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
                stamped = {
                    "book_id": book_id,
                    "status": BookStatus.FAILED,
                    "pages": pages,
                    "error": message,
                    "pdf_url": None,
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


def _media_type(data: bytes) -> str:
    """Chosen from the bytes themselves, like the filesystem store does."""
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    return "image/png"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
