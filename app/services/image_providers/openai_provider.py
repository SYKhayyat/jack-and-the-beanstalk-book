"""OpenAI image provider.

Uses the `images.edit` endpoint rather than `images.generate`, because the whole
design depends on sending reference images: pass 1 edits the uploaded photo into
a cartoon portrait, and pass 2 edits that portrait (plus the original photo) into
the scene. `images.edit` takes a list of images, so one call can carry both
identity anchors.

Cheapest configuration by default: `gpt-image-2.5-flare` at `quality=low`, which
is the one per-image figure OpenAI publishes — $0.00588 for a 1K output, from
196 output tokens at $30 per 1M image output tokens. Both model and quality are
settings, so moving up a tier is an environment change.

No retry loop, like every other provider here: a failed call fails loudly rather
than quietly spending a second call's worth of money.
"""

from __future__ import annotations

import base64
import io
import logging
from typing import Literal, cast, get_args

from app.redaction import redact
from app.services.image_providers.base import (
    GeneratedImage,
    ImageProvider,
    ImageRequest,
    ProviderError,
    ProviderUnavailable,
)

__all__ = ["OpenAIImageProvider", "Quality", "size_for_aspect_ratio"]

# Mirrors the SDK's own literal so a bad setting fails at startup, with a
# readable message, instead of on the first billable call.
Quality = Literal["low", "medium", "high", "xhigh", "max", "auto", "standard"]
QUALITIES: tuple[str, ...] = get_args(Quality)

logger = logging.getLogger(__name__)

# gpt-image-2.5 accepts custom sizes subject to three documented constraints:
# both edges a multiple of 16, aspect ratio within 1:3..3:1, and total pixels
# within 655,360..8,294,400. These are the smallest sizes satisfying all three
# for each ratio this app offers, so we are not paying for pixels nobody asked
# for.
_SIZE_BY_ASPECT: dict[str, str] = {
    "1:1": "1024x1024",
    "4:3": "1152x864",
    "3:4": "864x1152",
    "16:9": "1344x768",
}

_MULTIPLE_OF = 16
_MIN_PIXELS = 655_360
_MAX_PIXELS = 8_294_400
_EXTENSION_BY_MEDIA_TYPE = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


def size_for_aspect_ratio(aspect_ratio: str) -> str:
    """Map an aspect ratio to a concrete `WIDTHxHEIGHT` for the OpenAI API.

    Raises:
        ProviderError: if the ratio is unknown, or if a configured size ever
            drifts outside the documented bounds.
    """
    try:
        size = _SIZE_BY_ASPECT[aspect_ratio]
    except KeyError:
        raise ProviderError(
            f"Unsupported aspect ratio {aspect_ratio!r}. "
            f"Known ratios: {', '.join(sorted(_SIZE_BY_ASPECT))}."
        ) from None
    _assert_size_meets_constraints(size)
    return size


def _assert_size_meets_constraints(size: str) -> None:
    """Fail fast if a size would be rejected by the API, rather than after billing."""
    width_text, _, height_text = size.partition("x")
    width, height = int(width_text), int(height_text)
    problems = []
    if width % _MULTIPLE_OF or height % _MULTIPLE_OF:
        problems.append(f"edges must be multiples of {_MULTIPLE_OF}")
    if not (1 / 3 <= width / height <= 3):
        problems.append("aspect ratio must be within 1:3..3:1")
    if not _MIN_PIXELS <= width * height <= _MAX_PIXELS:
        problems.append(f"total pixels must be {_MIN_PIXELS}..{_MAX_PIXELS}")
    if problems:
        raise ProviderError(f"Invalid OpenAI size {size}: {'; '.join(problems)}.")


