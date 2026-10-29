"""Book persistence, behind one interface.

`BookStore` is the contract every store in this app implements; the concrete
implementations are:

* `FileBookStore` — one directory per book under ``<storage>/books``:
    books/<book_id>/book.json      what the uploader asked for + the draft id
    books/<book_id>/status.json    job status, updated as pages land
    books/<book_id>/portrait.png   the approved cartoon portrait
    books/<book_id>/page-<n>.png   finished page art, one file per page
    books/<book_id>/prompts.json   the exact prompt per page (grading artefact)
    books/<book_id>/book.pdf       the assembled book, written last
* `DatabaseBookStore` (`db_book_store.py`) — the same contract against
  `DATABASE_URL`, so a book survives the death of the process that made it.

Which implementation runs is the composition root's decision (`DATABASE_URL`
set or not); nothing above this module knows or cares.

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
the disk is ephemeral and a restart loses books — `reap_incomplete` marks the
ones that were mid-render as failed so they do not poll as "pending" forever.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from app.models.book import AboutMe

__all__ = ["BookRecord", "BookStatus", "BookStore", "FileBookStore"]

logger = logging.getLogger(__name__)


class BookStatus:
    """The states a book job moves through. Kept as strings in storage."""

    PENDING = "pending"
    RENDERING = "rendering"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class BookRecord:
    """The stored request for one book, reloaded by the runner."""

    book_id: str
    about: AboutMe
    generation_id: str
    created_at: str
    #: The Google `sub` of the person it belongs to, or None for an unowned
    #: book (local development, password-only deployments).
    user_id: str | None = None


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
    ) -> None:
        """Persist the book's request. Written once, before rendering starts."""
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
    ) -> None:
        """Persist job status. Called after every page so clients can poll it."""
        raise NotImplementedError

    def load_status(self, book_id: str) -> dict[str, object] | None:
        """The payload the status route returns, or None when the book is gone."""
        raise NotImplementedError

    # --- artefacts -------------------------------------------------------------

    def save_portrait(self, book_id: str, data: bytes) -> None:
        raise NotImplementedError

    def read_portrait(self, book_id: str) -> bytes | None:
        raise NotImplementedError

    def save_page(self, book_id: str, page_number: int, data: bytes) -> None:
        """Persist one page's art. Raises on a bad page number, never on disk."""
        raise NotImplementedError

    def read_page(self, book_id: str, page_number: int) -> bytes | None:
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

    def page_url(self, book_id: str, page_number: int) -> str:
        return f"/book-media/{book_id}/pages/{page_number}"

    # --- lifecycle -------------------------------------------------------------

    def delete(self, book_id: str) -> None:
        raise NotImplementedError

    def reap_incomplete(self) -> int:
        """Mark books still rendering as failed (a restart orphaned them).

        Returns the number of books marked.
        """
        raise NotImplementedError


