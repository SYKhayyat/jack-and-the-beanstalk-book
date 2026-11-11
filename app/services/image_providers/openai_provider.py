"""OpenAI image provider.

Uses the `images.edit` endpoint rather than `images.generate`, because the whole
design depends on sending reference images: pass 1 edits the uploaded photo into
a cartoon portrait, and pass 2 edits that portrait (plus the original photo) into
the scene. `images.edit` takes a list of images, so one call can carry both
identity anchors.

Cheapest configuration by default: `gpt-image-2.5-flare` at `quality=low`. Both
model and quality are settings, so moving up a tier is an environment change.

OpenAI bills an edit by token: the prompt's text tokens and every reference
image's tokens as input, plus the picture as output. The output alone is small
(196 tokens, $0.00588, for a 1K low-quality picture); the reference images
going in usually cost more. Each response reports its own token counts, so
every image returned carries its call's `TokenUsage` and the cost ledger prices
the call from what was actually billed. To keep that input small, the first
reference (the plate, or the photo being edited) is sent at most
`REFERENCE_MAX_EDGE` pixels on its long edge and every later one (a portrait,
read only for a face and a look) at most `PORTRAIT_MAX_EDGE`.

No retry loop for failures, like every other provider here: a failed call fails
loudly rather than quietly spending a second call's worth of money. The one
exception is a 429 rate-limit rejection, which is refused before any image is
made (so unbilled) and is re-sent after the server's `Retry-After`. Calls are
also paced to `OPENAI_IMAGES_PER_MINUTE` so that rarely happens.
"""

from __future__ import annotations

import base64
import io
import logging
import time
from typing import Literal, cast, get_args

from PIL import Image

from app.redaction import redact
from app.services.image_providers.base import (
    GeneratedImage,
    ImageProvider,
    ImageRequest,
    ProviderError,
    ProviderUnavailable,
    TokenUsage,
)
from app.services.image_providers.pacer import ImagePacer
from app.services.media import FAST_PNG_LEVEL, ShrunkCache

__all__ = ["OpenAIImageProvider", "Quality", "size_for_aspect_ratio"]

# Mirrors the SDK's own literal so a bad setting fails at startup, with a
# readable message, instead of on the first billable call.
Quality = Literal["low", "medium", "high", "xhigh", "max", "auto", "standard"]
QUALITIES: tuple[str, ...] = get_args(Quality)

logger = logging.getLogger(__name__)

# A 429 is rejected before any image is made, so it is free to re-send.
_RATE_LIMIT_RETRIES = 3
_MAX_RATE_LIMIT_WAIT = 60.0


