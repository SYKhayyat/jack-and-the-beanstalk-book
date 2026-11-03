"""Server-rendered pages."""

from __future__ import annotations

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
from app.models.book import BOOK_PAGES
from app.models.feedback import REJECTION_LABELS, RejectionReason
from app.models.labels import choices_for, intensity_label
from app.prompt_data import prompt_text
from app.routes.auth import CurrentSessionDep
from app.services.prompts import photoable_cast, render_book_page

__all__ = ["router"]

router = APIRouter(tags=["web"])


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request, container: ContainerDep, session: CurrentSessionDep
) -> HTMLResponse:
    """The book's landing page: one photo, one About page, one PDF.

    Everything the visitor fills in is here — no second flow, no cross-links.
    """
    templates: Jinja2Templates = request.app.state.templates
    defaults = CartoonSettings()
    book_data = prompt_text()["book"]
    return templates.TemplateResponse(
        request=request,
        name="book.html",
        context={
            "book_title": book_data["book_title"],
            "pages": [render_book_page(page, defaults) for page in BOOK_PAGES],
            # Optional character slots. Only the photoable cast appears:
            # Milky-White has no upload path anywhere (see prompts.toml).
            "characters": photoable_cast(),
            "defaults": defaults,
            "styles": choices_for(CartoonStyle),
            "intensities": [(level, intensity_label(level)) for level in range(1, 11)],
            "aspect_ratios": choices_for(AspectRatio),
            "ages": choices_for(ApparentAge),
            "photo_genders": choices_for(PhotoGender),
            "skin_tones": choices_for(SkinTone),
            "outfits": choices_for(Outfit),
            "hair_styles": choices_for(HairStyle),
            "hair_colours": choices_for(HairColour),
            "expressions": choices_for(Expression),
            "likeness_levels": choices_for(LikenessStrength),
            "backgrounds": choices_for(PortraitBackground),
            "rejection_reasons": [
                {"value": reason.value, "label": REJECTION_LABELS[reason]}
                for reason in RejectionReason
            ],
            "max_upload_mb": container.settings.max_upload_mb,
            "provider": container.provider_status(),
            "auth_enabled": container.settings.auth_enabled,
            # Who is signed in, when somebody is: the masthead can greet them
            # without asking the store on every render.
            "user_name": session.name if session else "",
            "signed_in": session is not None,
            # Google mode files each book under a person, so the library is
            # available there. Password/open mode has no identity and
            # deliberately offers no shared pile of everybody's books.
            "books": container.book_store.list_for_owner(session.user_id)
            if session and session.user_id
            else [],
            "library_enabled": bool(session and session.user_id),
        },
    )


@router.get("/books/{book_id}", response_class=HTMLResponse)
def book_page(
    book_id: str, request: Request, container: ContainerDep, session: CurrentSessionDep
) -> HTMLResponse:
    """One owned book, with its PDF, pages and edit controls."""
    owner = session.user_id if session else None
    stored_owner = container.book_store.owner_of(book_id)
    if stored_owner and stored_owner != (owner or ""):
        raise HTTPException(status_code=404, detail="No book with that id.")
    record = container.book_store.load_record(book_id)
    status_payload = container.book_store.load_status(book_id)
    if record is None or status_payload is None:
        raise HTTPException(status_code=404, detail="No book with that id.")

    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request=request,
        name="book_detail.html",
        context={
            "book": record,
            "about": record.about,
            "status": status_payload,
            "pages": status_payload.get("pages", []),
            "cover_url": f"/book-media/{book_id}/cover"
            if container.book_store.read_cover(book_id) is not None
            else None,
            "pdf_url": f"/book-media/{book_id}/pdf"
            if status_payload.get("status") == "complete"
            else None,
            "characters": photoable_cast(),
            "rejection_reasons": [
                {"value": reason.value, "label": REJECTION_LABELS[reason]}
                for reason in RejectionReason
            ],
            "user_name": session.name if session else "",
            "signed_in": session is not None,
            "max_upload_mb": container.settings.max_upload_mb,
            "books": container.book_store.list_for_owner(owner) if owner else [],
            "library_enabled": bool(owner),
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
