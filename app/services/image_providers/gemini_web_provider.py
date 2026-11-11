"""Pictures from Gemini in a local Chrome: the free provider for local books.

Same contract as every provider: the assembled prompt and the reference
images in, one picture out. Gemini answers in a chat, so the request is framed
as one: "make this image", then the app's own prompt. The picture comes back
as the full-size JPEG (about 2400 x 1800), never the on-page preview.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.services.gemini_web.attachment import Attachment
from app.services.image_providers.base import GeneratedImage, ImageProvider, ImageRequest
from app.services.media import media_type_for

if TYPE_CHECKING:
    from app.services.gemini_web.client import GeminiWeb

__all__ = ["GeminiWebImageProvider"]

logger = logging.getLogger(__name__)

MODEL = "gemini-web"

#: Gemini replies in prose unless the turn plainly asks for a picture.
_FRAME = "Generate one {aspect} image, and only the image, following this brief.\n\n{prompt}"


class GeminiWebImageProvider(ImageProvider):
    name = "gemini_web"

    def __init__(self, client: GeminiWeb) -> None:
        self._client = client

    def generate(self, request: ImageRequest) -> GeneratedImage:
        images = tuple(
            Attachment(data, media_type_for(data)) for data in request.reference_images
        )
        logger.info(
            "gemini_web.draw label=%s references=%d prompt_chars=%d",
            request.label,
            len(images),
            len(request.prompt),
        )
        data = self._client.draw(
            _FRAME.format(aspect=request.aspect_ratio, prompt=request.prompt), images
        )
        return GeneratedImage(data=data, media_type=media_type_for(data), model=MODEL)
