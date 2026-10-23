"""The generation pipeline: photo in, captioned scene image out.

Two passes, because the assignment asks for two distinct transformations and
they want different things from the model:

    pass 1  cartoonify  photo -> cartoon portrait          (applies all settings)
    pass 2  compose     cartoon portrait + photo -> scene  (applies scene spec)

The cartoon portrait from pass 1 is passed into pass 2 as a reference alongside
the original photo. That double anchor is what keeps the face recognisable once
the scene action competes for the model's attention.

The two passes are separate public methods, not one `run`. That is what lets the
user review the portrait, tweak settings and retry, and only then pay for the
scene call — and it means moving an approved portrait into the other scene
costs one call instead of two.
"""

from __future__ import annotations

import io
import logging
import textwrap
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from app.models import CartoonSettings, GenerationResult, Scene
from app.models.feedback import RejectionFeedback
from app.services.image_providers.base import (
    GeneratedImage,
    ImageProvider,
    ImageRequest,
    ProviderError,
)
from app.services.prompts import build_cartoon_prompt, build_scene_prompt, render_scene
from app.services.storage import Draft, GenerationStore, StoredGeneration

__all__ = [
    "ComposedGeneration",
    "GenerationPipeline",
    "PipelineOutcome",
    "UnknownCandidate",
    "UnknownGeneration",
]

logger = logging.getLogger(__name__)

# A real generated image is far bigger than this. Below it, the provider almost
# certainly returned a placeholder rather than a generation.
_MIN_PLAUSIBLE_IMAGE_BYTES = 1024


class UnknownCandidate(ValueError):
    """The requested portrait alternative does not exist for that draft."""

    def __init__(self, index: int, available: int) -> None:
        super().__init__(
            f"That draft has {available} portrait alternative"
            f"{'s' if available != 1 else ''}; candidate {index} does not exist."
        )
        self.index = index
        self.available = available


class UnknownGeneration(Exception):
    """No draft exists for that id — expired, deleted, or never created."""

    def __init__(self, generation_id: str) -> None:
        super().__init__(
            f"No draft found for {generation_id!r}. It may have expired. "
            "Upload the photo again to start a new one."
        )
        self.generation_id = generation_id


class PipelineOutcome:
    """A stored generation: the result plus where its files went."""

    __slots__ = ("result", "stored")

    def __init__(self, result: GenerationResult, stored: StoredGeneration) -> None:
        self.result = result
        self.stored = stored


class ComposedGeneration:
    """A draft plus the scene image composed from it."""

    __slots__ = ("result", "stored")

    def __init__(self, result: GenerationResult, stored: StoredGeneration) -> None:
        self.result = result
        self.stored = stored


