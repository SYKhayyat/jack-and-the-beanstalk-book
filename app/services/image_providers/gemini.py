"""Google Gemini image provider.

Uses the `google-genai` SDK and a cheap image-capable Gemini model, which
accepts reference images alongside text — that is the whole reason this provider
fits the job: the same call can carry both the uploaded face *and* the cartoon
self-portrait, so identity can be re-asserted during scene composition instead of
being left to whatever the model remembers from one call to the next.

Deliberately no retry loop. The assignment forbids wrapping the API call in a
loop, and a retry loop on a paid endpoint is exactly the failure mode that burns
a student's balance. A failed call fails loudly.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from app.services.image_providers.base import (
    GeneratedImage,
    ImageProvider,
    ImageRequest,
    ProviderError,
    ProviderUnavailable,
)

__all__ = ["GeminiImageProvider"]

logger = logging.getLogger(__name__)


class GeminiImageProvider(ImageProvider):
    """Image generation via Google's Gemini image models.

    The model accepts at most 3 reference images per prompt; the pipeline sends
    2 in the scene pass. It also caps inline images at 7MB, which upload
    normalisation keeps well under.
    """

    name = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-3.1-flash-lite-image",
        timeout_seconds: int = 600,
    ) -> None:
        if not api_key.strip():
            raise ProviderUnavailable(
                "GEMINI_API_KEY is not set. Get a free key at "
                "https://aistudio.google.com/apikey and add it to your environment."
            )
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise ProviderUnavailable(
                "The 'google-genai' package is not installed. Run: pip install google-genai"
            ) from exc

        self._model = model
        # The timeout goes on the HTTP client, not just the request, so a slow
        # generation genuinely aborts the socket instead of leaving a thread
        # burning tokens in the background.
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=timeout_seconds * 1000),
        )
        self._genai = genai

    def health_check(self) -> tuple[bool, str]:
        return True, f"gemini ready (model={self._model})"

    def generate(self, request: ImageRequest) -> GeneratedImage:
        from google.genai import types  # imported lazily so tests need no SDK

        contents: list[Any] = [
            types.Part.from_bytes(data=image, mime_type=media_type)
            for image, media_type in zip(
                request.reference_images, request.reference_media_types, strict=True
            )
        ]
        contents.append(types.Part.from_text(text=request.prompt))

        config = types.GenerateContentConfig(
            response_modalities=["IMAGE", "TEXT"],
            image_config=types.ImageConfig(aspect_ratio=request.aspect_ratio),
        )

        logger.info(
            "gemini.generate label=%s refs=%d prompt_chars=%d model=%s",
            request.label,
            len(request.reference_images),
            len(request.prompt),
            self._model,
        )

        try:
            # The SDK's stub types are narrower than the parts we build, but each
            # element really is a types.Part.
            response = self._client.models.generate_content(
                model=self._model, contents=cast(Any, contents), config=config
            )
        except Exception as exc:  # SDK raises a wide range of exception types
            raise ProviderError(f"Gemini request failed: {exc}") from exc

        return self._extract_image(response)

    def _extract_image(self, response: object) -> GeneratedImage:
        """Pull the first inline image out of the response, whatever the shape."""
        for candidate in getattr(response, "candidates", []) or []:
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", []) or []:
                inline = getattr(part, "inline_data", None)
                if inline is None:
                    continue
                data = getattr(inline, "data", None)
                if not data:
                    continue
                media_type = getattr(inline, "mime_type", None) or "image/png"
                return GeneratedImage(
                    data=bytes(data), media_type=media_type, model=self._model
                )

        # The model sometimes refuses or answers with prose only. Surface why.
        detail = getattr(response, "text", None) or "no image in response"
        raise ProviderError(
            f"Gemini returned no image. Model output began: {str(detail)[:400]!r}"
        )
