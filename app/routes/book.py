"""JSON API for the picture book.

The flow is three endpoints around one background job:

    POST /api/book/start  -> start rendering using an approved portrait
    GET  /api/book/{id}   -> poll: status + page list, updated as each lands
    GET  /api/books       -> the signed-in person's library

and two read-only story endpoints, `/api/stories/{key}/pages` and
`/api/stories/{key}/characters`, describing what a new book of that story
will contain.

The portrait itself is made with the existing `/api/cartoonify` endpoint and
reviewed through the existing approve flow — the book reuses the same pass-1
call and settings machinery, so the portrait the uploader saw is exactly the
portrait every page is anchored on.

Media (portrait, pages, PDF) lives in `app/routes/book_media.py` and is served
with the same no-store discipline as `/media`: those URLs carry the visitor's
data and must not be cached by shared browsers or proxies.

Everything the route accepts is validated like the rest of the app: closed enum
settings for the model-facing knobs, and the free-text About fields confined
to the PDF layer — visitor text never reaches a prompt.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Form, Request, Response, status
from pydantic import ValidationError

from app.container import Container
from app.dependencies import ContainerDep
from app.errors import (
    GenerationRateLimited,
    InvalidSettings,
    PaymentRequired,
    UnknownBook,
)
from app.models import (
    AboutMe,
    AboutPhotoChoice,
    AspectRatio,
    BookPage,
    CartoonSettings,
    PageFault,
    StoryEdition,
)
from app.routes.auth import CurrentSessionDep
from app.routes.book_access import book_edition as _book_edition
from app.routes.book_access import ensure_book as _ensure_book
from app.routes.book_access import new_book_edition
from app.routes.book_access import user_id as _user_id
from app.services.book_runner import CharacterReference
from app.services.book_store import BookRecord, BookStatus, BookSummary
from app.services.pipeline import UnknownGeneration
from app.services.prompts import book_name_for, render_book_page
from app.services.storage import Draft
from app.services.story_settings import story_rules
from app.services.wallet import InsufficientFunds, OutOfFunds, format_cents

__all__ = ["books_router", "router", "stories_router"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/book", tags=["book"])
books_router = APIRouter(prefix="/api/books", tags=["books"])
stories_router = APIRouter(prefix="/api/stories", tags=["stories"])

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


@stories_router.get("/{story_key}/pages")
def list_pages(story_key: str, container: ContainerDep) -> dict[str, object]:
    """What a new book of this story contains, page by page.

    Rendered with the story's default star name, so the words read as the
    book would print them for a visitor who leaves the name blank.
    """
    edition = new_book_edition(container, story_key)
    defaults = CartoonSettings()
    name = book_name_for(defaults, edition)
    return {
        "story": edition.key,
        "pages": [
            {
                "number": page.number,
                "key": page.key,
                "title": page.scene.title,
                "story": page.story,
            }
            for page in (
                render_book_page(page, defaults, prompt_name=name) for page in edition.pages
            )
        ],
    }


@stories_router.get("/{story_key}/characters")
def list_characters(story_key: str, container: ContainerDep) -> dict[str, object]:
    """The character slots the uploader may fill with a photo, for this story.

    Only the story's photoable supporting cast appears here. A character the
    bible says can never be photographed (Milky-White is not even cast; the
    wolf is cast but never photographed) appears in no catalogue, endpoint or
    form: there is no upload path for them anywhere, by construction.
    """
    edition = new_book_edition(container, story_key)
    return {
        "story": edition.key,
        "characters": [
            {"key": member.key, "label": member.label, "photoable": True}
            for member in edition.photoable_cast
        ],
    }


@books_router.get("")
def list_books(container: ContainerDep, session: CurrentSessionDep) -> dict[str, object]:
    """The signed-in person's books, newest first.

    Password/open deployments have no person to file a book under, so the
    library is deliberately unavailable there: an empty list is the honest
    answer, not a shared pile of everybody's children's photographs.
    """
    owner = _user_id(session)
    if not owner:
        return {"books": []}
    summaries = container.book_store.list_for_owner(owner)
    return {"books": [_summary_dict(summary) for summary in summaries]}


def _summary_dict(summary: BookSummary) -> dict[str, object]:
    return {
        "book_id": summary.book_id,
        "title": summary.title,
        "status": summary.status,
        "created_at": summary.created_at,
        "updated_at": summary.updated_at,
        "page_count": summary.page_count,
        "pages_done": summary.pages_done,
        "cover_url": summary.cover_url,
        "pdf_url": summary.pdf_url,
    }


@router.post("/start", status_code=status.HTTP_201_CREATED)
async def start_book(
    request: Request,
    response: Response,
    container: ContainerDep,
    session: CurrentSessionDep,
    story: str = Form(..., description="The story's key, e.g. `little_red_riding_hood`."),
    generation_id: str = Form(
        ..., description="Id of the approved portrait, returned by /api/cartoonify."
    ),
    child_name: str = Form("", max_length=60),
    about_name: str = Form("", max_length=60),
    about_age: int | None = Form(None, ge=0, le=99),
    about_title: str | None = Form(None, max_length=60),
    details: str = Form("", max_length=1000),
    about_photo: str = Form(
        AboutPhotoChoice.CARTOON,
        description="What the About page frames: `cartoon`, `photo` or `both`.",
    ),
    aspect_ratio: str = Form(
        "",
        description=(
            "The book's page shape, chosen on the About screen. Blank keeps the "
            "shape the star's portrait was made with."
        ),
    ),
    character: list[str] = Form(
        default=[],
        description=(
            "Repeated `cast_key:generation_id` pairs. Each names a photoable "
            "cast member of the story and the approved portrait made for them "
            "with /api/cartoonify; pages that cast the character send those "
            "images as references. A character who cannot be photographed is "
            "refused."
        ),
    ),
) -> dict[str, object]:
    """Start rendering the book from an already-approved cartoon portrait.

    Both names are optional: a blank resolves to the story's default star
    name (Jack or Jane, Red), following the photo-gender answer the portrait
    was made with. They are separate on purpose — the name the story uses and
    the name the About page shows may differ.

    The book is pinned to the edition of the story it starts on: the
    template version when there is one, otherwise the latest words and looks.
    With the operator's start gate on, a story whose plates are not approved
    yet is refused with 409 and one plain sentence.

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
    edition = await asyncio.to_thread(new_book_edition, container, story)
    draft = await asyncio.to_thread(container.store.load_draft, generation_id)
    if draft is None:
        raise UnknownGeneration(generation_id)
    if draft.story_key != edition.key:
        raise InvalidSettings(
            "That portrait was made for a different book. Make the star's portrait "
            "in this book's studio."
        )
    if draft.cast_key:
        # The book's identity anchor is the child. A portrait made for a cast
        # member would put, say, the mother's face on every page as Jack.
        raise InvalidSettings(
            "That portrait was made for a book character, not the book's star. "
            "Make the star's portrait from the child's own photo."
        )
    settings = _with_page_shape(draft.settings, aspect_ratio, edition)
    about = _about_from_form(
        edition, child_name, about_name, about_age, about_title, details, settings, about_photo
    )
    characters = await asyncio.to_thread(_character_references, character, container, edition)
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
        settings,
        edition,
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
    # The portraits this book is made from were paid for before it existed;
    # they are part of what it cost.
    drafts = [generation_id, *(pair.partition(":")[2].strip() for pair in character)]
    await asyncio.to_thread(container.costs.adopt_drafts, book_id, drafts)
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
        "book.started user=%s book=%s story=%s version=%s draft=%s provider=%s "
        "characters=%s pages=%d",
        user_id or "-",
        book_id,
        edition.key,
        edition.template_version_id or "-",
        generation_id,
        container.provider.name,
        ",".join(characters) or "-",
        len(edition.pages),
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
    settings: CartoonSettings,
    edition: StoryEdition,
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
        user_id=user_id,
        story_key=edition.key,
        generation_id=generation_id,
        about=about,
        character=character,
        aspect_ratio=settings.aspect_ratio,
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
        _require_funds(container, user_id)
        _check_rate(client_key, container)
        book_id = uuid.uuid4().hex
        container.book_store.save_record(
            book_id=book_id,
            user_id=user_id,
            about=about,
            generation_id=generation_id,
            edition=edition,
            fingerprint=fingerprint,
            settings=settings,
        )
    return (
        BookRecord(
            book_id=book_id,
            user_id=user_id,
            about=about,
            generation_id=generation_id,
            created_at="",
            story_key=edition.key,
            title=edition.title_for(about.prompt_name),
            fingerprint=fingerprint,
            settings=settings,
        ),
        False,
    )