class GenerationPipeline:
    """Runs the two passes, individually or together."""

    def __init__(self, provider: ImageProvider, store: GenerationStore) -> None:
        self._provider = provider
        self._store = store

    # --- pass 1 ---------------------------------------------------------------

    def cartoonify(
        self,
        *,
        photo_data: bytes,
        photo_media_type: str,
        settings: CartoonSettings,
        rejection: RejectionFeedback | None = None,
        generation_id: str | None = None,
        persist: bool = True,
    ) -> Draft:
        """Pass 1: photo in, cartoon portrait out.

        Persisting keeps the normalised photo alongside the portrait, so pass 2
        can send both as references without the browser re-uploading anything.
        The returned draft carries the id and the prompt that produced it.
        """
        draft = self._build_cartoon(
            photo_data=photo_data,
            photo_media_type=photo_media_type,
            settings=settings,
            rejection=rejection,
            generation_id=generation_id or uuid4().hex,
        )
        if persist:
            self._store.save_draft(draft)
        return draft

    def _build_cartoon(
        self,
        *,
        photo_data: bytes,
        photo_media_type: str,
        settings: CartoonSettings,
        rejection: RejectionFeedback | None,
        generation_id: str,
    ) -> Draft:
        """Pass 1's work, returned as a draft without persisting it."""
        prompt = build_cartoon_prompt(settings, rejection)

        logger.info(
            "pipeline.cartoonify id=%s provider=%s style=%s intensity=%s",
            generation_id,
            self._provider.name,
            settings.cartoon_style,
            settings.cartoon_intensity,
        )

        # One API call for the whole set of alternatives. Providers that cannot
        # do that return one image whatever was asked for.
        candidates = self._provider.generate_candidates(
            ImageRequest(
                prompt=prompt,
                reference_images=(photo_data,),
                reference_media_types=(photo_media_type,),
                output_format="png",
                aspect_ratio=str(settings.aspect_ratio),
                label="cartoonify",
            ),
            count=settings.candidates,
        )
        for candidate in candidates:
            _require_image(candidate, "cartoon portrait")

        return Draft(
            generation_id=generation_id,
            settings=settings,
            cartoon_prompt=prompt,
            candidates=tuple(candidate.data for candidate in candidates),
            photo_data=photo_data,
            photo_media_type=photo_media_type,
            provider=self._provider.name,
            model=candidates[0].model or "unknown",
        )

    # --- pass 2 ---------------------------------------------------------------

    def compose(
        self, *, draft: Draft, scene: Scene, persist: bool = True
    ) -> ComposedGeneration:
        """Pass 2: the cartoon portrait (plus the original photo) into a scene."""
        # Resolve the character's name and pronouns for this draft before either
        # the prompt or the burned caption is produced, so they cannot disagree.
        scene = render_scene(scene, draft.settings)
        prompt = build_scene_prompt(scene, draft.settings)

        logger.info(
            "pipeline.compose id=%s provider=%s scene=%s",
            draft.generation_id,
            self._provider.name,
            scene.key,
        )

        composed = self._provider.generate(
            ImageRequest(
                prompt=prompt,
                # Both anchors: the stylised face we already approved, and the
                # real one it came from.
                reference_images=(draft.cartoon_image, draft.photo_data),
                reference_media_types=("image/png", draft.photo_media_type),
                output_format="png",
                aspect_ratio=str(draft.settings.aspect_ratio),
                label="scene-compose",
            )
        )
        _require_image(composed, "scene image")

        warnings: list[str] = []
        try:
            captioned_scene = _caption_scene_image(composed.data, scene.caption)
        except ProviderError:
            # Keep the provider's bytes for diagnostics and preserve the existing
            # small-output warning. Real providers return decodable images; this
            # fallback keeps test doubles and provider error payloads observable.
            captioned_scene = composed.data
            warnings.append(
                "The caption could not be embedded because the provider output was "
                "not a readable image."
            )
        if len(composed.data) < _MIN_PLAUSIBLE_IMAGE_BYTES:
            warnings.append(
                "The returned image is suspiciously small; the provider may have "
                "returned a placeholder instead of a real generation."
            )

        result = GenerationResult(
            generation_id=draft.generation_id,
            provider=self._provider.name,
            model=composed.model or draft.model,
            scene_key=scene.key,
            # The caption burned onto the image is exactly the spec we sent.
            caption=scene.caption,
            settings=draft.settings,
            cartoon_prompt=draft.cartoon_prompt,
            scene_prompt=prompt,
            cartoon_image=draft.cartoon_image,
            scene_image=captioned_scene,
            candidate_count=len(draft.candidates),
            warnings=tuple(warnings),
        )

        stored = (
            self._store.save(result)
            if persist
            else StoredGeneration(self._store.root, draft.generation_id)
        )
        logger.info(
            "pipeline.composed id=%s scene_bytes=%d",
            result.generation_id,
            len(result.scene_image),
        )
        return ComposedGeneration(result=result, stored=stored)

    def compose_draft(
        self,
        *,
        generation_id: str,
        scene: Scene,
        candidate_index: int = 0,
        persist: bool = True,
    ) -> ComposedGeneration:
        """Pass 2 for a draft already on disk.

        Raises:
            UnknownGeneration: if no draft exists for that id.
        """
        draft = self._store.load_draft(generation_id)
        if draft is None:
            raise UnknownGeneration(generation_id)
        if candidate_index:
            try:
                draft = draft.with_candidate(candidate_index)
            except IndexError as exc:
                raise UnknownCandidate(candidate_index, len(draft.candidates)) from exc
        return self.compose(draft=draft, scene=scene, persist=persist)

    # --- both -----------------------------------------------------------------

    def run(
        self,
        *,
        photo_data: bytes,
        photo_media_type: str,
        scene: Scene,
        settings: CartoonSettings,
        rejection: RejectionFeedback | None = None,
        persist: bool = True,
    ) -> PipelineOutcome:
        """Both passes in one shot — the convenience path for API clients.

        The interactive UI uses `cartoonify` and `compose` separately so the
        portrait can be reviewed first.

        Raises:
            ProviderError: if either pass fails.
        """
        draft = self._build_cartoon(
            photo_data=photo_data,
            photo_media_type=photo_media_type,
            settings=settings,
            rejection=rejection,
            generation_id=uuid4().hex,
        )
        if persist:
            # Keep the intermediate portrait available even after the one-shot
            # request has completed.  This makes "same cartoon, other scene"
            # work for both API clients and the browser UI.
            self._store.save_draft(draft)
        composed = self.compose(draft=draft, scene=scene, persist=persist)
        return PipelineOutcome(result=composed.result, stored=composed.stored)


def _require_image(image: GeneratedImage, label: str) -> None:
    """Guard against a provider that returned empty bytes.

    Raising beats shipping: an empty image would be written to disk and served
    as a successful result.
    """
    if not image.data:
        raise ValueError(f"Provider returned an empty {label}.")


def _caption_scene_image(data: bytes, caption: str) -> bytes:
    """Burn the exact scene specification into the final image.

    The caption is also returned in JSON and metadata, but rendering it here is
    what makes the downloaded artefact satisfy the assignment independently of
    whether a provider honours text instructions.
    """
    try:
        with Image.open(io.BytesIO(data)) as opened:
            image = opened.convert("RGB")
    except (OSError, UnidentifiedImageError) as exc:
        raise ProviderError("Provider returned an unreadable scene image.") from exc

    width, height = image.size
    font_size = max(12, min(30, width // 42))
    try:
        font = ImageFont.load_default(size=font_size)
    except TypeError:  # pragma: no cover - Pillow < 10.1
        font = ImageFont.load_default()
    lines = textwrap.wrap(caption, width=max(24, width // 15)) or [caption]
    line_height = font_size + 6
    panel_height = min(height, max(56, line_height * len(lines) + 24))
    top = height - panel_height

    draw = ImageDraw.Draw(image, "RGBA")
    draw.rectangle((0, top, width, height), fill=(0, 0, 0, 185))
    draw.multiline_text(
        (16, top + 12),
        "\n".join(lines),
        fill=(255, 255, 255, 255),
        font=font,
        spacing=6,
    )

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
