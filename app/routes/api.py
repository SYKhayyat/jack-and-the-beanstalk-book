"""JSON API: the settings catalogue and the book's portrait pass."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Annotated, TypeVar

from fastapi import APIRouter, Depends, File, Request, UploadFile, status

from app.dependencies import ContainerDep
from app.errors import GenerationRateLimited, GenerationTimeout, ServerBusy
from app.models import (
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
    SkinTone,
)
from app.models.labels import choices_for
from app.routes.settings_form import cartoon_settings_from_form
from app.services.concurrency import GateTimeout
from app.services.images import InvalidImage, decode_upload
from app.services.storage import Draft, StoredGeneration

SettingsDep = Annotated[CartoonSettings, Depends(cartoon_settings_from_form)]

_T = TypeVar("_T")

__all__ = ["router"]


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["api"])


@router.get("/settings-options")
def list_settings_options() -> dict[str, object]:
    """Every enum the UI may offer, plus the default settings object."""
    return {
        "cartoon_style": _options(CartoonStyle),
        "aspect_ratio": _options(AspectRatio),
        "apparent_age": _options(ApparentAge),
        "character_gender": _options(CharacterGender),
        "photo_gender": _options(PhotoGender),
        "skin_tone": _options(SkinTone),
        "likeness_strength": _options(LikenessStrength),
        "portrait_background": _options(PortraitBackground),
        "outfit": _options(Outfit),
        "hair_style": _options(HairStyle),
        "hair_color": _options(HairColour),
        "expression": _options(Expression),
        "defaults": CartoonSettings().model_dump(mode="json"),
        "limits": {"cartoon_intensity": {"min": 1, "max": 10}},
    }


@router.post("/cartoonify", status_code=status.HTTP_201_CREATED)
async def cartoonify(
    request: Request,
    container: ContainerDep,
    settings: SettingsDep,
    photo: UploadFile = File(..., description="A photo of a person. JPEG, PNG or WEBP."),
) -> dict[str, object]:
    """Turn the upload into a cartoon portrait and stop there.

    This is the book's portrait pass: the user sees and judges the likeness
    before the book pages (the expensive part) are rendered. Re-calling this
    with tweaked settings is one call; starting the book is what costs the
    page calls.
    """
    _check_generation_rate(request, container, tokens=settings.candidates)
    raw = await _read_upload(photo, container)
    pipeline = container.pipeline
    budget = container.settings.generation_timeout_seconds

    def work() -> Draft:
        decoded = decode_upload(raw)
        return pipeline.cartoonify(
            photo_data=decoded.data,
            photo_media_type=decoded.media_type,
            settings=settings,
        )

    draft = await _run_gated(container, work, "cartoonify")
    return _cartoon_payload(draft, settings, budget)


# --- shared helpers ---------------------------------------------------------


def _check_generation_rate(request: Request, container: ContainerDep, tokens: int = 1) -> None:
    """Admit the request (or refuse it) and bill its images at once.

    `tokens` is the number of billable images this request will cost —
    candidate portraits included. Counting images rather than requests is
    what keeps the per-minute number honest.
    """
    client_key = request.client.host if request.client else "unknown"
    if not container.rate_limiter.allow(client_key, tokens):
        raise GenerationRateLimited(
            "This client has reached the image-generation limit for this minute. "
            "Please wait and try again."
        )


async def _read_upload(photo: UploadFile, container: ContainerDep) -> bytes:
    """Read and size-check the upload, before anything decodes it.

    ``UploadFile.read()`` with no size reads the whole multipart part into RAM.
    Read in bounded chunks and stop as soon as the configured limit is crossed.
    """
    limit = container.settings.max_upload_bytes
    chunks: list[bytes] = []
    total = 0
    while total <= limit:
        chunk = await photo.read(min(1024 * 1024, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise InvalidImage(
                f"That photo is larger than the {container.settings.max_upload_mb}MB limit."
            )

    raw = b"".join(chunks)
    if not raw:
        raise InvalidImage("No photo was uploaded.")
    return raw


async def _run_gated(
    container: ContainerDep,
    work: Callable[[], _T],
    label: str,
) -> _T:
    """Run blocking work on a worker thread under the gate and the time budget.

    One budget covers the wait for a slot and the work itself, not one each.
    The abort belongs to the provider's own HTTP timeout, so the task is
    shielded; the worker keeps the slot until it genuinely finishes.
    """
    budget = container.settings.generation_timeout_seconds
    deadline = asyncio.get_running_loop().time() + budget

    try:
        async with container.gate.lease(timeout=budget) as slot:
            task = asyncio.create_task(asyncio.to_thread(work))
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            try:
                return await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
            except TimeoutError as exc:
                slot.transfer()
                task.add_done_callback(lambda _finished: slot.release())
                logger.warning("api.%s_timeout budget=%.0fs", label, budget)
                raise GenerationTimeout(
                    f"Generation did not finish within {budget:.0f} seconds. "
                    "The image model was slower than expected — try again, or use "
                    "a simpler style."
                ) from exc
    except GateTimeout as exc:
        raise ServerBusy(str(exc)) from exc


def _cartoon_payload(
    draft: Draft,
    settings: CartoonSettings,
    budget: float,
) -> dict[str, object]:
    """Response for the portrait pass: the portrait, its alternatives, the ids
    the book-start call needs."""
    stored = StoredGeneration(Path(), draft.generation_id)
    urls = stored.to_urls()
    return {
        "generation_id": draft.generation_id,
        "stage": "cartoonified",
        "provider": draft.provider,
        "model": draft.model,
        "settings": settings.model_dump(mode="json"),
        "prompts": {"cartoon": draft.cartoon_prompt},
        "images": {
            "cartoon_url": urls["cartoon"],
            "candidate_urls": [
                stored.candidate_url(index) for index in range(len(draft.candidates))
            ],
        },
        "candidate_count": len(draft.candidates),
        "provider_call_count": 1,
        "billed_image_count": len(draft.candidates),
        "timeout_budget_seconds": budget,
    }


def _options(enum_cls: type[StrEnum]) -> list[dict[str, str]]:
    """Enum values as `{value, label}` pairs, with the same labels the page uses."""
    return [{"value": value, "label": label} for value, label in choices_for(enum_cls)]