class FileBookStore(BookStore):
    """Reads and writes book artefacts under a root directory."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # Pages render concurrently (one worker thread each), and every prompt
        # append is a read-modify-write of prompts.json. Without this lock two
        # pages landing together would each read the same list and the last
        # writer would silently drop the other's record.
        self._prompts_lock = threading.Lock()

    # --- ownership -------------------------------------------------------------

    def owner_of(self, book_id: str) -> str | None:
        if not _safe(book_id):
            return None
        path = self.path(book_id, "book.json")
        if not path.is_file():
            return None
        try:
            payload = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            return None
        owner = payload.get("user_id")
        return str(owner) if owner else None

    # --- the request -----------------------------------------------------------

    def save_record(
        self,
        *,
        book_id: str,
        user_id: str | None,
        about: AboutMe,
        generation_id: str,
    ) -> None:
        _guard(book_id)
        payload: dict[str, object] = {
            "book_id": book_id,
            "user_id": user_id,
            "about": about.model_dump(mode="json"),
            "generation_id": generation_id,
            "created_at": _now(),
            "status": BookStatus.PENDING,
            "pages": [],
        }
        self._book_dir(book_id).mkdir(parents=True, exist_ok=True)
        self._atomic_write_json(self.path(book_id, "book.json"), payload)
        self.save_status(book_id, cast(str, payload["status"]), [])

    def load_record(self, book_id: str) -> BookRecord | None:
        """Reload a book's request, or None if it is gone or malformed."""
        if not _safe(book_id):
            return None
        path = self.path(book_id, "book.json")
        if not path.is_file():
            return None
        try:
            payload: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
            about = AboutMe.model_validate(payload["about"])
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            return None
        owner = payload.get("user_id")
        return BookRecord(
            book_id=book_id,
            about=about,
            generation_id=str(payload["generation_id"]),
            created_at=str(payload.get("created_at", "")),
            user_id=str(owner) if owner else None,
        )

    # --- status ----------------------------------------------------------------

    def save_status(
        self,
        book_id: str,
        status: str,
        pages: list[dict[str, object]],
        error: str | None = None,
    ) -> None:
        """Persist job status. Called after every page so clients can poll it."""
        payload = {
            "book_id": book_id,
            "status": status,
            "pages": pages,
            "error": error,
            "pdf_url": self.pdf_url(book_id) if status == BookStatus.COMPLETE else None,
            "updated_at": _now(),
        }
        self._atomic_write_json(self.path(book_id, "status.json"), payload)

    def load_status(self, book_id: str) -> dict[str, object] | None:
        if not _safe(book_id):
            return None
        path = self.path(book_id, "status.json")
        if not path.is_file():
            return None
        try:
            return dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            return None

    # --- artefacts -------------------------------------------------------------

    def save_portrait(self, book_id: str, data: bytes) -> None:
        self._write_image(book_id, "portrait", data)

    def read_portrait(self, book_id: str) -> bytes | None:
        return self._read_image(book_id, "portrait")

    def save_page(self, book_id: str, page_number: int, data: bytes) -> None:
        """Persist one page's art. Raises on a bad page number, never on disk."""
        if page_number < 1:
            raise ValueError("page_number counts from 1")
        self._write_image(book_id, f"page-{page_number}", data)

    def read_page(self, book_id: str, page_number: int) -> bytes | None:
        if page_number < 1:
            return None
        return self._read_image(book_id, f"page-{page_number}")

    def save_prompts(self, book_id: str, prompts: list[dict[str, str]]) -> None:
        """The prompt per page: the record of what was actually sent."""
        self._atomic_write_json(self.path(book_id, "prompts.json"), {"prompts": prompts})

    def append_prompt(self, book_id: str, prompt: dict[str, str]) -> None:
        """Add one page's prompt progressively, so a failed book keeps the
        record of the pages that were really sent.

        Atomic against concurrent pages: the read-modify-write runs under a
        lock, or two pages finishing together would lose one another's entry.
        """
        with self._prompts_lock:
            self.save_prompts(book_id, (self.load_prompts(book_id) or []) + [prompt])

    def load_prompts(self, book_id: str) -> list[dict[str, str]] | None:
        if not _safe(book_id):
            return None
        path = self.path(book_id, "prompts.json")
        if not path.is_file():
            return None
        try:
            value: object = json.loads(path.read_text(encoding="utf-8"))
            prompts = cast("list[dict[str, str]]", cast("dict[str, object]", value)["prompts"])
            return [dict(entry) for entry in prompts]
        except (OSError, json.JSONDecodeError):
            return None

    def read_prompts_json(self, book_id: str) -> bytes | None:
        path = self.path(book_id, "prompts.json")
        if not path.is_file():
            return None
        return path.read_bytes()

    def save_pdf(self, book_id: str, data: bytes) -> None:
        self._atomic_write_bytes(self.path(book_id, "book.pdf"), data)

    def read_pdf(self, book_id: str) -> bytes | None:
        path = self.path(book_id, "book.pdf")
        if path.is_file():
            return path.read_bytes()
        return None

    # --- lifecycle -------------------------------------------------------------

    def path(self, book_id: str, name: str) -> Path:
        return self._book_dir(book_id) / name

    def delete(self, book_id: str) -> None:
        if not _safe(book_id):
            return
        directory = self._book_dir(book_id)
        if directory.is_dir():
            for child in directory.iterdir():
                if child.is_file():
                    child.unlink()
            directory.rmdir()

    def reap_incomplete(self) -> int:
        """Mark books still pending or rendering as failed.

        Called once at boot: a restart orphans every mid-run job (the asyncio
        task died with the process), so its stale status must not keep polling
        as "pending" forever. Returns the number of books marked.
        """
        reaped = 0
        for directory in self.root.iterdir():
            if not directory.is_dir() or not _safe(directory.name):
                continue
            status = self.load_status(directory.name)
            if status is None or status.get("status") != BookStatus.RENDERING:
                continue
            pages = cast(
                "list[dict[str, object]]",
                cast("dict[str, object]", status).get("pages", []),
            )
            self.save_status(
                directory.name,
                BookStatus.FAILED,
                pages,
                error="The server restarted while this book was rendering. "
                "Start a new one — the finished pages shown here were kept.",
            )
            reaped += 1
            logger.warning("book.reaped id=%s (server restart)", directory.name)
        return reaped

    # --- plumbing --------------------------------------------------------------

    def _book_dir(self, book_id: str) -> Path:
        _guard(book_id)
        return self.root / book_id

    def _write_image(self, book_id: str, stem: str, data: bytes) -> None:
        suffix = "jpg" if data[:2] == b"\xff\xd8" else "png"
        self._atomic_write_bytes(self.path(book_id, f"{stem}.{suffix}"), data)

    def _read_image(self, book_id: str, stem: str) -> bytes | None:
        directory = self._book_dir(book_id)
        for suffix in ("png", "jpg"):
            candidate = directory / f"{stem}.{suffix}"
            if candidate.is_file():
                return candidate.read_bytes()
        return None

    @staticmethod
    def _atomic_write_bytes(path: Path, data: bytes) -> None:
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_bytes(data)
            tmp.replace(path)
        finally:
            # A sibling thread writing the same path may race us to the rename;
            # a leftover temp file is still cleaned up here.
            tmp.unlink(missing_ok=True)

    def _atomic_write_json(self, path: Path, payload: object) -> None:
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)


def _guard(book_id: str) -> None:
    """Ids become path segments; refuse to operate on an unsafe one."""
    if not _safe(book_id):
        raise ValueError(f"Unsafe book id {book_id!r}.")


def _safe(book_id: str) -> bool:
    return (
        bool(book_id)
        and not book_id.startswith(".")
        and not any(part in book_id for part in ("/", "\\", "..", "\x00"))
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()
