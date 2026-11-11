"""Book artefacts: the PDF, portraits, photos, cover, pages and prompt record.

A separate router from the book API because these are the bytes a reader asks
for, not the operations that make them. The caching rule is why they live
together: every response here is derived from a child's photograph, so all of
it is private and none of it may be cached by a shared browser or proxy.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import zipfile

from fastapi import APIRouter, HTTPException, Request, Response

from app.dependencies import ContainerDep
from app.errors import BookNotReady, UnknownBook
from app.models import StoryEdition
from app.routes.auth import CurrentSessionDep
from app.routes.book_access import (
    book_edition,
    ensure_book,
    etag_matches,
    no_store,
    not_modified,
    private_cached,
    user_id,
    versioned_picture,
)
from app.services.book_store import BookStore
from app.services.media import extension_for, media_type_for
from app.services.pictures import (
    VARIANT_MEDIA_TYPE,
    VARIANT_WIDTHS,
    encode_variant,
    page_slot,
    short_version,
)
from app.single_flight import KeyedLocks

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/book-media", tags=["book-media"])

#: One screen copy made at a time per picture and width (see app.single_flight).
_variant_locks = KeyedLocks()
_WIDTHS_LISTED = ", ".join(str(width) for width in sorted(VARIANT_WIDTHS))


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
        # The button says "Download", so the browser must save the file.
        # Served inline, Chrome opens it in its viewer instead and nothing
        # lands in Downloads: the header beats the link's download attribute.
        download=True,
    )


@router.get("/{book_id}/portrait")
def book_portrait(
    book_id: str,
    request: Request,
    container: ContainerDep,
    session: CurrentSessionDep,
    download: bool = False,
    w: int | None = None,
    v: str | None = None,
) -> Response:
    """The approved cartoon portrait every page is anchored on.

    With `?download=1` it is handed over as a file instead of shown in place, so
    the reader can keep the cartoon of their face — and a download is never a
    304: the reader asked for the file, not for permission to reuse one.
    """
    ensure_book(book_id, user_id(session), container)
    if w is not None and not download:
        return _sized(request, container, book_id, "portrait", w, v, session)
    data = container.book_store.read_portrait(book_id)
    if data is None:
        raise UnknownBook("That book has no portrait stored.")
    if download:
        return no_store(
            data,
            media_type=media_type_for(data),
            filename=f"cartoon-{book_id}{extension_for(data)}",
            download=True,
        )
    return private_cached(request, data, media_type=media_type_for(data))


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
def book_cover(
    book_id: str,
    request: Request,
    container: ContainerDep,
    session: CurrentSessionDep,
    w: int | None = None,
    v: str | None = None,
) -> Response:
    """The finished cover illustration, as soon as it exists.

    With `?w=` it is a screen-sized copy instead (see `_sized`).
    """
    ensure_book(book_id, user_id(session), container)
    if w is not None:
        return _sized(request, container, book_id, "cover", w, v, session)
    data = container.book_store.read_cover(book_id)
    if data is None:
        raise BookNotReady("The cover is not ready yet — poll the book's status.")
    return private_cached(request, data, media_type=media_type_for(data))


@router.get("/{book_id}/pages/{page_number}")
def book_page(
    page_number: int,
    book_id: str,
    request: Request,
    container: ContainerDep,
    session: CurrentSessionDep,
    w: int | None = None,
    v: str | None = None,
) -> Response:
    """One page's art, as soon as it exists — the browser shows them landing.

    Revalidated by content hash (T13): a redrawn page has new bytes and therefore
    a new tag, and a page that has not changed is a 304 rather than a megabyte.
    With `?w=` it is a screen-sized copy instead (see `_sized`).
    """
    ensure_book(book_id, user_id(session), container)
    if w is not None:
        return _sized(request, container, book_id, page_slot(page_number), w, v, session)
    etag = container.book_store.read_page_etag(book_id, page_number)
    if etag is not None and etag_matches(request, etag):
        # Answered from the stored hash alone: the image is never read out of the
        # database, so revalidation costs a few bytes in each direction.
        logger.info(
            "book.page_media user=%s book=%s page=%d outcome=not_modified",
            user_id(session) or "-",
            book_id,
            page_number,
        )
        return not_modified(etag)
    data = container.book_store.read_page(book_id, page_number)
    if data is None:
        raise BookNotReady("That page is not ready yet — poll the book's status.")
    return private_cached(request, data, media_type=media_type_for(data), etag=etag)


def _sized(
    request: Request,
    container: ContainerDep,
    book_id: str,
    slot: str,
    width: int,
    version: str | None,
    session: CurrentSessionDep,
) -> Response:
    """A picture at screen size, as WebP, made once and kept (P1, P2).

    The full-size PNG stays the stored picture (the PDF embeds it); this copy
    is a tenth of its weight. A URL whose `v` is the picture's current version
    may be kept by the browser for a year (`versioned_picture`): a redrawn
    picture has a new hash, so the page links it under a new URL. A URL with
    an old or missing `v` still gets today's picture, revalidated every time.
    The copy is made from the bytes whose hash it is filed under, so it can
    never outlive its picture.
    """
    if width not in VARIANT_WIDTHS:
        raise HTTPException(
            status_code=422,
            detail=f"w must be one of {_WIDTHS_LISTED}.",
        )
    store = container.book_store
    sha = store.picture_sha(book_id, slot)
    if sha is None:
        raise BookNotReady("That picture is not ready yet — poll the book's status.")
    current = version == short_version(sha)
    etag = f"{sha[:32]}-w{width}"
    if etag_matches(request, etag):
        return versioned_picture(None, etag=etag, immutable=current)
    data = store.read_variant(book_id, slot, width, sha)
    outcome = "stored"
    if data is None:
        data, outcome = _make_variant(store, book_id, slot, width, sha)
    logger.info(
        "book.picture user=%s book=%s slot=%s width=%d bytes=%d outcome=%s",
        user_id(session) or "-",
        book_id,
        slot,
        width,
        len(data),
        outcome,
    )
    return versioned_picture(data, media_type=VARIANT_MEDIA_TYPE, etag=etag, immutable=current)


def _make_variant(
    store: BookStore, book_id: str, slot: str, width: int, sha: str
) -> tuple[bytes, str]:
    """Encode the missing copy once, however many requests ask at the same time.

    A request that waited on the lock finds the copy the first one saved.
    """
    with _variant_locks.hold((book_id, slot, width, sha)):
        made = store.read_variant(book_id, slot, width, sha)
        if made is not None:
            return made, "stored"
        original = store.read_picture(book_id, slot)
        if original is None:
            raise BookNotReady("That picture is not ready yet — poll the book's status.")
        data = encode_variant(original, width)
        store.save_variant(book_id, slot, width, hashlib.sha256(original).hexdigest(), data)
        return data, "encoded"


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


_SOURCES_README = """\
These are the pictures behind your picture book.