def _request_fingerprint(
    *,
    user_id: str | None,
    story_key: str,
    generation_id: str,
    about: AboutMe,
    character: list[str],
    aspect_ratio: str,
) -> str:
    """A stable hash of everything that would change the finished book."""
    material = json.dumps(
        {
            "user": user_id or "",
            "story": story_key,
            "generation": generation_id,
            "aspect_ratio": aspect_ratio,
            "about": about.model_dump(mode="json"),
            # Order is the form's, not the semantics': two requests naming the
            # same photos in a different order ask for the same book.
            "characters": sorted(character),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).hexdigest()


@router.post("/{book_id}/edit", status_code=status.HTTP_202_ACCEPTED)
async def edit_book(
    request: Request,
    book_id: str,
    container: ContainerDep,
    session: CurrentSessionDep,
    action: str = Form(...),
    page_number: int | None = Form(None),
    character_key: str = Form(""),
    generation_id: str = Form(""),
    child_name: str = Form("", max_length=60),
    about_name: str = Form("", max_length=60),
    about_age: int | None = Form(None, ge=0, le=99),
    about_title: str | None = Form(None, max_length=60),
    details: str = Form("", max_length=1000),
    about_photo: str = Form(""),
    reason: list[str] = Form(default=[]),
) -> dict[str, object]:
    """Edit a finished book without starting a new one.

    Each picture an image action draws is bought as it is drawn, at the
    wallet's picture price; `about` and `delete` draw nothing and cost
    nothing. When the wallet cannot cover the next picture the job stops with
    the ones already made kept, and the book page shows how to finish it.

    Actions, and exactly what each one costs:

    * `page` — redraw one story page (up to the page-attempt limit if the
      review fails it). The ticked chips are `PageFault` names: the reviewer's
      own vocabulary, so a parent's tick and a reviewer's finding steer the
      redraw with the same sentences.
    * `character` — replace one face. A new portrait draft is made by
      `/api/cartoonify` first (one call); pages that cast that character are
      redrawn. `character_key="star"` replaces the child themselves and redraws
      the whole book plus the cover.
    * `cover` — redraw only the cover. One image call.
    * `about` — rewrite the PDF text only. No image call.
    * `retry` — pick up the pages (and cover) a failed book never finished.
    * `delete` — remove the book and every artefact.

    Every image action returns immediately; poll `GET /api/book/{id}` exactly as
    for a new book. One edit per book may run at a time (409 otherwise).
    """
    user_id = _user_id(session)
    _ensure_book(book_id, user_id, container)
    record = await asyncio.to_thread(container.book_store.load_record, book_id)
    if record is None:
        raise UnknownBook("No book with that id. It may have expired — start a new one.")
    edition = await asyncio.to_thread(_book_edition, container, book_id)

    if action == "about":
        about = _about_from_form(
            edition,
            child_name or record.about.prompt_name,
            about_name or record.about.about_name,
            about_age,
            about_title,
            details,
            record.settings,
            about_photo or record.about.about_photo,
        )
        await asyncio.to_thread(
            lambda: container.book_store.update_about(
                book_id, about, title=edition.title_for(about.prompt_name)
            )
        )
        status_payload = (
            await asyncio.to_thread(container.book_store.load_status, book_id) or {}
        )
        if status_payload.get("status") == BookStatus.COMPLETE:
            await asyncio.to_thread(container.book_runner.rebuild_pdf, book_id)
        return _edit_started(book_id, "about")

    if action == "delete":
        # Stop the job, and let it unwind, before the store disappears: a
        # render that is still running would keep making billable calls, then
        # fail writing into the directory this call is about to remove.
        await container.book_runner.cancel(book_id)
        await asyncio.to_thread(container.book_store.delete, book_id)
        return {"book_id": book_id, "deleted": True}

    # Every action below draws pictures, and each picture is bought as it is
    # drawn. Refusing the whole edit when even one is unaffordable is the
    # friendliest version of that: nothing starts, nothing is charged, and the
    # message names the price and the balance.
    _require_picture_funds(container, user_id)
    payment = _picture_payment(container, user_id, book_id=book_id)
    charge = lambda: container.rate_limiter.allow(_client_key(request), 1)  # noqa: E731

    if action == "cover":
        await container.book_runner.start_edit(
            book_id,
            page_numbers=set(),
            draw_cover=True,
            page_charge=charge,
            picture_payment=payment,
        )
        return _edit_started(book_id, "cover")

    if action == "retry":
        has_cover = await asyncio.to_thread(container.book_store.read_cover, book_id)
        await container.book_runner.start_edit(
            book_id,
            page_numbers=None,
            draw_cover=has_cover is None,
            only_missing=True,
            page_charge=charge,
            picture_payment=payment,
        )
        return _edit_started(book_id, "retry")

    if action == "page":
        page = _page_to_edit(edition, page_number)
        await container.book_runner.start_edit(
            book_id,
            page_numbers={page.number},
            draw_cover=False,
            requested_fixes={page.number: _page_faults(reason)},
            page_charge=charge,
            picture_payment=payment,
        )
        return _edit_started(book_id, "page")

    if action == "character":
        if character_key != "star" and not edition.can_photograph(character_key):
            raise InvalidSettings(
                f"There is no character slot for {character_key!r} — "
                "it cannot be given a photo."
            )
        expected = "" if character_key == "star" else character_key
        reference = await asyncio.to_thread(
            _character_reference, generation_id, container, expected, edition.key
        )
        if character_key == "star":
            await asyncio.to_thread(
                container.book_store.update_identity,
                book_id,
                portrait=reference.portrait,
                photo=reference.photo,
                # Keep the book's own art settings: a replacement portrait
                # made with the form defaults must not silently change the
                # aspect ratio of every page already drawn.
                settings=record.settings,
            )
            await container.book_runner.start_edit(
                book_id,
                page_numbers=None,
                draw_cover=True,
                page_charge=charge,
                picture_payment=payment,
            )
            return _edit_started(book_id, "character")

        # Validate before storing: a character that is photoable in the
        # bible but absent from this book's pages is a 422, and a 422 must
        # not leave a face filed under the book.
        affected = edition.pages_casting(character_key)
        if not affected:
            raise InvalidSettings(f"{character_key} does not appear in this book.")
        await asyncio.to_thread(
            container.book_store.save_character, book_id, character_key, reference
        )
        await container.book_runner.start_edit(
            book_id,
            page_numbers=affected,
            draw_cover=False,
            page_charge=charge,
            picture_payment=payment,
        )
        return _edit_started(book_id, "character")

    raise InvalidSettings(f"Unknown edit action {action!r}.")


def _require_picture_funds(container: Container, user_id: str | None) -> None:
    """Refuse an edit this account cannot pay a single picture for.

    The same rule the runner enforces picture by picture, asked up front so a
    wallet with nothing in it gets a sentence instead of a render that stops
    after its first try. Nothing is charged and nothing is drawn here.
    """
    if container.payments is None or not user_id:
        return
    price = container.wallet.picture_price_cents
    balance = container.wallet.balance_cents(user_id)
    if balance >= price:
        return
    raise PaymentRequired(
        f"Each new picture costs {format_cents(price)} and your wallet has "
        f"{format_cents(balance)}. Add money on your wallet page, then try again."
    )


def _picture_payment(
    container: Container, user_id: str | None, *, book_id: str
) -> Callable[[str], None] | None:
    """The per-picture debit this edit's render is handed, or None when free.

    A deployment without payments, and an unowned book, draw without charge,
    exactly as they do for the book's own fee. With money on, each call takes
    one picture's price before that picture is drawn; the wallet's own lock is
    what makes two pictures racing each other unable to both spend the last
    15¢. The reference is unique per draw, so the ledger shows which picture it
    bought and a job retried with the same reference is not charged twice.
    """
    if container.payments is None or not user_id:
        return None
    price = container.wallet.picture_price_cents
    job = uuid.uuid4().hex
    drawn = itertools.count(1)

    def pay(label: str) -> None:
        try:
            container.wallet.charge_picture(
                user_id,
                reference=f"picture:{book_id}:{job}:{next(drawn)}",
                description=f"{label} — {format_cents(price)}",
            )
        except InsufficientFunds:
            balance = container.wallet.balance_cents(user_id)
            raise OutOfFunds(
                f"Each new picture costs {format_cents(price)} and your wallet has "
                f"{format_cents(balance)}. The pictures already made were kept — "
                "add money on your wallet page, then press Finish this book."
            ) from None

    return pay


def _edit_started(book_id: str, action: str) -> dict[str, object]:
    return {
        "book_id": book_id,
        "action": action,
        "status": BookStatus.RENDERING,
        "status_url": f"/api/book/{book_id}",
    }


def _page_to_edit(edition: StoryEdition, page_number: int | None) -> BookPage:
    if page_number is None:
        raise InvalidSettings("Choose a page to redraw.")
    page = edition.page(page_number)
    if page is None:
        raise InvalidSettings("That page does not exist in this book.")
    return page


def _page_faults(reasons: list[str]) -> tuple[str, ...]:
    """Validate the ticked chips of a page redraw into `PageFault` names.

    A closed set, for the same reason the portrait's chips are: nothing the
    visitor types can reach a prompt. An unknown value is a 422, not a skip.
    """
    try:
        return tuple(dict.fromkeys(PageFault(value).value for value in reasons))
    except ValueError as exc:
        raise InvalidSettings(f"Unknown redraw reason: {exc}") from exc


def _character_reference(
    generation_id: str, container: ContainerDep, expected_cast_key: str, story_key: str
) -> CharacterReference:
    if not generation_id:
        raise InvalidSettings("Choose the replacement photo first.")
    draft = container.store.load_draft(generation_id)
    if draft is None:
        raise UnknownGeneration(generation_id)
    if draft.cast_key != expected_cast_key or draft.story_key != story_key:
        raise InvalidSettings(
            "That portrait was made for a different character — make the "
            "cartoon from this slot's own photo."
        )
    return CharacterReference(
        portrait=draft.cartoon_image,
        portrait_media_type=draft.cartoon_media_type,
        photo=draft.photo_data,
        photo_media_type=draft.photo_media_type,
        generation_id=draft.generation_id,
        settings=draft.settings,
    )


@router.delete("/{book_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_book(
    book_id: str, container: ContainerDep, session: CurrentSessionDep
) -> Response:
    """Delete a book and every artefact that belongs to it."""
    _ensure_book(book_id, _user_id(session), container)
    # Cancel on the event loop, where the job's task lives, and wait for it;
    # only then remove the store (a blocking call, so it goes to a worker
    # thread). A delete that raced the cancellation handler could otherwise
    # have a status file rewritten behind it.
    await container.book_runner.cancel(book_id)
    await asyncio.to_thread(container.book_store.delete, book_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{book_id}")
def book_status(
    book_id: str, container: ContainerDep, session: CurrentSessionDep
) -> dict[str, object]:
    """The book's progress: status, page list and any failure message."""
    return _ensure_book(book_id, _user_id(session), container)


# The finished PDF is served under /book-media like every other book artefact,
# so the status record's pdf_url and this file have exactly one canonical path.


# --- shared helpers -----------------------------------------------------------


def _client_key(request: Request) -> str:
    """The rate-limit key for a request: its client address, or a stand-in."""
    return request.client.host if request.client else "unknown"


def _require_funds(container: Container, user_id: str | None) -> None:
    """Refuse to start a book this account cannot pay for.

    Only when money is on and there is somebody to charge: a password/open
    deployment, and a deployment without a Stripe key, keep making free books.
    The check lives here, after the reuse lookup, so polling or re-submitting
    an already-started book is never refused for money.

    The fee is taken when a book's PDF is written, not here, so the balance
    alone is not enough: a book already being made has money promised to it.
    Counting those books and setting their fees aside is what stops two tabs
    that each see $0.99 from both starting and leaving the ledger a fee short
    (B3). This runs under `_start_lock`, so two requests in this process cannot
    both read the same count before either records its book.
    """
    if container.payments is None or not user_id:
        return
    balance = container.wallet.balance_cents(user_id)
    price = container.wallet.price_cents
    open_books = _open_book_count(container, user_id)
    set_aside = open_books * price
    if balance - set_aside >= price:
        return
    if set_aside:
        noun = "book" if open_books == 1 else "books"
        raise PaymentRequired(
            f"One picture book costs {format_cents(price)} and your wallet has "
            f"{format_cents(balance)}. {format_cents(set_aside)} of that is set "
            f"aside for the {noun} you are already making — wait for it to "
            f"finish, or add money with the + $5 button in the header."
        )
    raise PaymentRequired(
        f"One picture book costs {format_cents(price)} and your wallet has "
        f"{format_cents(balance)}. Add money with the + $5 button in the header, "
        "or on your wallet page, then start the book again."
    )


#: How many of the owner's books are read to find the ones in flight. Their
#: library is newest first and a book in flight is always among the newest, so
#: this is far above the number anyone can fund at once ($0.99 each).
_OPEN_BOOKS_LOOKUP = 100

_OPEN_BOOK_STATES = frozenset({BookStatus.PENDING, BookStatus.RENDERING})


def _open_book_count(container: Container, user_id: str) -> int:
    """Books this account has started that have not finished or failed.

    Their fee belongs to them even though the ledger has not been debited yet.
    """
    return sum(
        1
        for book in container.book_store.list_for_owner(user_id, limit=_OPEN_BOOKS_LOOKUP)
        if book.status in _OPEN_BOOK_STATES
    )


def _check_rate(client_key: str, container: Container, tokens: int = 1) -> None:
    if not container.rate_limiter.allow(client_key, tokens):
        raise GenerationRateLimited(
            "This client has reached the image-generation limit for this minute. "
            "A book costs one billable image per page, so it takes a few "
            "minutes of allowance. Please wait and try again."
        )


def _about_from_form(
    edition: StoryEdition,
    child_name: str,
    about_name: str,
    about_age: int | None,
    about_title: str | None,
    details: str,
    settings: CartoonSettings,
    about_photo: str = AboutPhotoChoice.CARTOON,
) -> AboutMe:
    """Resolve blank names and validate the uploader's words.

    A blank prompt name or About name becomes the story's default star name
    — decided by the photo-gender answer the settings carry — rather than
    praying to a required field. Exactly one resolver lives here: the model
    receives an object whose names are always set.

    The text itself is free — it is drawn on the PDF, never sent to a model —
    but a malformed value fails here with a 422 naming the field rather than
    surfacing as a blank About page later.
    """
    resolved = book_name_for(settings, edition)
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
                "about_photo": about_photo,
            }
        )
    except ValidationError as exc:
        raise InvalidSettings(f"The About page details did not validate: {exc}") from exc


