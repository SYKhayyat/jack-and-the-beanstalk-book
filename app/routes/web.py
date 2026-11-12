"""Server-rendered pages: the shelf, the studio, the About screen, the book.

One flow, one screen per step (``docs/ui-redesign-plan.md``):

    /                               the shelf — choose a book
    /stories/{story}/studio         give the characters faces
    /stories/{story}/about          say who it is for
    /books/{book_id}                watch it being drawn, then read and keep it

Every page shares one header (wordmark, My books, wallet, sign out) built by
`_chrome`, so the masthead cannot drift between screens.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.container import Container
from app.dependencies import ContainerDep
from app.models import (
    ApparentAge,
    Expression,
    HairColour,
    HairStyle,
    LikenessStrength,
    Outfit,
    PhotoGender,
    PortraitBackground,
    SkinTone,
)
from app.models.book import AboutPhotoChoice, BookPage
from app.models.feedback import PAGE_FAULT_LABELS, REJECTION_LABELS, PageFault, RejectionReason
from app.models.labels import choices_for, intensity_label
from app.models.story import COVER_NUMBER, StoryEdition
from app.prompt_data import prompt_text
from app.routes.auth import CurrentSessionDep
from app.routes.book_access import book_edition
from app.services.book_store import BookStatus, BookSummary
from app.services.pictures import picture_set
from app.services.prompts import render_book_page
from app.services.story_settings import story_rules
from app.services.typography import book_text
from app.services.wallet import REFUND_REQUESTED, format_cents
from app.stories import NOT_READY, Story, shelf_story
from app.stories import shelf as shelf_cards

__all__ = ["page_chrome", "router"]

router = APIRouter(tags=["web"])

logger = logging.getLogger(__name__)

_ABOUT_PHOTO_LABELS = (
    (AboutPhotoChoice.CARTOON, "The cartoon"),
    (AboutPhotoChoice.PHOTO, "The photo"),
    (AboutPhotoChoice.BOTH, "Both"),
)

_IN_PROGRESS = frozenset({BookStatus.PENDING, BookStatus.RENDERING})

#: What a wallet return may say. A closed set: `message` arrives in a query
#: string, so it can only select a sentence this app already wrote. Shared by
#: every screen, because a top-up can start from any screen and return to it.
WALLET_MESSAGES = {
    "funded": "Your top-up arrived — the balance is up to date.",
    "cancelled": "No payment was taken.",
    "payment_failed": "That payment did not go through. No money was taken.",
    "payments_off": "Payments are not configured on this deployment.",
    "refund_requested": (
        "Your refund request is in. It goes back to the card that paid once "
        "the operator sends it."
    ),
    "nothing_to_refund": "There is nothing to refund right now.",
}


@router.get("/", response_class=HTMLResponse)
def shelf(
    request: Request, container: ContainerDep, session: CurrentSessionDep
) -> HTMLResponse:
    """The shelf: one card per story, each a single big way in."""
    chrome = page_chrome(container, session, request)
    books: list[BookSummary] = chrome["books"]
    return _render(
        request,
        "shelf.html",
        {
            **chrome,
            "cards": [_card(story, books) for story in shelf_cards(container.templates)],
        },
    )


@router.get("/stories/{story_key}/studio", response_class=HTMLResponse)
def studio(
    story_key: str, request: Request, container: ContainerDep, session: CurrentSessionDep
) -> HTMLResponse:
    """The first three steps: who the star is, their portrait, the story's friends.

    One decision per step (UI.org G2). The name comes first, with the cover it
    goes on, and the boy/girl answer has no default (Q2). Settings the story
    decides are not offered at all (U5): the style always, the outfit when the
    tale has a costume, and the background once the story has plates.
    """
    story, edition = _startable_or_404(container, story_key)
    rules = story_rules(edition)
    sections = [
        {"key": "star", "label": "The star", "required": True},
        *(
            {"key": member.key, "label": member.label, "required": False}
            for member in edition.photoable_cast
        ),
    ]
    sample = story.samples[0]
    return _render(
        request,
        "studio.html",
        {
            **page_chrome(container, session, request),
            "story": story,
            "sections": sections,
            "steps": _steps(edition),
            "sample_cover": sample,
            "cover_words": {
                "title": edition.book_title,
                "byline": edition.cover_subtitle
                or str(prompt_text()["book"]["about"]["credit"]).replace(
                    "{about_name}", "{name}"
                ),
                "names": dict(edition.names),
            },
            "locked": set(rules.locked),
            "defaults": rules.defaults(),
            "photo_genders": choices_for(PhotoGender),
            "intensities": [(level, intensity_label(level)) for level in range(1, 11)],
            "ages": choices_for(ApparentAge),
            "skin_tones": choices_for(SkinTone),
            "outfits": choices_for(Outfit),
            "hair_styles": choices_for(HairStyle),
            "hair_colours": choices_for(HairColour),
            "expressions": choices_for(Expression),
            "likeness_levels": choices_for(LikenessStrength),
            "backgrounds": choices_for(PortraitBackground),
            "rejection_reasons": _rejection_reasons(),
            "max_upload_mb": container.settings.max_upload_mb,
        },
    )


@router.get("/stories/{story_key}/about", response_class=HTMLResponse)
def about_screen(
    story_key: str, request: Request, container: ContainerDep, session: CurrentSessionDep
) -> HTMLResponse:
    """The last step: the words for the last page, its picture, and Make the book.

    There is no page-shape choice: every page is the plates' shape.
    """
    story, edition = _startable_or_404(container, story_key)
    chrome = page_chrome(container, session, request)
    return _render(
        request,
        "about_page.html",
        {
            **chrome,
            "story": story,
            "steps": _steps(edition),
            "names": dict(edition.names),
            "about_photo_choices": _ABOUT_PHOTO_LABELS,
            "cast": [
                ("star", "Star"),
                *((member.key, member.label) for member in edition.photoable_cast),
            ],
            "fee_label": chrome["price_label"],
            "balance_label": chrome["wallet_balance"],
            "wallet_short": chrome["wallet_short"],
        },
    )


def _steps(edition: StoryEdition) -> list[tuple[str, str]]:
    """The making-a-book steps, as (key, label); "friends" only when there are any."""
    return [
        ("name", "Name"),
        ("photo", "Photo"),
        *([("friends", "Story friends")] if edition.photoable_cast else []),
        ("make", "Make it"),
    ]


@router.get("/books/{book_id}", response_class=HTMLResponse)
def book_page(
    book_id: str, request: Request, container: ContainerDep, session: CurrentSessionDep
) -> HTMLResponse:
    """The book: drawn live, flippable, then downloadable and editable."""
    owner = session.user_id if session else None
    stored_owner = container.book_store.owner_of(book_id)
    if stored_owner and stored_owner != (owner or ""):
        raise HTTPException(status_code=404, detail="No book with that id.")
    record = container.book_store.load_record(book_id)
    status_payload = container.book_store.load_status(book_id)
    if record is None or status_payload is None:
        raise HTTPException(status_code=404, detail="No book with that id.")

    state = str(status_payload.get("status", BookStatus.PENDING))
    edition = book_edition(container, book_id)
    rendered = [
        render_book_page(page, record.settings, prompt_name=record.about.prompt_name)
        for page in edition.pages
    ]
    tiles = _tiles(status_payload.get("pages", []), rendered)
    expires_at, expiring_soon = _expiry(
        record.created_at, container.settings.book_retention_days
    )
    heading, *sentences = (book_text(line) for line in record.about.rendered_about_lines())
    chrome = page_chrome(container, session, request)
    return _render(
        request,
        "book.html",
        {
            **chrome,
            "current_id": book_id,
            "book": record,
            "title": record.title,
            "subtitle": edition.subtitle_for(record.about.prompt_name),
            "about": record.about,
            "status": state,
            "error": status_payload.get("error") or "",
            # Why it stopped, in one word: the reader shows the wallet notice
            # for `out_of_funds` on first paint, before any poll (B4).
            "error_code": status_payload.get("error_code") or "",
            "phase": status_payload.get("phase") or "",
            "tiles": tiles,
            "done_count": sum(1 for tile in tiles if tile["status"] == "done"),
            # A book can be complete with a picture that could not be drawn
            # (B2); the finished block offers to finish it in one press.
            "missing_count": sum(1 for tile in tiles if tile["status"] == "failed"),
            "about_heading": heading,
            "about_sentences": sentences,
            "about_images": _about_images(
                book_id,
                record.about.about_photo,
                container.book_store.picture_sha(book_id, "portrait"),
            ),
            # The page's shape, until the first picture says exactly (the
            # reader measures it); CSS takes "4 / 3".
            "art_ratio": record.settings.aspect_ratio.value.replace(":", " / "),
            "book_gender": record.settings.photo_gender.value,
            "story_pages": len(rendered),
            # Each picture is bought as it is drawn; only a deployment that
            # charges has a price to name (the book's own fee was taken once).
            "edit_note": (
                f"Each new picture costs {format_cents(container.wallet.picture_price_cents)}"
                " — a page, the cover, or a face's pages, taken as it is drawn. "
                "The book's own fee was taken once."
            )
            if chrome["wallet_enabled"]
            else "Changes are free: redraw a page, the cover or a face as often as you need.",
            "pdf_url": f"/book-media/{book_id}/pdf" if state == BookStatus.COMPLETE else None,
            "sources_url": f"/book-media/{book_id}/sources.zip",
            # The footer has always promised seven days; this book says which
            # day that is, and raises its voice when the day is close (I6).
            "expires_at": expires_at,
            "expiring_soon": expiring_soon,
            "characters": [(member.key, member.label) for member in edition.photoable_cast],
            "page_faults": _page_faults(),
            "about_photo_choices": _ABOUT_PHOTO_LABELS,
            "max_upload_mb": container.settings.max_upload_mb,
            "max_attempts": container.settings.page_attempts,
        },
    )


def _expiry(created_at: str, days: int) -> tuple[str, bool]:
    """When this book's pictures and PDF are deleted, and whether that is near.

    The same clock the sweep uses (`BOOK_RETENTION_DAYS` from the moment the
    book started), said on the page rather than only in the footer: a promise
    with no date on it is not a clock (I6). "Soon" is the last two days, which
    is when a download is worth nagging about.
    """
    try:
        started = datetime.fromisoformat(created_at)
    except ValueError:
        return "", False
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    expires = started + timedelta(days=days)
    return expires.isoformat(), expires - datetime.now(UTC) < timedelta(days=2)


@router.get("/healthz")
async def healthz(container: ContainerDep) -> dict[str, object]:
    """Liveness, whether the pictures are real, and the concurrency ceiling.

    Async, and touching nothing but the already-built container: a health check
    must not wait in the thread pool behind image work or a database round trip
    (Run 2, 2026-11-11, when a busy render made this take 20-50 s and the
    platform restarted the process).

    This URL is open to anyone, because the platform polls it, so it says only
    that the service is up, whether it is drawing real pictures, and how many
    generations may run at once — the ceiling is operational, not a backend
    detail. Which provider and which model are behind it are the operator's
    business: they live in the startup log and in the deployment's environment,
    where a visitor cannot fetch them.
    """
    status_payload = container.provider_status()
    return {
        "status": "ok",
        "provider": {
            "ready": status_payload["ready"],
            "using_real_api": status_payload["using_real_api"],
            "max_concurrent_generations": status_payload["max_concurrent_generations"],
        },
    }


# --- shared context ------------------------------------------------------------


def _render(request: Request, name: str, context: dict[str, Any]) -> HTMLResponse:
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(request=request, name=name, context=context)


def _waiting_refunds(container: Container) -> int:
    """How many refunds are recorded and not yet sent back; never raises.

    A badge is not worth failing a page for: a deployment without payments has
    none by definition, and a ledger hiccup shows no badge rather than a 500.
    """
    if container.payments is None:
        return 0
    try:
        return sum(
            1
            for item in container.wallet.all_refund_requests(limit=100)
            if item.status == REFUND_REQUESTED
        )
    except Exception:
        logger.warning("wallet.refund_badge_failed", exc_info=True)
        return 0


def page_chrome(
    container: ContainerDep,
    session: CurrentSessionDep,
    request: Request | None = None,
) -> dict[str, Any]:
    """What the header, footer and every screen's money line need.

    Google mode files each book under a person, so My books exists there.
    Password/open mode has no identity and deliberately offers no shared pile
    of everybody's books. The wallet itself is shown whenever there is a
    person to show it to, even if payments are off: a visible balance that
    explains itself beats a disappearing pill nobody can find again.
    """
    owner = (session.user_id if session else None) or ""
    payments_enabled = container.payments is not None
    wallet_visible = bool(owner)
    balance = container.wallet.balance_cents(owner) if wallet_visible else 0
    wallet_enabled = payments_enabled and wallet_visible
    is_operator = bool(
        session
        and container.settings.is_operator_identity(sub=session.sub, email=session.email)
    )
    return {
        "auth_enabled": container.settings.auth_enabled,
        "signed_in": session is not None,
        "user_name": session.name if session else "",
        "user_initials": _initials(session.name or session.email) if session else "",
        "is_operator": is_operator,
        # A refund waits for a human and nothing else nudges them, so every
        # operator page carries the count in the header (W4); /admin/status
        # opens with it too, for the page that has to be opened to act.
        "waiting_refunds": _waiting_refunds(container) if is_operator else 0,
        "provider": container.provider_status(),
        "library_enabled": bool(owner),
        "books": container.book_store.list_for_owner(owner) if owner else [],
        # Money is on and this person has a wallet that can be funded.
        "wallet_enabled": wallet_enabled,
        # The balance pill is visible even when money is off; only the quick
        # top-up button depends on payments being configured.
        "wallet_visible": wallet_visible,
        "payments_enabled": payments_enabled,
        "wallet_balance": format_cents(balance) if wallet_visible else "",
        "wallet_balance_cents": balance,
        "wallet_short": wallet_enabled and balance < container.wallet.price_cents,
        # The price of a book, independent of who is looking: it belongs on
        # the public shelf, where the decision to start is made (U3). Payment
        # is a separate question from whether the shelf may state the fee.
        "book_price": format_cents(container.wallet.price_cents) if payments_enabled else "",
        "price_label": format_cents(container.wallet.price_cents) if wallet_enabled else "",
        # What one picture drawn on request costs. The book's own pictures are
        # inside the fee, so this is the number a portrait or a redraw shows.
        "picture_price": format_cents(container.wallet.picture_price_cents)
        if payments_enabled
        else "",
        "flash": wallet_message(request.query_params.get("message", ""))
        if request is not None
        else "",
    }


def wallet_message(raw: str) -> str:
    """One of this app's own wallet sentences, or nothing."""
    return WALLET_MESSAGES.get(raw, "")