star/        the photo you gave for the star, and the storybook portrait
             made from it
<name>/      the same pair for every other character you gave a face to
cover.*      the cover illustration
pages/       each story page's illustration, without the words
about.txt    the About page, as plain text
prompts.json exactly what the image model was asked to draw, page by page

The book's link stops working after its retention window, so keep this file
somewhere safe if you want the pictures for good.
"""


@router.get("/{book_id}/sources.zip")
async def book_sources(
    book_id: str, container: ContainerDep, session: CurrentSessionDep
) -> Response:
    """Every photo and picture behind the book, in one archive to keep.

    Assembled on request from what the book store already holds — nothing new
    is stored. Character photos come from the book's own references, not from
    the 24-hour drafts, so the archive is complete for the book's whole life.
    """
    ensure_book(book_id, user_id(session), container)
    edition = await asyncio.to_thread(book_edition, container, book_id)
    data = await asyncio.to_thread(_sources_archive, container.book_store, book_id, edition)
    logger.info(
        "book.sources user=%s book=%s bytes=%d", user_id(session) or "-", book_id, len(data)
    )
    return no_store(
        data,
        media_type="application/zip",
        filename=f"book-{book_id}-pictures.zip",
        download=True,
    )


def _sources_archive(store: BookStore, book_id: str, edition: StoryEdition) -> bytes:
    """Build the archive in memory; every member is optional but the README."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", _SOURCES_README)

        def add(name: str, data: bytes | None) -> None:
            if data is not None:
                archive.writestr(f"{name}{extension_for(data)}", data)

        add("star/photo", store.read_source_photo(book_id))
        add("star/portrait", store.read_portrait(book_id))
        for key, reference in sorted(store.load_characters(book_id).items()):
            add(f"{key}/photo", reference.photo)
            add(f"{key}/portrait", reference.portrait)
        add("cover", store.read_cover(book_id))
        for page in edition.pages:
            add(f"pages/page-{page.number:02d}", store.read_page(book_id, page.number))
        record = store.load_record(book_id)
        if record is not None:
            archive.writestr(
                "about.txt", "\n\n".join(record.about.rendered_about_lines()) + "\n"
            )
        prompts = store.read_prompts_json(book_id)
        if prompts is not None:
            archive.writestr("prompts.json", prompts)
    return buffer.getvalue()
