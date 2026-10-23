"""Server-rendered pages."""

from __future__ import annotations

from fastapi import APIRouter, Request
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
from app.models.labels import choices_for
from app.prompt_data import prompt_text
from app.services.prompts import render_book_page

__all__ = ["router"]

router = APIRouter(tags=["web"])


@router.get("/", response_class=HTMLResponse)
def index(request: Request, container: ContainerDep) -> HTMLResponse:
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
            "defaults": defaults,
            "styles": choices_for(CartoonStyle),
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
        },
    )


@router.get("/healthz")
def healthz(container: ContainerDep) -> dict[str, object]:
    """Liveness plus which provider is active. Safe to expose."""
    status_payload = container.provider_status()
    return {"status": "ok", "provider": status_payload}
