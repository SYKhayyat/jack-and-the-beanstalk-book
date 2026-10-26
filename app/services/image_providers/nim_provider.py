"""NVIDIA NIM image provider.

NVIDIA's hosted NIM service speaks the OpenAI images shape at
`https://integrate.api.nvidia.com/v1`, so adding it is a provider, not a
rewrite: one POST to `/images/edits` carrying the reference images and the
prompt together, which is the capability this app is built on.

Three properties of the NIM contract shape the code below, and all three come
from NVIDIA's own OpenAI-compatible schema rather than from guesswork:

* **The prompt is capped at 800 characters.** This app writes prompts of about
  three thousand. Sending one unchanged is a 422, so the prompt is condensed
  first: the head (the task and the identity directive) and the tail (the
  negative constraints) are kept, and the middle is dropped. See
  `condense_prompt`.
* **`n` may only be 1.** Asking for three alternative portraits therefore
  yields one image, and `generate_candidates` says so rather than pretending.
* **Images go out as data URIs.** The schema takes `image` as a string (or a
  list of strings), each a `data:image/{format};base64,...` value, not as an
  uploaded file part.

Like every other provider here, there is no retry loop: a failed call fails
loudly. NIM's free tier is rate limited to roughly 40 requests a minute, so a
retry storm would be both wasteful and rude.
"""

from __future__ import annotations

import base64
import binascii
import io
import logging
from typing import Any

import httpx

from app.redaction import redact
from app.services.image_providers.base import (
    GeneratedImage,
    ImageProvider,
    ImageRequest,
    ProviderError,
    ProviderUnavailable,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "NimImageProvider",
    "condense_prompt",
    "size_for_aspect_ratio",
]

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = "flux.2-klein-4b"

# The OpenAI-compatible image-editing schema caps the prompt at 800 characters
# and allows only `n=1`.
MAX_PROMPT_CHARS = 800

# How much of the tail survives the cut when a prompt has to be shortened. The
# tail is where this app puts its negative constraints ("no text, no extra
# people"), so it is worth more per character than the middle.
PROMPT_TAIL_CHARS = 200

# NIM's FLUX models accept any edge between 512 and 1344 in steps of 16, so
# these sizes are inside the documented range and keep each ratio close to
# one megapixel.
_SIZE_BY_ASPECT: dict[str, str] = {
    "1:1": "1024x1024",
    "4:3": "1216x912",
    "3:4": "912x1216",
    "16:9": "1344x768",
}

# NIM accepts jpg, jpeg and png. Anything else is converted to PNG on the way
# out, because a 422 from a format mismatch is a wasted round trip.
_ACCEPTED_MEDIA_TYPES = {"image/png", "image/jpeg"}

_MEDIA_TYPE_BY_FORMAT: dict[str, str] = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "webp": "image/webp",
}


def size_for_aspect_ratio(aspect_ratio: str) -> str:
    """Map an aspect ratio to a concrete `WIDTHxHEIGHT` for NIM.

    Raises:
        ProviderError: if the ratio is unknown.
    """
    try:
        return _SIZE_BY_ASPECT[aspect_ratio]
    except KeyError:
        raise ProviderError(
            f"Unsupported aspect ratio {aspect_ratio!r}. "
            f"Known ratios: {', '.join(sorted(_SIZE_BY_ASPECT))}."
        ) from None


def condense_prompt(prompt: str, limit: int, tail_chars: int) -> str:
    """Shorten a prompt to what NIM will accept, keeping the parts that matter.

    The head carries the task and the identity directive; the tail carries the
    negative constraints. The middle — the style detail — is what goes, because
    a prompt rejected for being too long produces no picture at all.

    Returns the prompt unchanged when it already fits.
    """
    if len(prompt) <= limit:
        return prompt

    tail_chars = min(tail_chars, max(0, limit - 1))
    marker = "\n[...]\n"
    head_chars = limit - tail_chars - len(marker)
    if head_chars <= 0:
        # The limit is too small to hold both ends; the head wins.
        return prompt[:limit]

    logger.warning(
        "nim.prompt_condensed original_chars=%d limit=%d kept_head=%d kept_tail=%d",
        len(prompt),
        limit,
        head_chars,
        tail_chars,
    )
    return prompt[:head_chars] + marker + prompt[-tail_chars:]


