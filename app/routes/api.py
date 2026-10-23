"""JSON API: scene catalogue and generation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Annotated, TypeVar

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status

from app.dependencies import ContainerDep
from app.errors import (
    GenerationRateLimited,
    GenerationTimeout,
    InvalidRejection,
    ServerBusy,
    UnknownScene,
)
from app.models import (
    SCENES,
    ApparentAge,
    AspectRatio,
    CartoonSettings,
    CartoonStyle,
    CharacterGender,
    GenerationResult,
    LikenessStrength,
    PhotoGender,
    Scene,
    SkinTone,
    get_scene,
)
from app.models.feedback import (
    REJECTION_LABELS,
    RejectionFeedback,
    RejectionReason,
)
from app.models.labels import choices_for
from app.models.settings import (
    Expression,
    HairColour,
    HairStyle,
    Outfit,
    PortraitBackground,
)
from app.routes.settings_form import cartoon_settings_from_form
from app.services.concurrency import GateTimeout
from app.services.images import InvalidImage, decode_upload
from app.services.pipeline import (
    GenerationPipeline,
    PipelineOutcome,
)
from app.services.prompts import render_scene
from app.services.storage import Draft, StoredGeneration

SettingsDep = Annotated[CartoonSettings, Depends(cartoon_settings_from_form)]

_T = TypeVar("_T")

__all__ = ["router"]


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["api"])


@router.get("/scenes")
def list_scenes() -> dict[str, object]:
    """The scenes this app can draw, for the UI to populate.

    Rendered with the default settings (a boy, so Jack) purely so the catalogue
    reads naturally; the real name is substituted at generation time.
    """
    defaults = CartoonSettings()
    return {
        "scenes": [
            {
                "key": scene.key,
                "title": rendered.title,
                "caption": rendered.caption,
                "checklist": list(rendered.must_include),
            }
            for scene in SCENES
            for rendered in (render_scene(scene, defaults),)
        ]
    }


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
        "rejection_reasons": [
            {
                "value": reason.value,
                "label": REJECTION_LABELS[reason],
            }
            for reason in RejectionReason
        ],
        "defaults": CartoonSettings().model_dump(mode="json"),
        "limits": {"cartoon_intensity": {"min": 1, "max": 10}},
    }


@router.post("/cartoonify", status_code=status.HTTP_201_CREATED)
async def cartoonify(
    request: Request,
    container: ContainerDep,
    settings: SettingsDep,
    photo: UploadFile = File(..., description="A photo of a person. JPEG, PNG or WEBP."),
    # Wire name is the singular "reason" because that is what each checkbox in
    # the form is called; it simply repeats. Aliased so the Python side can read
    # as a plural without the two drifting apart.
    reasons: Annotated[list[str] | None, Form(alias="reason")] = None,
) -> dict[str, object]:
    """Pass 1 only: turn the upload into a cartoon portrait and stop there.

    Lets the user see and judge the likeness before paying for the scene pass.
    Re-calling this with tweaked settings is one call; calling `/api/compose`
    afterwards is the only thing that costs a second call.
    """
    _check_generation_rate(request, container, tokens=settings.candidates)
    raw = await _read_upload(photo, container)
    pipeline = container.pipeline
    budget = container.settings.generation_timeout_seconds
    feedback = _rejection_from_form(reasons or [])

    def work() -> Draft:
        decoded = decode_upload(raw)
        return pipeline.cartoonify(
            photo_data=decoded.data,
            photo_media_type=decoded.media_type,
            settings=settings,
            rejection=feedback,
        )

    draft = await _run_gated(container, work, "cartoonify")
    return _cartoon_payload(draft, settings, budget)


@router.post("/compose", status_code=status.HTTP_201_CREATED)
async def compose(
    request: Request,
    container: ContainerDep,
    generation_id: str = Form(
        ..., description="Id returned by /api/cartoonify for the approved portrait."
    ),
    scene: str = Form(..., description="Scene key, e.g. planting-seeds."),
    candidate: int = Form(
        0, ge=0, description="Which portrait alternative to use, counting from 0."
    ),
) -> dict[str, object]:
    """Pass 2 only: put an already-approved cartoon portrait into a scene.

    Costs exactly one call, so trying both scenes with the same portrait is
    half the price of re-running the whole pipeline twice.
    """
    _check_generation_rate(request, container)
    selected = _require_scene(scene)

    outcome = await _run_gated(
        container,
        lambda: container.pipeline.compose_draft(
            generation_id=generation_id, scene=selected, candidate_index=candidate
        ),
        "compose",
    )
    title = render_scene(selected, outcome.result.settings).title
    return _result_payload(outcome.result, outcome.stored, title)


@router.post("/generate", status_code=status.HTTP_201_CREATED)
async def generate(
    request: Request,
    container: ContainerDep,
    settings: SettingsDep,
    photo: UploadFile = File(..., description="A photo of a person. JPEG, PNG or WEBP."),
    scene: str = Form(..., description="Scene key, e.g. planting-seeds."),
) -> dict[str, object]:
    """Both passes in one request.

    Two calls, not one. Convenient for scripts; for exploring likeness it is
    wasteful, because a portrait you do not like still cost you the scene call.
    Prefer `/api/cartoonify` then `/api/compose`.
    """
    _check_generation_rate(request, container, tokens=2)
    selected = _require_scene(scene)
    raw = await _read_upload(photo, container)

    outcome = await _run_gated(
        container,
        lambda: _generate_blocking(container.pipeline, raw, selected, settings),
        "generate",
    )
    title = render_scene(selected, settings).title
    return _result_payload(outcome.result, outcome.stored, title)


# --- shared helpers ---------------------------------------------------------


def _check_generation_rate(request: Request, container: ContainerDep, tokens: int = 1) -> None:
    """Admit the request (or refuse it) and bill its images at once.

    `tokens` is the number of billable images this request will cost —
    candidate portraits in pass 1, the two passes of /api/generate. Counting
    images rather than requests is what keeps the per-minute number honest.
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


