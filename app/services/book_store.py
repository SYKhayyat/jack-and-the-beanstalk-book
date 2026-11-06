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
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from app.models.book import AboutMe
from app.models.settings import CartoonSettings
from app.services.media import image_filename, media_type_for

__all__ = [
    "BookRecord",
    "BookStatus",
    "BookStore",
    "BookSummary",
    "CharacterReference",
    "FileBookStore",
]

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True, slots=True)
class BookSummary:
    """One card in the library: enough to list and link, no blobs."""

    book_id: str
    title: str
    status: str
    created_at: str
    updated_at: str
    page_count: int
    pages_done: int
    cover_url: str | None
    pdf_url: str | None


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
        fingerprint: str | None = None,
        settings: CartoonSettings | None = None,
    ) -> None:
        """Persist the book's request. Written once, before rendering starts."""
        raise NotImplementedError

    def update_about(self, book_id: str, about: AboutMe) -> None:
        """Replace the About fields (the cheap edit: PDF text only)."""
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

    def save_cover(self, book_id: str, data: bytes) -> None:
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

    def cover_url(self, book_id: str) -> str:
        return f"/book-media/{book_id}/cover"

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
        fingerprint: str | None = None,
        settings: CartoonSettings | None = None,
    ) -> None:
        _guard(book_id)
        payload: dict[str, object] = {
            "book_id": book_id,
            "user_id": user_id,
            "about": about.model_dump(mode="json"),
            "generation_id": generation_id,
            "request_fingerprint": fingerprint,
            "settings": (settings or CartoonSettings()).model_dump(mode="json"),
            "created_at": _now(),
            "status": BookStatus.PENDING,
            "pages": [],
        }
        self._book_dir(book_id).mkdir(parents=True, exist_ok=True)
        self._atomic_write_json(self.path(book_id, "book.json"), payload)
        self.save_status(book_id, cast(str, payload["status"]), [], title=about.book_title)

    def update_about(self, book_id: str, about: AboutMe) -> None:
        """Rewrite just the About fields; the request fingerprint stays."""
        _guard(book_id)
        path = self.path(book_id, "book.json")
        try:
            payload = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise KeyError(f"No record for book {book_id!r}.") from exc
        payload["about"] = about.model_dump(mode="json")
        self._atomic_write_json(path, payload)
        status = self.load_status(book_id)
        if status is not None:
            self.save_status(
                book_id,
                str(status.get("status", BookStatus.PENDING)),
                _pages_of(status),
                cast("str | None", status.get("error")),
                title=about.book_title,
                cover_url=self._cover_url_if_present(book_id),
            )

    def list_for_owner(self, user_id: str, *, limit: int = 50) -> list[BookSummary]:
        """Scan the book directories for this owner's books, newest first."""
        if not user_id:
            return []
        found: list[BookSummary] = []
        for directory in self.root.iterdir():
            if not directory.is_dir() or not _safe(directory.name):
                continue
            record = self.load_record(directory.name)
            if record is None or record.user_id != user_id:
                continue
            found.append(self._summary(directory.name, record))
        found.sort(key=lambda summary: summary.created_at, reverse=True)
        return found[:limit]

    def _summary(self, book_id: str, record: BookRecord) -> BookSummary:
        status = self.load_status(book_id) or {}
        pages = _pages_of(status)
        cover_url = self._cover_url_if_present(book_id)
        page_count, done = page_totals(pages, cover_done=cover_url is not None)
        state = str(status.get("status", BookStatus.PENDING))
        return BookSummary(
            book_id=book_id,
            title=record.about.book_title,
            status=state,
            created_at=record.created_at,
            updated_at=str(status.get("updated_at", record.created_at)),
            page_count=page_count,
            pages_done=done,
            cover_url=cover_url,
            pdf_url=self.pdf_url(book_id) if state == BookStatus.COMPLETE else None,
        )

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
            settings = CartoonSettings.model_validate(payload.get("settings") or {})
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            return None
        owner = payload.get("user_id")
        fingerprint = payload.get("request_fingerprint")
        return BookRecord(
            book_id=book_id,
            about=about,
            generation_id=str(payload["generation_id"]),
            created_at=str(payload.get("created_at", "")),
            user_id=str(owner) if owner else None,
            fingerprint=str(fingerprint) if fingerprint else None,
            settings=settings,
        )

    def find_started(self, *, fingerprint: str, since: datetime) -> str | None:
        """Scan the book directories for an identical request in flight.

        Books are few and short-lived (retention deletes them), so a directory
        scan is the honest filesystem answer to a lookup by value. Each
        candidate costs two small JSON reads; a status that cannot be read is
        skipped rather than treated as a match.
        """
        for directory in self.root.iterdir():
            if not directory.is_dir() or not _safe(directory.name):
                continue
            record = self.load_record(directory.name)
            if record is None or record.fingerprint != fingerprint:
                continue
            status = self.load_status(directory.name)
            if status is None or status.get("status") not in _REUSABLE_STATUSES:
                continue
            if _started_at_or_after(record.created_at, since):
                return directory.name
        return None

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
        """Persist job status. Called after every page so clients can poll it."""
        payload: dict[str, object] = {
            "book_id": book_id,
            "status": status,
            "pages": pages,
            "error": error,
            "pdf_url": self.pdf_url(book_id) if status == BookStatus.COMPLETE else None,
            "cover_url": cover_url
            if cover_url is not None
            else self._cover_url_if_present(book_id),
            "title": title,
            "updated_at": _now(),
        }
        if extra:
            payload.update(extra)
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

    def load_status_and_owner(
        self, book_id: str
    ) -> tuple[dict[str, object] | None, str | None]:
        """Both documents a poll needs, from the two files that hold them."""
        return self.load_status(book_id), self.owner_of(book_id)

    # --- artefacts -------------------------------------------------------------

    def save_portrait(self, book_id: str, data: bytes) -> None:
        self._write_image(book_id, "portrait", data)

    def read_portrait(self, book_id: str) -> bytes | None:
        return self._read_image(book_id, "portrait")

    def save_source_photo(self, book_id: str, data: bytes) -> None:
        self._write_image(book_id, "photo", data)

    def update_identity(
        self,
        book_id: str,
        *,
        portrait: bytes,
        photo: bytes,
        settings: CartoonSettings,
    ) -> None:
        self.save_portrait(book_id, portrait)
        self.save_source_photo(book_id, photo)
        path = self.path(book_id, "book.json")
        try:
            payload = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise KeyError(f"No record for book {book_id!r}.") from exc
        payload["settings"] = settings.model_dump(mode="json")
        self._atomic_write_json(path, payload)

    def read_source_photo(self, book_id: str) -> bytes | None:
        return self._read_image(book_id, "photo")

    def save_cover(self, book_id: str, data: bytes) -> None:
        self._write_image(book_id, "cover", data)

    def read_cover(self, book_id: str) -> bytes | None:
        return self._read_image(book_id, "cover")

    def save_characters(self, book_id: str, characters: dict[str, CharacterReference]) -> None:
        directory = self._book_dir(book_id) / "characters"
        directory.mkdir(parents=True, exist_ok=True)
        for key, ref in characters.items():
            self.save_character(book_id, key, ref)

    def save_character(self, book_id: str, key: str, ref: CharacterReference) -> None:
        _guard(key)
        directory = self._book_dir(book_id) / "characters"
        directory.mkdir(parents=True, exist_ok=True)
        self._atomic_write_bytes(
            directory / image_filename(f"{key}-portrait", ref.portrait), ref.portrait
        )
        self._atomic_write_bytes(
            directory / image_filename(f"{key}-photo", ref.photo), ref.photo
        )

    def load_characters(self, book_id: str) -> dict[str, CharacterReference]:
        directory = self._book_dir(book_id) / "characters"
        if not directory.is_dir():
            return {}
        found: dict[str, CharacterReference] = {}
        for child in directory.iterdir():
            if not child.is_file() or "-portrait" not in child.stem:
                continue
            key = child.stem.removesuffix("-portrait")
            if not _safe(key):
                continue
            portrait = self._read_image_file(directory, f"{key}-portrait")
            photo = self._read_image_file(directory, f"{key}-photo")
            if portrait is None or photo is None:
                continue
            found[key] = CharacterReference(
                portrait=portrait,
                portrait_media_type=media_type_for(portrait),
                photo=photo,
                photo_media_type=media_type_for(photo),
            )
        return found

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
            for child in sorted(directory.rglob("*"), reverse=True):
                if child.is_file():
                    child.unlink()
                elif child.is_dir():
                    child.rmdir()
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
            # A book is orphaned from the moment it has a record. A crash
            # between `save_record` (which writes PENDING) and the runner's
            # first status write leaves a book that never renders, so PENDING
            # must be reaped exactly like RENDERING.
            if status is None or status.get("status") not in {
                BookStatus.PENDING,
                BookStatus.RENDERING,
            }:
                continue
            pages = cast(
                "list[dict[str, object]]",
                cast("dict[str, object]", status).get("pages", []),
            )
            record = self.load_record(directory.name)
            self.save_status(
                directory.name,
                BookStatus.FAILED,
                pages,
                error="The server restarted while this book was rendering. "
                "Start a new one — the finished pages shown here were kept.",
                title=record.about.book_title if record is not None else None,
            )
            reaped += 1
            logger.warning("book.reaped id=%s (server restart)", directory.name)
        return reaped

    def expire(self, *, older_than_seconds: float) -> int:
        """Delete finished books older than the window; see `BookStore.expire`.

        Age is the book's last activity — the status document's `updated_at`
        (written at completion and on every edit) — not the moment the request
        was first made. A book made a week ago and edited today has just been
        used and must not be swept because its `created_at` is old.
        """
        if older_than_seconds <= 0:
            return 0
        cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
        removed = 0
        for directory in self.root.iterdir():
            if not directory.is_dir() or not _safe(directory.name):
                continue
            status = self.load_status(directory.name)
            if status is not None and status.get("status") in {
                BookStatus.PENDING,
                BookStatus.RENDERING,
            }:
                continue
            record = self.load_record(directory.name)
            last_activity = _last_activity(status, record, directory)
            if last_activity is not None and last_activity > cutoff:
                continue
            self.delete(directory.name)
            removed += 1
            logger.info("book.expired id=%s", directory.name)
        return removed

    # --- plumbing --------------------------------------------------------------

    def _book_dir(self, book_id: str) -> Path:
        _guard(book_id)
        return self.root / book_id

    def _write_image(self, book_id: str, stem: str, data: bytes) -> None:
        self._atomic_write_bytes(self.path(book_id, image_filename(stem, data)), data)

    def _read_image(self, book_id: str, stem: str) -> bytes | None:
        return self._read_image_file(self._book_dir(book_id), stem)

    @staticmethod
    def _read_image_file(directory: Path, stem: str) -> bytes | None:
        for suffix in ("png", "jpg"):
            candidate = directory / f"{stem}.{suffix}"
            if candidate.is_file():
                return candidate.read_bytes()
        return None

    def _cover_url_if_present(self, book_id: str) -> str | None:
        if self.read_cover(book_id) is not None:
            return self.cover_url(book_id)
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