class NimImageProvider(ImageProvider):
    """Image generation via NVIDIA's hosted NIM service."""

    name = "nim"

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: int = 600,
        size: str | None = None,
        max_prompt_chars: int = MAX_PROMPT_CHARS,
        prompt_tail_chars: int = PROMPT_TAIL_CHARS,
    ) -> None:
        # Strip before use: a key pasted into an environment variable often
        # carries a trailing newline, which produces an illegal Authorization
        # header rather than a clean auth error.
        api_key = api_key.strip()
        if not api_key:
            raise ProviderUnavailable(
                "NIM_API_KEY is not set. Create a key at "
                "https://build.nvidia.com/settings/api-keys and add it to your "
                "environment."
            )

        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._size = size or None
        self._max_prompt_chars = max_prompt_chars
        self._prompt_tail_chars = prompt_tail_chars
        # httpx does not retry on its own, which is what this app wants: one
        # call, one billable request, no hidden second attempt.
        self._client = httpx.Client(
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )

    def health_check(self) -> tuple[bool, str]:
        return True, f"nim ready (model={self._model})"

    def generate(self, request: ImageRequest) -> GeneratedImage:
        response = self._post(self._build_payload(request))
        return self._extract_image(response)

    def generate_candidates(self, request: ImageRequest, count: int) -> list[GeneratedImage]:
        """Return one image: NIM's editing endpoint accepts `n=1` only.

        The pipeline treats a short list as a legitimate answer — providers that
        cannot do alternatives in one call return one image whatever was asked
        for — so the portrait step still works with this provider.
        """
        if count < 1:
            raise ValueError("count must be at least 1")
        if count > 1:
            logger.info("nim.single_candidate requested=%d reason=n_is_limited_to_one", count)
        return [self.generate(request)]

    def _build_payload(self, request: ImageRequest) -> dict[str, Any]:
        """Shape one editing call."""
        if not request.reference_images:
            raise ProviderError(
                "The NVIDIA NIM provider needs at least one reference image; this "
                "pipeline always supplies one."
            )

        prompt = condense_prompt(
            request.prompt, self._max_prompt_chars, self._prompt_tail_chars
        )
        size = self._size or size_for_aspect_ratio(request.aspect_ratio)

        logger.info(
            "nim.generate label=%s refs=%d prompt_chars=%d model=%s size=%s",
            request.label,
            len(request.reference_images),
            len(prompt),
            self._model,
            size,
        )

        return {
            "model": self._model,
            "prompt": prompt,
            "image": [
                _as_data_uri(data, media_type)
                for data, media_type in zip(
                    request.reference_images,
                    request.reference_media_types,
                    strict=True,
                )
            ],
            "size": size,
            # The schema allows n=1 only, and b64_json is the only response
            # format NIM produces.
            "n": 1,
            "response_format": "b64_json",
        }

    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        """Send the call once and hand back the raw response."""
        url = f"{self._base_url}/images/edits"
        try:
            response = self._client.post(url, json=payload)
        except httpx.HTTPError as exc:
            # Redact the key and drop the cause: a transport error can embed
            # the raw Authorization header, and `from None` keeps that message
            # out of the traceback.
            detail = redact(str(exc), (self._api_key,))
            raise ProviderError(f"NVIDIA NIM request failed: {detail}") from None

        if response.status_code >= 400:
            raise ProviderError(
                f"NVIDIA NIM request failed ({response.status_code}): "
                f"{_error_detail(response, self._api_key)}"
            )
        return response

    def _extract_image(self, response: httpx.Response) -> GeneratedImage:
        """Pull the image out of the response."""
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError("NVIDIA NIM returned a response that was not JSON.") from exc

        if not isinstance(body, dict):
            raise ProviderError("NVIDIA NIM returned an unexpected response shape.")

        images = body.get("data") or []
        if not images:
            raise ProviderError(
                "NVIDIA NIM returned no image. The request may have been refused; "
                "try a different photo or prompt."
            )

        first = images[0]
        encoded = first.get("b64_json") if isinstance(first, dict) else None
        if not encoded:
            raise ProviderError(
                "NVIDIA NIM returned no image data. This provider expects the "
                "base64 form of the image."
            )

        try:
            raw = base64.b64decode("".join(str(encoded).split()), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ProviderError("NVIDIA NIM returned undecodable image data.") from exc
        if not raw:
            raise ProviderError("NVIDIA NIM returned an empty image.")

        media_type = _sniff_media_type(raw) or _media_type_by_format(body.get("output_format"))
        return GeneratedImage(data=raw, media_type=media_type, model=self._model)


def _as_data_uri(data: bytes, media_type: str) -> str:
    """Wrap reference bytes in the `data:` form NIM's schema asks for."""
    normalised = media_type.lower()
    if normalised == "image/jpg":
        normalised = "image/jpeg"
    if normalised not in _ACCEPTED_MEDIA_TYPES:
        normalised = "image/png"
        data = _to_png(data)
    encoded = base64.b64encode(data).decode()
    return f"data:{normalised};base64,{encoded}"


def _to_png(data: bytes) -> bytes:
    """Re-encode an image NIM would not accept as PNG."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as opened:
            buffer = io.BytesIO()
            opened.convert("RGB").save(buffer, format="PNG")
            return buffer.getvalue()
    except Exception as exc:
        raise ProviderError(
            f"A reference image could not be converted for NVIDIA NIM: {exc}"
        ) from exc


def _sniff_media_type(data: bytes) -> str | None:
    """Read the image format from its own bytes.

    NIM may answer with PNG or JPEG depending on the model, and the app stores
    the file under the extension its media type implies — so the bytes decide,
    not the request.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _media_type_by_format(output_format: object) -> str:
    """Fall back to the format NIM reported, then to PNG."""
    if isinstance(output_format, str):
        media_type = _MEDIA_TYPE_BY_FORMAT.get(output_format.strip().lower())
        if media_type:
            return media_type
    return "image/png"


def _error_detail(response: httpx.Response, api_key: str) -> str:
    """Turn an error response into one short line an operator can act on."""
    detail = ""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        raw = body.get("detail") or body.get("error") or body.get("message")
        if isinstance(raw, list):
            detail = "; ".join(str(item) for item in raw)
        elif raw is not None:
            detail = str(raw)
    if not detail:
        detail = response.text.strip()
    detail = redact(detail, (api_key,))
    if len(detail) > 300:
        detail = detail[:300] + "..."
    return detail or "no detail returned"