def _startable_or_404(container: ContainerDep, key: str) -> tuple[Story, StoryEdition]:
    """The story a studio or About screen is for, and what a book of it holds.

    A story the start gate is holding back has no studio yet: its card says
    so, and a hand-typed URL gets the same sentence rather than a studio whose
    first portrait would be refused.
    """
    story = shelf_story(container.templates, key)
    if story is None:
        raise HTTPException(status_code=404, detail="There is no book by that name yet.")
    if not story.ready:
        raise HTTPException(status_code=409, detail=NOT_READY)
    return story, container.templates.latest_edition(key)


def _rejection_reasons() -> list[dict[str, str]]:
    return [
        {"value": reason.value, "label": REJECTION_LABELS[reason]}
        for reason in RejectionReason
    ]


def _page_faults() -> list[dict[str, str]]:
    """The chips of a page redraw: what can be wrong with a finished page."""
    return [{"value": fault.value, "label": PAGE_FAULT_LABELS[fault]} for fault in PageFault]


def _card(story: Story, books: list[BookSummary]) -> dict[str, Any]:
    """A shelf card and its one honest state.

    A person's books change only their own story's card. "Keep going" is
    decided in the browser (the studio draft lives there) and only ever
    replaces "Start this book" or "Make another". A story the start gate is
    holding back is "waiting": no way in, and one sentence saying why.
    """
    if not story.available:
        state = "soon"
    elif not story.ready:
        state = "waiting"
    else:
        state = "fresh"
    card: dict[str, Any] = {
        "story": story,
        "state": state,
        "href": f"/stories/{story.key}/studio" if state == "fresh" else None,
        "cover_url": None,
        "cover_title": None,
        "cover_subtitle": "",
        "cover_sha256": None,
        #: Every finished cover this person has of this story, newest first.
        #: The shelf card turns through them; the first is also `cover_url`.
        "covers": [],
    }
    if state != "fresh":
        return card
    books = [book for book in books if book.story_key == story.key]
    drawing = next((book for book in books if book.status in _IN_PROGRESS), None)
    finished = next((book for book in books if book.status == BookStatus.COMPLETE), None)
    if drawing is not None:
        card.update(state="drawing", href=f"/books/{drawing.book_id}")
    elif finished is not None:
        card["state"] = "finished"
    card["covers"] = [
        {
            "url": book.cover_url,
            "title": book.title,
            "subtitle": book.subtitle,
            "book_id": book.book_id,
            "sha256": book.cover_sha256,
        }
        for book in books
        if book.cover_url
    ]
    newest = next((book for book in books if book.cover_url), None)
    if newest is not None:
        card.update(
            cover_url=newest.cover_url,
            cover_title=newest.title,
            cover_subtitle=newest.subtitle,
            cover_sha256=newest.cover_sha256,
        )
    return card


