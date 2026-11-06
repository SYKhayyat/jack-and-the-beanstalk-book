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
from app.models.book import BOOK_PAGES, AboutPhotoChoice, BookPage
from app.models.feedback import REJECTION_LABELS, RejectionReason
from app.models.labels import choices_for, intensity_label
from app.routes.auth import CurrentSessionDep
from app.services.book_document import COVER_NUMBER
from app.services.book_store import BookStatus, BookSummary
from app.services.prompts import photoable_cast, render_book_page
from app.services.wallet import format_cents
from app.stories import STORIES, Story, story_for

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
        {**chrome, "cards": [_card(story, books) for story in STORIES]},
    )


@router.get("/stories/{story_key}/studio", response_class=HTMLResponse)
def studio(
    story_key: str, request: Request, container: ContainerDep, session: CurrentSessionDep
) -> HTMLResponse:
    """Who's in the story: one collapsible section per character, star first."""
    story = _story_or_404(story_key)
    defaults = CartoonSettings()
    sections = [
        {"key": "star", "label": "The star", "required": True},
        *({"key": key, "label": label, "required": False} for key, label in photoable_cast()),
    ]
    return _render(
        request,
        "studio.html",
        {
            **page_chrome(container, session),
            "story": story,
            "sections": sections,
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
    story = _story_or_404(story_key)
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
            "cast": [("star", "Star"), *photoable_cast()],
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
    rendered = [
        render_book_page(page, record.settings, prompt_name=record.about.prompt_name)
        for page in BOOK_PAGES
    ]
    tiles = _tiles(status_payload.get("pages", []), rendered)
    heading, *sentences = record.about.rendered_about_lines()
    return _render(
        request,
        "book.html",
        {
            **page_chrome(container, session),
            "current_id": book_id,
            "book": record,
            "about": record.about,
            "status": state,
            "error": status_payload.get("error") or "",
            "phase": status_payload.get("phase") or "",
            "tiles": tiles,
            "done_count": sum(1 for tile in tiles if tile["status"] == "done"),
            "about_heading": heading,
            "about_sentences": sentences,
            "about_images": _about_images(book_id, record.about.about_photo),
            "pdf_url": f"/book-media/{book_id}/pdf" if state == BookStatus.COMPLETE else None,
            "sources_url": f"/book-media/{book_id}/sources.zip",
            "characters": photoable_cast(),
            "rejection_reasons": _rejection_reasons(),
            "about_photo_choices": _ABOUT_PHOTO_LABELS,
            "max_upload_mb": container.settings.max_upload_mb,
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


def _story_or_404(key: str) -> Story:
    story = story_for(key)
    if story is None:
        raise HTTPException(status_code=404, detail="There is no book by that name yet.")
    return story


def _rejection_reasons() -> list[dict[str, str]]:
    return [
        {"value": reason.value, "label": REJECTION_LABELS[reason]}
        for reason in RejectionReason
    ]


def _card(story: Story, books: list[BookSummary]) -> dict[str, Any]:
    """A shelf card and its one honest state.

    Every stored book is Jack's until the story catalogue lands, so a person's
    books only ever change Jack's card. "Keep going" is decided in the browser
    (the studio draft lives there) and only ever replaces "Start this book" or
    "Make another".
    """
    card: dict[str, Any] = {
        "story": story,
        "state": "fresh" if story.available else "soon",
        "href": f"/stories/{story.key}/studio" if story.available else None,
        "cover_url": None,
    }
    if not story.available or story.key != "jack_and_the_beanstalk":
        return card
    drawing = next((book for book in books if book.status in _IN_PROGRESS), None)
    finished = next((book for book in books if book.status == BookStatus.COMPLETE), None)
    if drawing is not None:
        card.update(state="drawing", href=f"/books/{drawing.book_id}")
    elif finished is not None:
        card["state"] = "finished"
    newest_cover = next((book.cover_url for book in books if book.cover_url), None)
    card["cover_url"] = newest_cover
    return card


def _tiles(raw: object, rendered: list[BookPage]) -> list[dict[str, Any]]:
    """The 20 drawn pictures in book order — cover first — with their state.

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
                "story": entry.get("story", story) or "",
                "status": status,
                "url": entry.get("url") if status == "done" else None,
            }
        )
    return tiles


def _about_images(book_id: str, choice: AboutPhotoChoice) -> list[dict[str, str]]:
    photo = {"url": f"/book-media/{book_id}/photo", "alt": "The original photo"}
    cartoon = {"url": f"/book-media/{book_id}/portrait", "alt": "The cartoon portrait"}
    if choice == AboutPhotoChoice.BOTH:
        return [photo, cartoon]
    if choice == AboutPhotoChoice.PHOTO:
        return [photo]
    return [cartoon]