def _rate_limit_delay(exc: Exception, attempt: int) -> float | None:
    """Seconds to wait before re-sending after a 429, or None if not retryable.

    Out-of-quota is also a 429 but waiting will not fix it, so it is not retried.
    """
    if getattr(exc, "status_code", None) != 429:
        return None
    if getattr(exc, "code", None) == "insufficient_quota":
        return None
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    try:
        wait = float(headers.get("retry-after", ""))
    except (TypeError, ValueError):
        wait = 5.0 * (2**attempt)
    return min(max(wait, 1.0), _MAX_RATE_LIMIT_WAIT)


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
#: The longest edge a reference is uploaded at. Input image tokens grow with
#: resolution, and the picture that comes back is about 1K: the plate needs its
#: layout, poses and colours and the portraits their faces, which all survive
#: at 1024. Smaller references are sent untouched, never enlarged.
REFERENCE_MAX_EDGE = 1024
#: The longest edge of every reference after the first. Those are portraits:
#: the model reads a face, hair and outfit from them, never a layout, and they
#: survive at 512 for about a quarter of the image tokens.
PORTRAIT_MAX_EDGE = 512

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
        images_per_minute: int = 0,
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
        # Spaces calls under OpenAI's images-per-minute cap, so they wait here
        # rather than being rejected. 0 turns pacing off.
        self._pacer = ImagePacer(images_per_minute) if images_per_minute > 0 else None

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

        for attempt in range(_RATE_LIMIT_RETRIES + 1):
            if self._pacer is not None:
                waited = self._pacer.acquire(n)
                if waited >= 1:
                    logger.info("openai.paced label=%s waited=%.1fs", request.label, waited)
            try:
                return self._client.images.edit(
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
                delay = _rate_limit_delay(exc, attempt)
                if delay is not None and attempt < _RATE_LIMIT_RETRIES:
                    # A rate-limit rejection is refused before generating, so it
                    # is not billed: waiting and re-sending cannot double-charge.
                    logger.warning(
                        "openai.rate_limited label=%s retry_in=%.1fs", request.label, delay
                    )
                    time.sleep(delay)
                    continue
                # Redact the key and drop the cause: a transport error can embed the
                # raw Authorization header, and `from None` stops that original
                # message (secret included) from being printed in the traceback.
                detail = redact(str(exc), (self._api_key,))
                raise ProviderError(f"OpenAI request failed: {detail}") from None
        raise AssertionError("unreachable")  # pragma: no cover

    def generate_candidates(self, request: ImageRequest, count: int) -> list[GeneratedImage]:
        """Return `count` alternatives from a single call, via OpenAI's `n`."""
        if count < 1:
            raise ValueError("count must be at least 1")
        return self._extract_images(self._call(request, n=count), expected=count)

    def _extract_images(self, response: object, *, expected: int) -> list[GeneratedImage]:
        """Pull every image out of the response, whatever shape it arrived in."""
        usage = _token_usage(response)
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
            found.append(
                GeneratedImage(
                    data=raw, media_type="image/png", model=self._model, usage=usage
                )
            )

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


#: The shrunk references, by content (see `_fit_reference`).
_REFERENCE_COPIES = ShrunkCache()


def _as_upload(data: bytes, media_type: str, index: int) -> io.BytesIO:
    """Wrap reference bytes in a named stream, which is how the SDK builds multipart."""
    limit = REFERENCE_MAX_EDGE if index == 0 else PORTRAIT_MAX_EDGE
    data, media_type = _fit_reference(data, media_type, limit)
    extension = _EXTENSION_BY_MEDIA_TYPE.get(media_type, "png")
    stream = io.BytesIO(data)
    stream.name = f"reference-{index}.{extension}"
    return stream


def _fit_reference(data: bytes, media_type: str, limit: int) -> tuple[bytes, str]:
    """The reference scaled to `limit` pixels on its long edge, as a lossless PNG.

    A reference already that small, or one Pillow cannot read, goes as it came:
    the API judges an unreadable file, and its error is clearer than ours.
    Remembered by content (`ShrunkCache`): a book sends the same portrait and
    faces with every page, and the same plate with each of a page's attempts.
    """
    return _REFERENCE_COPIES.get(
        data, ("reference", media_type, limit), lambda: _fit(data, media_type, limit)
    )


def _fit(data: bytes, media_type: str, limit: int) -> tuple[bytes, str]:
    try:
        with Image.open(io.BytesIO(data)) as image:
            if max(image.size) <= limit:
                return data, media_type
            image.load()
            fitted = image.convert("RGBA" if "A" in image.getbands() else "RGB")
    except (OSError, ValueError, Image.DecompressionBombError):
        return data, media_type
    fitted.thumbnail((limit, limit), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    fitted.save(buffer, format="PNG", compress_level=FAST_PNG_LEVEL)
    return buffer.getvalue(), "image/png"


def _token_usage(response: object) -> TokenUsage | None:
    """The call's token bill from `response.usage`, or None when it is absent.

    Input is split into text and image tokens by `input_tokens_details`. If a
    response gives only the input total, all of it is counted as image tokens,
    the dearer rate: an estimate may run high, never low.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    total_in = _count(getattr(usage, "input_tokens", None))
    output = _count(getattr(usage, "output_tokens", None))
    if total_in is None or output is None:
        return None
    details = getattr(usage, "input_tokens_details", None)
    text = _count(getattr(details, "text_tokens", None)) if details is not None else None
    image = _count(getattr(details, "image_tokens", None)) if details is not None else None
    if text is None or image is None:
        text, image = 0, total_in
    return TokenUsage(text_input_tokens=text, image_input_tokens=image, output_tokens=output)


def _count(value: object) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )
