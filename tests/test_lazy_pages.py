"""A book's pictures are read from the store as the PDF draws them, not held."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from app.models.book import AboutMe
from app.services import book_document as book_document_module
from app.services.book_document import BookDocument
from app.services.book_store import BookRecord
from app.services.pdf import PageArt
from tests.helpers import jack, make_photo_bytes

PAGES = jack().pages


def test_a_lazy_page_reads_its_picture_on_every_access() -> None:
    reads: list[int] = []

    def load() -> bytes:
        reads.append(1)
        return b"art"

    art = PageArt(image=load, page=PAGES[0])
    assert reads == []
    assert art.image == b"art" and art.image == b"art"
    assert len(reads) == 2
    assert PageArt(image=b"held", page=art.page).image == b"held"


class Pages:
    def __init__(self) -> None:
        self.read: list[int] = []

    def read_page(self, book_id: str, number: int) -> bytes | None:
        self.read.append(number)
        return None if number == PAGES[2].number else make_photo_bytes()

    def __getattr__(self, name: str) -> Any:
        return lambda *_args, **_kwargs: None


def test_assemble_hands_the_pdf_lazy_pages_and_the_fresh_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Pages()
    seen: dict[str, Any] = {}

    def build(title: str, about: AboutMe, arts: list[PageArt], *a: Any, **k: Any) -> bytes:
        seen["read_before_drawing"] = list(store.read)
        seen["pages"] = [art.page for art in arts]
        seen["first"] = arts[0].image
        with pytest.raises(ValueError, match="is missing"):
            _ = arts[2].image
        return b"%PDF"

    monkeypatch.setattr(book_document_module, "build_book_pdf", build)
    document = BookDocument(book_store=store)  # type: ignore[arg-type]
    pages = list(PAGES[:3])
    fresh = replace(pages[1], story="formatted")
    record = BookRecord(
        book_id="b",
        about=AboutMe(prompt_name="Jack", about_name="Jack"),
        generation_id="g",
        created_at="",
        story_key="jack",
        title="T",
    )
    document.assemble(record, pages, {}, {2: fresh})

    assert seen["read_before_drawing"] == []
    assert seen["pages"] == [pages[0], fresh, pages[2]]
    assert seen["first"].startswith(b"\x89PNG")
    assert store.read == [PAGES[0].number, PAGES[2].number]
