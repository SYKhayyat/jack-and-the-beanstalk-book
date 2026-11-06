"""Book artefacts: the PDF, portraits, photos, cover, pages and prompt record.

A separate router from the book API because these are the bytes a reader asks
for, not the operations that make them. The caching rule is why they live
together: every response here is derived from a child's photograph, so all of
it is private and none of it may be cached by a shared browser or proxy.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Response

from app.dependencies import ContainerDep
from app.errors import BookNotReady, UnknownBook
from app.routes.auth import CurrentSessionDep
from app.routes.book_access import ensure_book, no_store, user_id
from app.services.media import extension_for, media_type_for

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/book-media", tags=["book-media"])


@router.get("/{book_id}/pdf", response_class=Response)
def book_pdf(book_id: str, container: ContainerDep, session: CurrentSessionDep) -> Response:
    """Serve the assembled book PDF once rendering has completed."""
    ensure_book(book_id, user_id(session), container)
    data = container.book_store.read_pdf(book_id)
    if data is None:
        raise BookNotReady(
            "The book is not ready yet — poll its status and retry once it reports complete."
        )
    logger.info(
        "book.pdf user=%s book=%s bytes=%d outcome=served",
        user_id(session) or "-",
        book_id,
        len(data),
    )
    return no_store(
        data,
        media_type="application/pdf",
        filename=f"book-{book_id}.pdf",
    )


@router.get("/{book_id}/portrait")
def book_portrait(
    book_id: str,
    container: ContainerDep,
    session: CurrentSessionDep,
    download: bool = False,
) -> Response:
    """The approved cartoon portrait every page is anchored on.

    With `?download=1` it is handed over as a file instead of shown in place,
    so the reader can keep the cartoon of their face.
    """
    ensure_book(book_id, user_id(session), container)
    data = container.book_store.read_portrait(book_id)
    if data is None:
        raise UnknownBook("That book has no portrait stored.")
    return no_store(
        data,
        media_type=media_type_for(data),
        filename=f"cartoon-{book_id}{extension_for(data)}",
        download=download,
    )


@router.get("/{book_id}/photo")
def book_photo(
    book_id: str,
    container: ContainerDep,
    session: CurrentSessionDep,
    download: bool = False,
) -> Response:
    """The original photograph the book was drawn from.

    With `?download=1` the reader gets the untouched photo back, not a copy of
    somebody else's face.
    """
    ensure_book(book_id, user_id(session), container)
    data = container.book_store.read_source_photo(book_id)
    if data is None:
        raise UnknownBook("That book has no original photo stored.")
    return no_store(
        data,
        media_type=media_type_for(data),
        filename=f"original-photo-{book_id}{extension_for(data)}",
        download=download,
    )


@router.get("/{book_id}/cover")
def book_cover(book_id: str, container: ContainerDep, session: CurrentSessionDep) -> Response:
    """The finished cover illustration, as soon as it exists."""
    ensure_book(book_id, user_id(session), container)
    data = container.book_store.read_cover(book_id)
    if data is None:
        raise BookNotReady("The cover is not ready yet — poll the book's status.")
    return no_store(data, media_type=media_type_for(data))


@router.get("/{book_id}/pages/{page_number}")
def book_page(
    page_number: int, book_id: str, container: ContainerDep, session: CurrentSessionDep
) -> Response:
    """One page's art, as soon as it exists — the browser shows them landing."""
    ensure_book(book_id, user_id(session), container)
    data = container.book_store.read_page(book_id, page_number)
    if data is None:
        raise BookNotReady("That page is not ready yet — poll the book's status.")
    return no_store(data, media_type=media_type_for(data))


@router.get("/{book_id}/prompts.json")
def book_prompts(
    book_id: str, container: ContainerDep, session: CurrentSessionDep
) -> Response:
    """The exact prompt sent for each page — the record of what was sent."""
    ensure_book(book_id, user_id(session), container)
    data = container.book_store.read_prompts_json(book_id)
    if data is None:
        raise BookNotReady("The prompt record appears when the book completes.")
    return no_store(data, media_type="application/json")