class OpenAIImageProvider(ImageProvider):
    """Image generation via OpenAI's GPT image models."""

    name = "openai"

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-image-2.5-flare",
        quality: str = "low",
        timeout_seconds: int = 600,
    ) -> None:
        # Strip before use: a key pasted into an environment variable often
        # carries a trailing newline, which produces an illegal Authorization
        # header ("Illegal header value b'Bearer sk-...\n'") rather than a clean
        # auth error. Trimming here makes that class of paste harmless.
        api_key = api_key.strip()
        if not api_key:
            raise ProviderUnavailable(
                "OPENAI_API_KEY is not set. Create a key at "
                "https://platform.openai.com/api-keys and add it to your environment."
            )
        self._api_key = api_key
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise ProviderUnavailable(
                "The 'openai' package is not installed. Run: pip install openai"
            ) from exc

        if quality not in QUALITIES:
            raise ProviderUnavailable(
                f"Unknown OPENAI_QUALITY {quality!r}. Valid values: {', '.join(QUALITIES)}."
            )

        self._model = model
        self._quality: Quality = cast(Quality, quality)
        self._client = OpenAI(
            api_key=api_key,
            # On the client, so a slow generation aborts the socket instead of
            # leaving a thread running after the request has already timed out.
            timeout=timeout_seconds,
            max_retries=0,  # no retry loop around a billable call
        )

    def health_check(self) -> tuple[bool, str]:
        return True, f"openai ready (model={self._model}, quality={self._quality})"

    def generate(self, request: ImageRequest) -> GeneratedImage:
        return self._extract_images(self._call(request, n=1), expected=1)[0]

    def _call(self, request: ImageRequest, *, n: int) -> object:
        """Shape the request, send it once, and hand back the raw response."""
        if not request.reference_images:
            raise ProviderError(
                "The OpenAI provider needs at least one reference image; this "
                "pipeline always supplies one."
            )

        size = size_for_aspect_ratio(request.aspect_ratio)
        references = [
            _as_upload(data, media_type, index)
            for index, (data, media_type) in enumerate(
                zip(
                    request.reference_images,
                    request.reference_media_types,
                    strict=True,
                )
            )
        ]

        logger.info(
            "openai.generate label=%s refs=%d n=%d prompt_chars=%d model=%s "
            "quality=%s size=%s",
            request.label,
            len(references),
            n,
            len(request.prompt),
            self._model,
            self._quality,
            size,
        )

        try:
            response = self._client.images.edit(
                model=self._model,
                # A single image is sent bare; several go as a list.
                image=references[0] if len(references) == 1 else references,
                prompt=request.prompt,
                size=size,
                quality=self._quality,
                output_format="png",
                # `input_fidelity` is deliberately omitted: OpenAI documents it
                # for gpt-image-1 and 1.5 only, and says to omit it for
                # gpt-image-2 and later. Facial fidelity is carried by the prompt.
                n=n,
            )
        except Exception as exc:  # the SDK raises a wide range of exception types
            # Redact the key and drop the cause: a transport error can embed the
            # raw Authorization header, and `from None` stops that original
            # message (secret included) from being printed in the traceback.
            detail = redact(str(exc), (self._api_key,))
            raise ProviderError(f"OpenAI request failed: {detail}") from None

        return response

    def generate_candidates(self, request: ImageRequest, count: int) -> list[GeneratedImage]:
        """Return `count` alternatives from a single call, via OpenAI's `n`."""
        if count < 1:
            raise ValueError("count must be at least 1")
        return self._extract_images(self._call(request, n=count), expected=count)

    def _extract_images(self, response: object, *, expected: int) -> list[GeneratedImage]:
        """Pull every image out of the response, whatever shape it arrived in."""
        found: list[GeneratedImage] = []
        saw_url_only = False
        for item in getattr(response, "data", None) or []:
            encoded = getattr(item, "b64_json", None)
            if not encoded:
                saw_url_only = saw_url_only or bool(getattr(item, "url", None))
                continue
            try:
                raw = base64.b64decode(encoded)
            except (ValueError, TypeError) as exc:
                raise ProviderError("OpenAI returned undecodable image data.") from exc
            found.append(GeneratedImage(data=raw, media_type="image/png", model=self._model))

        if len(found) == expected:
            return found

        if saw_url_only:
            raise ProviderError(
                "OpenAI returned an image URL instead of image data. This provider "
                "expects base64 output; check that response_format is not being "
                "overridden to 'url'."
            )

        if found:
            logger.warning(
                "openai.returned_fewer_images requested=%d received=%d", expected, len(found)
            )
            return found

        # Usually a content-policy refusal, which comes back with no image at all.
        raise ProviderError(
            "OpenAI returned no image. The request may have been refused by the "
            "usage policy; try a different photo or prompt."
        )


def _as_upload(data: bytes, media_type: str, index: int) -> io.BytesIO:
    """Wrap reference bytes in a named stream, which is how the SDK builds multipart."""
    extension = _EXTENSION_BY_MEDIA_TYPE.get(media_type, "png")
    stream = io.BytesIO(data)
    stream.name = f"reference-{index}.{extension}"
    return stream