def _pages_of(status: dict[str, object]) -> list[dict[str, object]]:
    """The page list from a status document, safely typed for rewriting."""
    raw = status.get("pages", [])
    if not isinstance(raw, list):
        return []
    return [dict(entry) for entry in raw if isinstance(entry, dict)]


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


#: The states in which an identical `start` request should be answered with
#: the book that already exists rather than a second one.
_REUSABLE_STATUSES = frozenset({BookStatus.PENDING, BookStatus.RENDERING, BookStatus.COMPLETE})


def _last_activity(
    status: dict[str, object] | None,
    record: BookRecord | None,
    directory: Path,
) -> datetime | None:
    """When a book was last written, for retention.

    The status document's timestamp wins: it is written when a book finishes
    and again on every edit, so it is the honest "last used". A book with no
    readable status falls back to its record, and one with neither to the
    directory's own mtime. `None` means nothing datable was found — which the
    caller treats as old enough to remove, exactly as it did for an unreadable
    record before.
    """
    raw_values: tuple[object, ...] = (
        status.get("updated_at") if status else None,
        record.created_at if record else None,
    )
    for raw in raw_values:
        if not isinstance(raw, str) or not raw:
            continue
        try:
            stamp = datetime.fromisoformat(raw)
        except ValueError:
            continue
        return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)
    try:
        return datetime.fromtimestamp(directory.stat().st_mtime, UTC)
    except OSError:
        return None


def _started_at_or_after(created_at: str, since: datetime) -> bool:
    """Whether a stored (ISO) creation time is inside the reuse window."""
    try:
        started = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return started >= since


def _now() -> str:
    return datetime.now(UTC).isoformat()
