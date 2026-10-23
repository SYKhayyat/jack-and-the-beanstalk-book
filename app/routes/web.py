"""Server-rendered pages."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.dependencies import ContainerDep
from app.models import (
    REJECTION_LABELS,
    SCENES,
    ApparentAge,
    AspectRatio,
    CartoonSettings,
    CartoonStyle,
    CharacterGender,
    Expression,
    HairColour,
    HairStyle,
    LikenessStrength,
    Outfit,
    PhotoGender,
    PortraitBackground,
    RejectionReason,
    SkinTone,
)
from app.models.labels import choices_for
from app.prompt_data import prompt_text
from app.services.prompts import render_book_page, render_scene

__all__ = ["router"]

router = APIRouter(tags=["web"])


@router.get("/", response_class=HTMLResponse)
def index(request: Request, container: ContainerDep) -> HTMLResponse:
    """The single-page UI: upload, settings, scene picker, results."""
    templates: Jinja2Templates = request.app.state.templates
    defaults = CartoonSettings()
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            # Rendered with the defaults so the catalogue reads naturally; the
            # chosen name is substituted again when a scene is generated.
            "scenes": [render_scene(scene, defaults) for scene in SCENES],
            "defaults": defaults,
            "styles": _choices(CartoonStyle),
            "aspect_ratios": _choices(AspectRatio),
            "ages": _choices(ApparentAge),
            "genders": _choices(CharacterGender),
            "photo_genders": _choices(PhotoGender),
            "skin_tones": _choices(SkinTone),
            "outfits": _choices(Outfit),
            "hair_styles": _choices(HairStyle),
            "hair_colours": _choices(HairColour),
            "expressions": _choices(Expression),
            "likeness_levels": _choices(LikenessStrength),
            "backgrounds": _choices(PortraitBackground),
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


@router.get("/book", response_class=HTMLResponse)
def book_page(request: Request, container: ContainerDep) -> HTMLResponse:
    """The picture-book page: one child, several pages, one PDF.

    A deliberate fork from the single-scene flow: only the book is made here,
    and the approve step exists to launch the book.
    """
    from app.models.book import BOOK_PAGES

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
            "ages": _choices(ApparentAge),
            "genders": _choices(CharacterGender),
            "photo_genders": _choices(PhotoGender),
            "skin_tones": _choices(SkinTone),
            "outfits": _choices(Outfit),
            "hair_styles": _choices(HairStyle),
            "hair_colours": _choices(HairColour),
            "expressions": _choices(Expression),
            "likeness_levels": _choices(LikenessStrength),
            "aspect_ratios": _choices(AspectRatio),
            "max_upload_mb": container.settings.max_upload_mb,
            "provider": container.provider_status(),
            "auth_enabled": container.settings.auth_enabled,
        },
    )


_choices = choices_for
