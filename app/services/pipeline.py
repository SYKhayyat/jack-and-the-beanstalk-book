"""The generation pipeline: photo in, cartoon portrait out.

One pass: the portrait is the identity anchor for every book page. The book
runner renders pages separately — the pipeline's job is to persist the
normalised photo alongside the portrait so page calls can send both as
references without the browser re-uploading anything.
"""

from __future__ import annotations

import logging
from uuid import uuid4

from app.models import CartoonSettings
from app.services.image_providers.base import GeneratedImage, ImageProvider, ImageRequest
from app.services.prompts import build_cartoon_prompt
from app.services.storage import Draft, GenerationStore

__all__ = [
    "GenerationPipeline",
    "UnknownGeneration",
]

logger = logging.getLogger(__name__)


class UnknownGeneration(Exception):
    """No draft exists for that id — expired, deleted, or never created."""

    def __init__(self, generation_id: str) -> None:
        super().__init__(
            f"No draft found for {generation_id!r}. It may have expired. "
            "Upload the photo again to start a new one."
        )
        self.generation_id = generation_id


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
        generation_id: str | None = None,
        persist: bool = True,
    ) -> Draft:
        """Turn the photo into a cartoon portrait and persist it as a draft.

        Persisting keeps the normalised photo alongside the portrait, so the
        book runner can send both as references without the browser
        re-uploading anything. The returned draft carries the id and the
        prompt that produced it.
        """
        draft = self._build_cartoon(
            photo_data=photo_data,
            photo_media_type=photo_media_type,
            settings=settings,
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
        generation_id: str,
    ) -> Draft:
        """The portrait pass's work, returned as a draft without persisting it."""
        prompt = build_cartoon_prompt(settings)

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


def _require_image(image: GeneratedImage, label: str) -> None:
    """Guard against a provider that returned empty bytes.

    Raising beats shipping: an empty image would be written to disk and served
    as a successful result.
    """
    if not image.data:
        raise ValueError(f"Provider returned an empty {label}.")