def _initials(name: str) -> str:
    """Two letters for the header avatar; a star when there is no name."""
    parts = [part for part in name.replace("@", " ").split() if part]
    if not parts:
        return "✦"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _tiles(raw: object, rendered: list[BookPage]) -> list[dict[str, Any]]:
    """Every drawn picture in book order — cover first — with its state.

    The status document may be empty (a book that has just started) or
    partial; every picture the book will have gets a tile regardless, so the
    map always shows the whole book.
    """
    stored: dict[int, dict[str, Any]] = {}
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict) and isinstance(entry.get("number"), int):
                stored[entry["number"]] = entry
    tiles: list[dict[str, Any]] = []
    for number, title, story in (
        (COVER_NUMBER, "Cover", ""),
        *((page.number, page.scene.title, page.story) for page in rendered),
    ):
        entry = stored.get(number, {})
        status = str(entry.get("status") or "pending")
        tiles.append(
            {
                "number": number,
                # The cover is page 1 (the PDF prints story page n as n + 1),
                # so the map's story labels carry the same number as the
                # folio and the reader bar (B7).
                "label": "Cover" if number == COVER_NUMBER else f"Page {number + 1}",
                "short": "C" if number == COVER_NUMBER else str(number + 1),
                "title": entry.get("title") or title,
                "story": book_text(str(entry.get("story", story) or "")),
                "status": status,
                "url": entry.get("url") if status == "done" else None,
                # Sized, versioned URLs (`app.services.pictures`), stamped on
                # the status document from each picture's row.
                "src": entry.get("src") or entry.get("url") if status == "done" else None,
                "srcset": entry.get("srcset") or "" if status == "done" else "",
                "thumb": entry.get("thumb") or entry.get("url") if status == "done" else None,
                # The quality loop's record for this picture (see
                # `_with_picture_records`); 0 means none was stored.
                "attempts": _int(entry.get("attempts")),
                "chosen_attempt": _int(entry.get("chosen_attempt")),
                "best_effort": bool(entry.get("best_effort")),
            }
        )
    return tiles


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _about_images(
    book_id: str, choice: AboutPhotoChoice, portrait_sha: str | None
) -> list[dict[str, str]]:
    """The About page's framed pictures.

    The cartoon is served sized and versioned like every other picture; the
    photograph stays the untouched original, never cached (`no_store`).
    """
    photo = {"url": f"/book-media/{book_id}/photo", "srcset": "", "alt": "The original photo"}
    portrait = picture_set(f"/book-media/{book_id}/portrait", portrait_sha)
    cartoon = {"url": portrait.src, "srcset": portrait.srcset, "alt": "The cartoon portrait"}
    if choice == AboutPhotoChoice.BOTH:
        return [photo, cartoon]
    if choice == AboutPhotoChoice.PHOTO:
        return [photo]
    return [cartoon]
