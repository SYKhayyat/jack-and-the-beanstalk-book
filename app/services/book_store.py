"""Filesystem persistence for picture-book jobs.

One directory per book under ``<storage>/books``:

    books/<book_id>/book.json      what the uploader asked for + the draft id
    books/<book_id>/status.json    job status, updated as pages land
    books/<book_id>/photo.jpg      child's normalised photo (copied from the draft)
    books/<book_id>/portrait.png   the approved cartoon portrait
    books/<book_id>/prompts.json   the exact prompt per page (grading artefact)
    books/<book_id>/page-<n>.png   finished page art, one file per page
    books/<book_id>/book.pdf       the assembled book, written last

The status file is written after every page so the browser can show progress,
and so a page that did land stays visible even if a later page failed. As with
`storage.py`, Render's disk is ephemeral: a restart loses unfinished books,
which is acceptable for this iteration.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from app.models.book import AboutMe

__all__ = ["BookRecord", "BookStatus", "BookStore"]


class BookStatus:
    """The states a book job moves through. Kept as strings on disk."""

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


class BookStore:
    """Reads and writes book artefacts under a root directory."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # --- the request -----------------------------------------------------------

    def save_record(self, *, book_id: str, about: AboutMe, generation_id: str) -> None:
        """Persist the book's request. Written once, before rendering starts."""
        _guard(book_id)
        payload = {
            "book_id": book_id,
            "about": about.model_dump(mode="json"),
            "generation_id": generation_id,
            "created_at": _now(),
            "status": BookStatus.PENDING,
            "pages": [],
        }
        self._book_dir(book_id).mkdir(parents=True, exist_ok=True)
        self.path(book_id, "book.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
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
        return BookRecord(
            book_id=book_id,
            about=about,
            generation_id=str(payload["generation_id"]),
            created_at=str(payload.get("created_at", "")),
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
        self.path(book_id, "status.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )

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

    def read_pages(self, book_id: str, count: int) -> list[bytes | None]:
        """Every page in order; missing pages appear as None."""
        return [self.read_page(book_id, number) for number in range(1, count + 1)]

    def save_prompts(self, book_id: str, prompts: list[dict[str, str]]) -> None:
        """The prompt per page: the record of what was actually sent."""
        self.path(book_id, "prompts.json").write_text(
            json.dumps({"prompts": prompts}, indent=2), encoding="utf-8"
        )

    def save_pdf(self, book_id: str, data: bytes) -> None:
        self.path(book_id, "book.pdf").write_bytes(data)

    def read_pdf(self, book_id: str) -> bytes | None:
        path = self.path(book_id, "book.pdf")
        if path.is_file():
            return path.read_bytes()
        return None

    def pdf_url(self, book_id: str) -> str:
        return f"/book-media/{book_id}/pdf"

    def portrait_url(self, book_id: str) -> str:
        return f"/book-media/{book_id}/portrait"

    def page_url(self, book_id: str, page_number: int) -> str:
        return f"/book-media/{book_id}/pages/{page_number}"

    # --- plumbing --------------------------------------------------------------

    def path(self, book_id: str, name: str) -> Path:
        return self._book_dir(book_id) / name

    def delete(self, book_id: str) -> None:
        if not _safe(book_id):
            return
        directory = self._book_dir(book_id)
        if directory.is_dir():
            for child in directory.iterdir():
                child.unlink()

    def _book_dir(self, book_id: str) -> Path:
        return self.root / book_id

    def _write_image(self, book_id: str, stem: str, data: bytes) -> None:
        suffix = "jpg" if data[:2] == b"\xff\xd8" else "png"
        self.path(book_id, f"{stem}.{suffix}").write_bytes(data)

    def _read_image(self, book_id: str, stem: str) -> bytes | None:
        directory = self._book_dir(book_id)
        for suffix in ("png", "jpg"):
            candidate = directory / f"{stem}.{suffix}"
            if candidate.is_file():
                return candidate.read_bytes()
        return None


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
