"""Server-rendered pages: the shelf, the studio, the About screen, the book.

One flow, one screen per step (``docs/ui-redesign-plan.md``):

    /                               the shelf — choose a book
    /stories/{story}/studio         give the characters faces
    /stories/{story}/about          say who it is for, choose the page shape
    /books/{book_id}                watch it being drawn, then read and keep it

Every page shares one header (wordmark, My books, wallet, sign out) built by
`_chrome`, so the masthead cannot drift between screens.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.dependencies import ContainerDep
from app.models import (
    ApparentAge,
    AspectRatio,
    CartoonSettings,
    CartoonStyle,
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
from app.routes.auth import CurrentSessionDep
from app.routes.book_access import book_edition
from app.services.book_store import BookStatus, BookSummary
from app.services.pictures import picture_set
from app.services.prompts import render_book_page
from app.services.typography import book_text
from app.services.wallet import format_cents
from app.stories import NOT_READY, Story, shelf_story
from app.stories import shelf as shelf_cards

__all__ = ["page_chrome", "router"]

router = APIRouter(tags=["web"])

#: The page shapes the About screen offers, in the order a person thinks of them.
_PAGE_SHAPES = (
    (AspectRatio.LANDSCAPE, "Landscape"),
    (AspectRatio.PORTRAIT, "Square"),
    (AspectRatio.TALL, "Portrait"),
    (AspectRatio.CINEMATIC, "Wide"),
)

_ABOUT_PHOTO_LABELS = (
    (AboutPhotoChoice.CARTOON, "The cartoon"),
    (AboutPhotoChoice.PHOTO, "The photo"),
    (AboutPhotoChoice.BOTH, "Both"),
)

_IN_PROGRESS = frozenset({BookStatus.PENDING, BookStatus.RENDERING})


@router.get("/", response_class=HTMLResponse)
def shelf(
    request: Request, container: ContainerDep, session: CurrentSessionDep
) -> HTMLResponse:
    """The shelf: one card per story, each a single big way in."""
    chrome = page_chrome(container, session)
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
    """Who's in the story: one collapsible section per character, star first."""
    story, edition = _startable_or_404(container, story_key)
    defaults = CartoonSettings()
    sections = [
        {"key": "star", "label": "The star", "required": True},
        *(
            {"key": member.key, "label": member.label, "required": False}
            for member in edition.photoable_cast
        ),
    ]
    return _render(
        request,
        "studio.html",
        {
            **page_chrome(container, session),
            "story": story,
            "sections": sections,
            # A tale whose star wears the story's own costume (the red hood)
            # has no outfit to choose.
            "star_costume": bool(edition.star_member.costume),
            "defaults": defaults,
            "photo_genders": choices_for(PhotoGender),
            "styles": choices_for(CartoonStyle),
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
    """The last page is about the star: words, the picture choice, the shape."""
    story, edition = _startable_or_404(container, story_key)
    chrome = page_chrome(container, session)
    return _render(
        request,
        "about_page.html",
        {
            **chrome,
            "story": story,
            "page_shapes": _PAGE_SHAPES,
            "default_shape": CartoonSettings().aspect_ratio,
            "about_photo_choices": _ABOUT_PHOTO_LABELS,
            "cast": [
                ("star", "Star"),
                *((member.key, member.label) for member in edition.photoable_cast),
            ],
            "fee_label": format_cents(container.wallet.price_cents)
            if chrome["wallet_enabled"]
            else "",
        },
    )


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
    heading, *sentences = (book_text(line) for line in record.about.rendered_about_lines())
    chrome = page_chrome(container, session)
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
            "phase": status_payload.get("phase") or "",
            "tiles": tiles,
            "done_count": sum(1 for tile in tiles if tile["status"] == "done"),
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
            # Edits are always free; only a deployment that charges says why.
            "edit_note": "Changes are included — the book's fee was taken once."
            if chrome["wallet_enabled"]
            else "Changes are free: redraw a page, the cover or a face as often as you need.",
            "pdf_url": f"/book-media/{book_id}/pdf" if state == BookStatus.COMPLETE else None,
            "sources_url": f"/book-media/{book_id}/sources.zip",
            "characters": [(member.key, member.label) for member in edition.photoable_cast],
            "page_faults": _page_faults(),
            "about_photo_choices": _ABOUT_PHOTO_LABELS,
            "max_upload_mb": container.settings.max_upload_mb,
            "max_attempts": container.settings.page_attempts,
        },
    )


@router.get("/healthz")
def healthz(container: ContainerDep) -> dict[str, object]:
    """Liveness, whether the pictures are real, and the concurrency ceiling.

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


def page_chrome(container: ContainerDep, session: CurrentSessionDep) -> dict[str, Any]:
    """What the header and footer need on every screen.

    Google mode files each book under a person, so My books exists there.
    Password/open mode has no identity and deliberately offers no shared pile
    of everybody's books.
    """
    owner = (session.user_id if session else None) or ""
    wallet_enabled = container.payments is not None and bool(owner)
    return {
        "auth_enabled": container.settings.auth_enabled,
        "signed_in": session is not None,
        "user_name": session.name if session else "",
        "provider": container.provider_status(),
        "library_enabled": bool(owner),
        "books": container.book_store.list_for_owner(owner) if owner else [],
        "wallet_enabled": wallet_enabled,
        "wallet_balance": format_cents(container.wallet.balance_cents(owner))
        if wallet_enabled
        else "",
    }


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
    newest = next((book for book in books if book.cover_url), None)
    if newest is not None:
        card.update(
            cover_url=newest.cover_url,
            cover_title=newest.title,
            cover_subtitle=newest.subtitle,
            cover_sha256=newest.cover_sha256,
        )
    return card


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
                "label": "Cover" if number == COVER_NUMBER else f"Page {number}",
                "short": "C" if number == COVER_NUMBER else str(number),
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
