"""Agnes AI image provider.

`agnes-image-2.5-flash` is the hosted endpoint this app actually needs: it takes
an **array of reference images** (Data URI Base64 or public URLs) alongside the
prompt, which is the one capability every free image tier so far has refused.
NVIDIA's hosted NIM, for comparison, answers with `Expected: example_id, got:
base64` and only accepts its four canned preview photos.

The contract, from <https://wiki.agnes-ai.com/en/docs/agnes-image-25-flash>:

* `POST {base}/images/generations` — generation and editing share one endpoint.
* `size` is a **tier** (`1K`, `2K`, `3K`, `4K`) paired with a `ratio`; there is
  no `WIDTHxHEIGHT` form worth relying on, because unsupported exact sizes are
  normalised to the nearest tier.
* References and the output format live in `extra_body`, not at the top level.
  Putting `response_format` at the top level is the documented common mistake.
* The free tier is capped by resolution: about 10 requests a minute at `1K`,
  5 at `2K`, and a single request per minute above that. `1K` is therefore the
  only sane default, and `AGNES_SIZE` is how an operator steps it up.
* Everything is currently priced at zero, and the first three reference images
  were always free — this app sends at most two.

No retry loop, like every other provider here: a failed generation fails
loudly rather than quietly spending a second request.
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
    "SIZES",
    "AgnesImageProvider",
    "size_for_aspect_ratio",
]

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://apihub.agnes-ai.com/v1"
DEFAULT_MODEL = "agnes-image-2.5-flash"

# The resolution tiers the service documents, cheapest first: 1K is the only
# one the free tier can sustain more than a few calls a minute of.
SIZES = ("1K", "2K", "3K", "4K")

# The ratios the service documents. The app's own AspectRatio enum offers only
# values from this list, and at 1K they come back as 1024x1024 (1:1), 1152x864
# (4:3), 864x1152 (3:4) and 1312x736 (16:9).
_SIZE_BY_ASPECT: dict[str, str] = {
    "1:1": "1K",
    "4:3": "1K",
    "3:4": "1K",
    "16:9": "1K",
    "9:16": "1K",
    "2:3": "1K",
    "3:2": "1K",
    "21:9": "1K",
}

# Agnes accepts jpg, png and jpeg Data URIs; anything else is re-encoded to PNG
# on the way out, so a format mismatch cannot cost a round trip.
_ACCEPTED_MEDIA_TYPES = {"image/png", "image/jpeg"}

_HTTP_TIMEOUT_HINT = 360


def size_for_aspect_ratio(aspect_ratio: str) -> tuple[str, str]:
    """Map an aspect ratio to a `(size tier, ratio)` pair for the Agnes API.

    Raises:
        ProviderError: if the ratio is not one the service documents.
    """
    if aspect_ratio not in _SIZE_BY_ASPECT:
        raise ProviderError(
            f"Unsupported aspect ratio {aspect_ratio!r}. "
            f"Known ratios: {', '.join(sorted(_SIZE_BY_ASPECT))}."
        )
    return _SIZE_BY_ASPECT[aspect_ratio], aspect_ratio


class AgnesImageProvider(ImageProvider):
    """Image generation and editing via Agnes AI's hosted image models."""

    name = "agnes"

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: int = 600,
        size: str = "1K",
    ) -> None:
        # Strip before use: a key pasted into an environment variable often
        # carries a trailing newline, which produces an illegal Authorization
        # header rather than a clean auth error.
        api_key = api_key.strip()
        if not api_key:
            raise ProviderUnavailable(
                "AGNES_API_KEY is not set. Create a key in the Agnes AI console "
                "and add it to your environment."
            )

        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        if size not in SIZES:
            raise ProviderUnavailable(
                f"Unknown AGNES_SIZE {size!r}. Valid values: {', '.join(SIZES)}."
            )
        self._size = size
        # httpx does not retry on its own, which is what this app wants: one
        # call, one request, no hidden second attempt.
        self._client = httpx.Client(
            timeout=float(min(timeout_seconds, _HTTP_TIMEOUT_HINT)),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )

    def health_check(self) -> tuple[bool, str]:
        return True, f"agnes ready (model={self._model}, size={self._size})"

    def generate(self, request: ImageRequest) -> GeneratedImage:
        response = self._post(self._build_payload(request))
        return self._extract_image(response)

    def generate_candidates(self, request: ImageRequest, count: int) -> list[GeneratedImage]:
        """Return one image: one call buys one picture.

        The pipeline treats a short list as a legitimate answer — providers that
        cannot do alternatives in one call return one image whatever was asked
        for — so the portrait step still works with this provider.
        """
        if count < 1:
            raise ValueError("count must be at least 1")
        if count > 1:
            logger.info("agnes.single_candidate requested=%d reason=one_image_per_call", count)
        return [self.generate(request)]

    def _build_payload(self, request: ImageRequest) -> dict[str, Any]:
        """Shape one generation or editing call."""
        if not request.reference_images:
            raise ProviderError(
                "The Agnes provider needs at least one reference image; this "
                "pipeline always supplies one."
            )

        size, ratio = size_for_aspect_ratio(request.aspect_ratio)
        size = self._size or size

        logger.info(
            "agnes.generate label=%s refs=%d prompt_chars=%d model=%s size=%s ratio=%s",
            request.label,
            len(request.reference_images),
            len(request.prompt),
            self._model,
            size,
            ratio,
        )

        # References and the output format go in `extra_body`. The docs are
        # explicit that `response_format` at the top level is the common
        # mistake, and every working example puts both under `extra_body`.
        return {
            "model": self._model,
            "prompt": request.prompt,
            "size": size,
            "ratio": ratio,
            "extra_body": {
                "image": [
                    _as_data_uri(data, media_type)
                    for data, media_type in zip(
                        request.reference_images,
                        request.reference_media_types,
                        strict=True,
                    )
                ],
                "response_format": "b64_json",
            },
        }

    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        """Send the call once and hand back the raw response."""
        url = f"{self._base_url}/images/generations"
        try:
            response = self._client.post(url, json=payload)
        except httpx.HTTPError as exc:
            detail = redact(str(exc), (self._api_key,))
            raise ProviderError(f"Agnes request failed: {detail}") from None

        if response.status_code >= 400:
            raise ProviderError(
                f"Agnes request failed ({response.status_code}): "
                f"{_error_detail(response, self._api_key)}"
            )
        return response

    def _extract_image(self, response: httpx.Response) -> GeneratedImage:
        """Pull the image out of the response."""
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError("Agnes returned a response that was not JSON.") from exc

        if not isinstance(body, dict):
            raise ProviderError("Agnes returned an unexpected response shape.")

        images = body.get("data") or []
        if not images:
            raise ProviderError(
                "Agnes returned no image. The request may have been refused; try "
                "a different photo or prompt."
            )

        first = images[0]
        encoded = first.get("b64_json") if isinstance(first, dict) else None
        if not encoded:
            if isinstance(first, dict) and first.get("url"):
                raise ProviderError(
                    "Agnes returned an image URL instead of image data. This "
                    "provider expects base64 output."
                )
            raise ProviderError(
                "Agnes returned no image data. This provider expects the base64 "
                "form of the image."
            )

        try:
            raw = base64.b64decode("".join(str(encoded).split()), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ProviderError("Agnes returned undecodable image data.") from exc
        if not raw:
            raise ProviderError("Agnes returned an empty image.")

        return GeneratedImage(data=raw, media_type=_sniff_media_type(raw), model=self._model)


def _as_data_uri(data: bytes, media_type: str) -> str:
    """Wrap reference bytes in the Data URI form Agnes accepts."""
    normalised = media_type.lower()
    if normalised == "image/jpg":
        normalised = "image/jpeg"
    if normalised not in _ACCEPTED_MEDIA_TYPES:
        normalised = "image/png"
        data = _to_png(data)
    return f"data:{normalised};base64,{base64.b64encode(data).decode()}"


def _to_png(data: bytes) -> bytes:
    """Re-encode an image Agnes would not accept as PNG."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as opened:
            buffer = io.BytesIO()
            opened.convert("RGB").save(buffer, format="PNG")
            return buffer.getvalue()
    except Exception as exc:
        raise ProviderError(
            f"A reference image could not be converted for Agnes: {exc}"
        ) from exc


def _sniff_media_type(data: bytes) -> str:
    """Read the image format from its own bytes.

    Agnes may answer with PNG or JPEG depending on the model, and the app
    stores the file under the extension its media type implies — so the bytes
    decide, not the request.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _error_detail(response: httpx.Response, api_key: str) -> str:
    """Turn an error response into one short line an operator can act on."""
    detail = ""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        raw = body.get("message") or body.get("detail") or body.get("error")
        if isinstance(raw, dict):
            raw = raw.get("message") or raw
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