def _rejection_from_form(reasons: list[str]) -> RejectionFeedback:
    """Validate the rejection chips.

    This is the guard that closed-set feedback buys us: an unknown or malformed
    chip is a 422, never a line of visitor-supplied text reaching the prompt.
    Unknown values are reported as 400s rather than silently dropped, so a typo
    in the client does not look like "no reason given".
    """
    try:
        return RejectionFeedback(reasons=tuple(RejectionReason(r) for r in reasons))
    except ValueError as exc:
        raise InvalidRejection(
            f"Unknown rejection reason in {reasons!r}. "
            f"Valid values: {', '.join(r.value for r in RejectionReason)}."
        ) from exc


def _require_scene(scene: str) -> Scene:
    selected = get_scene(scene)
    if selected is None:
        raise UnknownScene(
            f"Unknown scene {scene!r}. Known scenes: {', '.join(s.key for s in SCENES)}."
        )
    return selected


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
    """Response for pass 1. Deliberately carries no scene: none has been chosen yet."""
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
        "next_step": {
            "endpoint": "/api/compose",
            "how": "POST generation_id and a scene key to get the scene image.",
        },
        "timeout_budget_seconds": budget,
    }


def _result_payload(
    result: GenerationResult,
    stored: StoredGeneration,
    scene_title: str,
) -> dict[str, object]:
    """Response for a completed scene image."""
    urls = stored.to_urls()
    logger.info(
        "api.result id=%s scene=%s cartoon_bytes=%d scene_bytes=%d",
        result.generation_id,
        result.scene_key,
        len(result.cartoon_image),
        len(result.scene_image),
    )
    return {
        "generation_id": result.generation_id,
        "stage": "composed",
        "provider": result.provider,
        "model": result.model,
        "scene_key": result.scene_key,
        "scene_title": scene_title,
        "caption": result.caption,
        "created_at": result.created_at.isoformat(),
        "images": {
            "cartoon_url": urls["cartoon"],
            "scene_url": urls["scene"],
            "scene_extension": _extension(result.scene_image),
            "cartoon_bytes": len(result.cartoon_image),
            "scene_bytes": len(result.scene_image),
        },
        "candidate_count": result.candidate_count,
        "provider_call_count": 2,
        "billed_image_count": result.candidate_count + 1,
        "prompts": {
            "cartoon": result.cartoon_prompt,
            "scene": result.scene_prompt,
        },
        "settings": result.settings.model_dump(mode="json"),
        "warnings": list(result.warnings),
        "meta_url": urls["meta"],
    }


def _generate_blocking(
    pipeline: GenerationPipeline,
    raw_upload: bytes,
    scene: Scene,
    settings: CartoonSettings,
) -> PipelineOutcome:
    """The blocking half of `/api/generate`.

    Runs on a worker thread: Pillow decoding is CPU-bound and the two model
    calls are network-bound, and neither should ever sit on the event loop.
    """
    decoded = decode_upload(raw_upload)
    return pipeline.run(
        photo_data=decoded.data,
        photo_media_type=decoded.media_type,
        scene=scene,
        settings=settings,
    )


def _options(enum_cls: type[StrEnum]) -> list[dict[str, str]]:
    """Enum values as `{value, label}` pairs, with the same labels the page uses."""
    return [{"value": value, "label": label} for value, label in choices_for(enum_cls)]


def _extension(data: bytes) -> str:
    """Return the extension used by the normalized download."""
    return "jpg" if data.startswith(b"\xff\xd8") else "png"
