"""JSON API for the picture book, and the book's own media.

The flow is three endpoints around one background job:

    POST /api/book/start       -> start rendering using an approved portrait
    GET  /api/book/{id}        -> poll: status + page list, updated as each lands
    GET  /book-media/{id}/pdf  -> the assembled book once rendering is complete

The portrait itself is made with the existing `/api/cartoonify` endpoint and
reviewed through the existing approve flow — the book reuses the same pass-1
call and settings machinery, so the portrait the uploader saw is exactly the
portrait every page is anchored on.

Media (portrait, pages) lives under `/book-media` and is served with the same
no-store discipline as `/media`: the URLs carry the visitor's data and must not
be cached by shared browsers or proxies.

Everything the route accepts is validated like the rest of the app: closed enum
settings for the model-facing knobs, and the free-text About fields confined
to the PDF layer — visitor text never reaches a prompt.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import uuid
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from fastapi import APIRouter, Form, Request, Response, status
from pydantic import ValidationError

from app.container import Container
from app.dependencies import ContainerDep
from app.errors import (
    BookNotReady,
    GenerationRateLimited,
    InvalidSettings,
    UnknownBook,
)
from app.models import BOOK_PAGES, AboutMe, CartoonSettings
from app.routes.auth import CurrentSessionDep
from app.services.book_runner import CharacterReference
from app.services.book_store import BookRecord, BookStatus
from app.services.pipeline import UnknownGeneration
from app.services.prompts import (
    book_name_for,
    cast_photoable,
    photoable_cast,
    render_book_page,
)
from app.services.storage import Draft

__all__ = ["media_router", "router"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/book", tags=["book"])
media_router = APIRouter(prefix="/book-media", tags=["book-media"])

#: How long an identical `start` request reuses the book it already started.
#: Long enough to cover a double-submit, a second tab and a client retrying a
#: dropped response; short enough that "make another book from this portrait"
#: works again in the same sitting.
_START_REUSE_SECONDS = 10 * 60

#: Serialises the find-then-create pair so two identical requests arriving
#: together cannot both miss each other and start two books. A `threading`
#: lock, not an asyncio one: the work runs in a worker thread, so the lock is
#: independent of — and safe across — the several event loops tests create.
#: It protects one process; two processes could still race, which is why the
#: store lookup is the real guarantee and this is the cheap belt.
_start_lock = threading.Lock()


@lru_cache(maxsize=1)
def _default_rendered_pages() -> tuple[dict[str, object], ...]:
    """The catalogue rendered with default settings, computed once.

    The rendered titles are request-invariant (defaults only vary please talk
    with children's names, and the catalogue is name-free), so re-formatting
    per request was pure waste.
    """
    defaults = CartoonSettings()
    rendered = [render_book_page(page, defaults) for page in BOOK_PAGES]
    return tuple(
        {
            "number": page.number,
            "key": page.key,
            "title": page.scene.title,
            "story": page.story,
            "text_zone": page.text_zone.value,
        }
        for page in rendered
    )


@router.get("/pages")
def list_pages() -> dict[str, object]:
    """The book's page catalogue, so the UI can say what the book contains."""
    return {"pages": list(_default_rendered_pages())}


@router.get("/characters")
def list_characters() -> dict[str, object]:
    """The character slots the uploader may fill with a photo.

    Only the book's photoable cast appears here. Milky-White is cast in the
    prose and appears in no catalogue, endpoint or form: there is no upload
    path for her anywhere, by construction (`photoable = false` in the data).
    """
    return {
        "characters": [
            {"key": key, "label": label, "photoable": True} for key, label in photoable_cast()
        ]
    }


@router.post("/start", status_code=status.HTTP_201_CREATED)
async def start_book(
    request: Request,
    response: Response,
    container: ContainerDep,
    session: CurrentSessionDep,
    generation_id: str = Form(
        ..., description="Id of the approved portrait, returned by /api/cartoonify."
    ),
    child_name: str = Form("", max_length=60),
    about_name: str = Form("", max_length=60),
    about_age: int | None = Form(None, ge=0, le=99),
    about_title: str | None = Form(None, max_length=60),
    details: str = Form("", max_length=1000),
    character: list[str] = Form(
        default=[],
        description=(
            "Repeated `cast_key:generation_id` pairs. Each names a photoable "
            "cast member and the approved portrait made for them with "
            "/api/cartoonify; pages that cast the character send those images "
            "as references. Milky-White is refused: she is prose-pinned."
        ),
    ),
) -> dict[str, object]:
    """Start rendering the book from an already-approved cartoon portrait.

    Both names are optional: a blank resolves to Jack or Jane, following the
    photo-gender answer the portrait was made with. They are separate on
    purpose — the name the story uses and the name the About page shows may
    differ.

    One book costs one provider call per page. This endpoint charges one
    admission for the job and then one page token as each page actually
    renders — not the whole page count up front — so a 20-page book can never
    be refused by a 12-a-minute brake. The portrait call was already paid in
    pass 1 and is reused as an identity anchor on every page. Character
    portraits are paid the same way, one /api/cartoonify call each, before
    this endpoint is reached. Returns immediately; poll `GET /api/book/{id}`
    for progress and the PDF link.

    **Idempotent for ten minutes.** A request identical to one that already
    started a book — same portrait, same About fields, same character photos,
    same owner — returns that book with `200` and `"reused": true` instead of
    starting and billing a second one. Double-clicks, a second tab and a
    client retrying a dropped response all land here. A *failed* book is not
    reused: asking again after a failure means "try again".
    """
    client_key = _client_key(request)
    # Every store call here is blocking: file reads for the drafts, and in
    # database mode a network round trip for the record and the portrait blob.
    # None of it may run on the event loop that is also serving the polls.
    draft = await asyncio.to_thread(container.store.load_draft, generation_id)
    if draft is None:
        raise UnknownGeneration(generation_id)
    about = _about_from_form(
        child_name, about_name, about_age, about_title, details, draft.settings
    )
    characters = await asyncio.to_thread(_character_references, character, container)
    # The person the book belongs to. In Google mode every request here has a
    # session (the guard guarantees it); a password or open deployment has no
    # identity to file under, and the book stays unowned.
    user_id = _user_id(session)

    record, reused = await asyncio.to_thread(
        _start_or_reuse,
        container,
        client_key,
        user_id,
        generation_id,
        about,
        character,
    )
    book_id = record.book_id
    if reused:
        payload = await asyncio.to_thread(container.book_store.load_status, book_id) or {}
        logger.info(
            "book.start_reused user=%s book=%s draft=%s",
            user_id or "-",
            book_id,
            generation_id,
        )
        response.status_code = status.HTTP_200_OK
        return {
            "book_id": book_id,
            "status": payload.get("status", BookStatus.PENDING),
            "status_url": f"/api/book/{book_id}",
            "pdf_url": payload.get("pdf_url"),
            "reused": True,
        }
    try:
        await container.book_runner.start(
            record,
            characters,
            lambda: container.rate_limiter.allow(client_key, 1),
        )
    except UnknownGeneration:
        # Clean the half-created book so no zombie id is left discoverable.
        await asyncio.to_thread(container.book_store.delete, book_id)
        raise
    logger.info(
        "book.started user=%s book=%s draft=%s provider=%s characters=%s pages=%d",
        user_id or "-",
        book_id,
        generation_id,
        container.provider.name,
        ",".join(characters) or "-",
        len(BOOK_PAGES),
    )
    return {
        "book_id": book_id,
        "status": BookStatus.PENDING,
        "status_url": f"/api/book/{book_id}",
        "pdf_url": container.book_store.pdf_url(book_id),
        "reused": False,
    }


def _start_or_reuse(
    container: Container,
    client_key: str,
    user_id: str | None,
    generation_id: str,
    about: AboutMe,
    character: list[str],
) -> tuple[BookRecord, bool]:
    """The book this request already started, or a newly recorded one.

    Runs in a worker thread — every store call here is blocking, and in
    database mode each is a round trip — under a lock, so the find-then-create
    pair is one step for concurrent identical requests. The fingerprint covers
    every input that changes the artefact: the owner, the approved portrait,
    the About fields and the character photos, so a different name or a
    different photo is a different book.

    The rate-limit admission is charged here, after the reuse lookup and only
    for a book that is really about to render. A retry that finds its book
    started nothing, so it spends nothing — and a request that is over the
    brake fails before a record exists to clean up.
    """
    fingerprint = _request_fingerprint(
        user_id=user_id, generation_id=generation_id, about=about, character=character
    )
    with _start_lock:
        existing = container.book_store.find_started(
            fingerprint=fingerprint,
            since=datetime.now(UTC) - timedelta(seconds=_START_REUSE_SECONDS),
        )
        if existing is not None:
            record = container.book_store.load_record(existing)
            if record is not None:
                return record, True
        _check_rate(client_key, container)
        book_id = uuid.uuid4().hex
        container.book_store.save_record(
            book_id=book_id,
            user_id=user_id,
            about=about,
            generation_id=generation_id,
            fingerprint=fingerprint,
        )
    return (
        BookRecord(
            book_id=book_id,
            user_id=user_id,
            about=about,
            generation_id=generation_id,
            created_at="",
            fingerprint=fingerprint,
        ),
        False,
    )


def _request_fingerprint(
    *, user_id: str | None, generation_id: str, about: AboutMe, character: list[str]
) -> str:
    """A stable hash of everything that would change the finished book."""
    material = json.dumps(
        {
            "user": user_id or "",
            "generation": generation_id,
            "about": about.model_dump(mode="json"),
            # Order is the form's, not the semantics': two requests naming the
            # same photos in a different order ask for the same book.
            "characters": sorted(character),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).hexdigest()


@router.get("/{book_id}")
def book_status(
    book_id: str, container: ContainerDep, session: CurrentSessionDep
) -> dict[str, object]:
    """The book's progress: status, page list and any failure message."""
    return _ensure_book(book_id, _user_id(session), container)


# The finished PDF is served under /book-media like every other book artefact,
# so the status record's pdf_url and this file have exactly one canonical path.


# --- book-media: the pages as they render -------------------------------------


@media_router.get("/{book_id}/pdf", response_class=Response)
def book_pdf(book_id: str, container: ContainerDep, session: CurrentSessionDep) -> Response:
    """Serve the assembled book PDF once rendering has completed."""
    _ensure_book(book_id, _user_id(session), container)
    data = container.book_store.read_pdf(book_id)
    if data is None:
        raise BookNotReady(
            "The book is not ready yet — poll its status and retry once it reports complete."
        )
    logger.info(
        "book.pdf user=%s book=%s bytes=%d outcome=served",
        _user_id(session) or "-",
        book_id,
        len(data),
    )
    return _no_store(
        data,
        media_type="application/pdf",
        filename=f"book-{book_id}.pdf",
    )


@media_router.get("/{book_id}/portrait")
def book_portrait(
    book_id: str, container: ContainerDep, session: CurrentSessionDep
) -> Response:
    """The approved cartoon portrait every page is anchored on."""
    _ensure_book(book_id, _user_id(session), container)
    data = container.book_store.read_portrait(book_id)
    if data is None:
        raise UnknownBook("That book has no portrait stored.")
    return _no_store(data, media_type=_detect_media_type(data))


@media_router.get("/{book_id}/pages/{page_number}")
def book_page(
    page_number: int, book_id: str, container: ContainerDep, session: CurrentSessionDep
) -> Response:
    """One page's art, as soon as it exists — the browser shows them landing."""
    _ensure_book(book_id, _user_id(session), container)
    data = container.book_store.read_page(book_id, page_number)
    if data is None:
        raise BookNotReady("That page is not ready yet — poll the book's status.")
    return _no_store(data, media_type=_detect_media_type(data))


@media_router.get("/{book_id}/prompts.json")
def book_prompts(
    book_id: str, container: ContainerDep, session: CurrentSessionDep
) -> Response:
    """The exact prompt sent for each page — the record of what was sent."""
    _ensure_book(book_id, _user_id(session), container)
    data = container.book_store.read_prompts_json(book_id)
    if data is None:
        raise BookNotReady("The prompt record appears when the book completes.")
    return _no_store(data, media_type="application/json")


# --- shared helpers -----------------------------------------------------------


def _user_id(session: CurrentSessionDep) -> str | None:
    """The person behind the request, or None (password/anon deployments)."""
    return session.user_id if session else None


def _ensure_book(
    book_id: str,
    user_id: str | None,
    container: ContainerDep,
) -> dict[str, object]:
    """Authorise this request to touch a book's artefacts, and return its status.

    Books are keyed to the person who started them. A book with an owner is
    reachable only by that owner — anybody else, signed in or not, gets the
    same plain 404 they would get for a book that does not exist at all, so
    the id leaks no information. An unowned book is open to whoever has a
    session (that only happens where there is no identity to own things).

    Authorisation and payload come from one store call: this is the hot path
    of the progress UI, and a poll that spends three reads authorising itself
    is three round trips per browser tab per 1.5 seconds in database mode.
    """
    payload, owner = container.book_store.load_status_and_owner(book_id)
    if payload is None:
        raise UnknownBook("No book with that id. It may have expired — start a new one.")
    if owner and owner != (user_id or ""):
        raise UnknownBook("No book with that id. It may have expired — start a new one.")
    return payload


def _client_key(request: Request) -> str:
    """The rate-limit key for a request: its client address, or a stand-in."""
    return request.client.host if request.client else "unknown"


def _check_rate(client_key: str, container: Container, tokens: int = 1) -> None:
    if not container.rate_limiter.allow(client_key, tokens):
        raise GenerationRateLimited(
            "This client has reached the image-generation limit for this minute. "
            "A book costs one billable image per page, so it takes a few "
            "minutes of allowance. Please wait and try again."
        )


def _about_from_form(
    child_name: str,
    about_name: str,
    about_age: int | None,
    about_title: str | None,
    details: str,
    settings: CartoonSettings,
) -> AboutMe:
    """Resolve blank names and validate the uploader's words.

    A blank prompt name or About name becomes Jack or Jane — decided by the
    photo-gender answer the settings carry — rather than praying to a required
    field. Exactly one resolver lives here: the model receives an object whose
    names are always set.

    The text itself is free — it is drawn on the PDF, never sent to a model —
    but a malformed value fails here with a 422 naming the field rather than
    surfacing as a blank About page later.
    """
    resolved = book_name_for(settings)
    resolved_prompt_name = child_name.strip() or resolved
    resolved_about_name = about_name.strip() or resolved

    try:
        return AboutMe.model_validate(
            {
                "prompt_name": resolved_prompt_name,
                "about_name": resolved_about_name,
                "about_age": about_age,
                "title": about_title,
                "details": details,
            }
        )
    except ValidationError as exc:
        raise InvalidSettings(f"The About page details did not validate: {exc}") from exc


def _character_references(
    character: list[str], container: ContainerDep
) -> dict[str, CharacterReference]:
    """Resolve the repeated `character=key:generation_id` form parts.

    Each pair names a photoable cast member and the approved cartoon portrait
    the uploader made for them with `/api/cartoonify` — the same pass 1 the
    book's own portrait went through, one call per character. The portrait
    draft supplies both halves of the reference the pages get: the cartoon
    portrait and the original photograph behind it.

    Refused outright:

    * a key that is not photoable — Milky-White above all: the cow is pinned
      in prose, and there is no route, form or endpoint that can attach a face
      to her;
    * the same key twice — two photos of one character is ambiguous, and the
      later one would silently win;
    * a malformed pair, or a draft that has expired.

    The returned map is keyed by cast key and handed whole to the runner; the
    runner selects, per page, only the members that page's `cast` names.
    """
    references: dict[str, CharacterReference] = {}
    for pair in character:
        key, _, generation_id = pair.partition(":")
        key = key.strip()
        generation_id = generation_id.strip()
        if not key or not generation_id:
            raise InvalidSettings("Each character entry must be `key:generation_id`.")
        if not cast_photoable(key):
            # Covers both an unknown key and a real character that is not
            # photoable (the cow): neither has an upload path.
            raise InvalidSettings(
                f"There is no character slot for {key!r} — it cannot be given a photo."
            )
        if key in references:
            raise InvalidSettings(f"{key} was given more than one photo.")
        draft: Draft | None = container.store.load_draft(generation_id)
        if draft is None:
            raise UnknownGeneration(generation_id)
        references[key] = CharacterReference(
            portrait=draft.cartoon_image,
            portrait_media_type=draft.cartoon_media_type,
            photo=draft.photo_data,
            photo_media_type=draft.photo_media_type,
        )
    return references


def _no_store(data: bytes, *, media_type: str, filename: str | None = None) -> Response:
    """A response carrying child-derived data: private, never cached."""
    headers: dict[str, str] = {
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if filename:
        headers["Content-Disposition"] = f'inline; filename="{filename}"'
    return Response(content=data, media_type=media_type, headers=headers)


def _detect_media_type(data: bytes) -> str:
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG"):
        return "image/png"
    return "application/octet-stream"