def _with_page_shape(
    settings: CartoonSettings, aspect_ratio: str, edition: StoryEdition
) -> CartoonSettings:
    """The star's settings with the book-wide page shape and the story's rules applied.

    The page shape is chosen once for the whole book, on the About screen, so
    it overrides whatever shape the star's portrait draft was made with. A
    blank value keeps the draft's own shape (older clients send none). A
    plated story has one shape, its plates': asking for another is refused
    rather than quietly ignored. The story's rules are applied again here
    because plates may have been approved since the portrait was drawn.
    """
    rules = story_rules(edition)
    if aspect_ratio:
        try:
            shape = AspectRatio(aspect_ratio)
        except ValueError as exc:
            raise InvalidSettings(
                f"Unknown page shape {aspect_ratio!r}. "
                f"Valid values: {', '.join(ratio.value for ratio in AspectRatio)}."
            ) from exc
        if rules.page_shape is not None and shape != rules.page_shape:
            raise InvalidSettings(
                "This story's pictures are painted landscape, so its pages are landscape."
            )
        settings = settings.model_copy(update={"aspect_ratio": shape})
    return rules.fit(settings)


def _character_references(
    character: list[str], container: ContainerDep, edition: StoryEdition
) -> dict[str, CharacterReference]:
    """Resolve the repeated `character=key:generation_id` form parts.

    Each pair names a photoable cast member and the approved cartoon portrait
    the uploader made for them with `/api/cartoonify` — the same pass 1 the
    book's own portrait went through, one call per character. The portrait
    draft supplies both halves of the reference the pages get: the cartoon
    portrait and the original photograph behind it.

    Refused outright:

    * a key that is not a photoable supporting character of this story — the
      wolf above all: he is pinned in prose, and there is no route, form or
      endpoint that can attach a face to him;
    * the same key twice — two photos of one character is ambiguous, and the
      later one would silently win;
    * a portrait made for a different slot, the star, or another story — the
      draft records the cast key and story it was made for, so the mother's
      face cannot be filed as the giant's, nor Jack's mother as Red's;
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
        if not edition.can_photograph(key):
            # Covers both an unknown key and a real character that is not
            # photoable (the wolf): neither has an upload path.
            raise InvalidSettings(
                f"There is no character slot for {key!r} — it cannot be given a photo."
            )
        if key in references:
            raise InvalidSettings(f"{key} was given more than one photo.")
        draft: Draft | None = container.store.load_draft(generation_id)
        if draft is None:
            raise UnknownGeneration(generation_id)
        if draft.cast_key != key or draft.story_key != edition.key:
            raise InvalidSettings(
                f"That portrait was not made for {key} — make the cartoon from "
                f"{key}'s own photo, with the character slot selected."
            )
        references[key] = CharacterReference(
            portrait=draft.cartoon_image,
            portrait_media_type=draft.cartoon_media_type,
            photo=draft.photo_data,
            photo_media_type=draft.photo_media_type,
            generation_id=draft.generation_id,
            settings=draft.settings,
        )
    return references
