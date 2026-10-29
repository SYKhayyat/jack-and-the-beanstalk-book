"""Inbound image handling: validate, normalise, strip metadata.

An upload is untrusted input. Before anything else we open it with Pillow, which
both proves it really is an image (rather than a script with a .jpg name) and
gives us a chance to rewrite it. Re-encoding is not incidental: it drops EXIF,
which on phone photos routinely carries GPS coordinates of the user's home.
"""

from __future__ import annotations

import io
import logging
import warnings
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

__all__ = [
    "MAX_EDGE_PIXELS",
    "MAX_INPUT_PIXELS",
    "DecodedImage",
    "InvalidImage",
    "decode_upload",
]

logger = logging.getLogger(__name__)

MAX_EDGE_PIXELS = 1536
# A compressed image can claim enormous dimensions while occupying very little
# upload space.  Refuse it before Pillow decodes the pixels.
#
# The ceiling has to clear an ordinary camera photo, not merely the output
# size: a 4032x3024 phone image is 12.2 MP, so `1536 * 1536 * 4` (9.4 MP)
# rejected stock uploads the page promises to shrink. 25 MP covers 12 MP and
# 24 MP cameras while keeping two concurrent decodes well inside a small
# container; anything larger is still refused before allocation.
MAX_INPUT_PIXELS = 25_000_000
_ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}


class InvalidImage(ValueError):
    """The upload is not a usable still image."""


@dataclass(frozen=True, slots=True)
class DecodedImage:
    """A validated, normalised upload."""

    data: bytes
    media_type: str

    @property
    def suffix(self) -> str:
        return "jpg" if self.media_type == "image/jpeg" else "png"


def decode_upload(raw: bytes) -> DecodedImage:
    """Validate and normalise an uploaded photo.

    Rejects anything that is not a still JPEG/PNG/WEBP/GIF, applies EXIF orientation,
    downsizes over-long edges, and re-encodes without metadata.

    Raises:
        InvalidImage: if the bytes are not a decodable still image.
    """
    if not raw:
        raise InvalidImage("The uploaded file was empty.")

    try:
        with warnings.catch_warnings():
            # Pillow's default bomb threshold is intentionally much larger than
            # this app needs. Treat its warning as an exception as an extra guard
            # for pathological dimensions before any decode occurs.
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as opened:
                if opened.width * opened.height > MAX_INPUT_PIXELS:
                    raise InvalidImage(
                        "That image has too many pixels. Please upload a smaller photo."
                    )
                if getattr(opened, "is_animated", False):
                    # Take the first frame rather than refusing. Someone who uploads
                    # an animated GIF of themselves means "here is my photo".
                    logger.info("upload.animated_using_first_frame frames=%s", opened.n_frames)
                    opened.seek(0)
                image_format = (opened.format or "").upper()
                if image_format not in _ALLOWED_FORMATS:
                    raise InvalidImage(
                        f"Unsupported image format {image_format or 'unknown'!r}. "
                        "Please upload a JPEG, PNG, WEBP or GIF."
                    )

                image = ImageOps.exif_transpose(opened)
                image = image.convert("RGB")
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise InvalidImage(
            "That image has unsafe dimensions. Please upload a smaller photo."
        ) from exc
    except UnidentifiedImageError as exc:
        raise InvalidImage(
            "That file is not a readable image. Please upload a JPEG, PNG, WEBP or GIF."
        ) from exc
    except OSError as exc:
        raise InvalidImage("That image could not be decoded; it may be corrupted.") from exc

    image.thumbnail((MAX_EDGE_PIXELS, MAX_EDGE_PIXELS), Image.Resampling.LANCZOS)

    buffer = io.BytesIO()
    # Re-encoding discards the original metadata block, including EXIF GPS data.
    image.save(buffer, format="JPEG", quality=92, optimize=True)
    return DecodedImage(data=buffer.getvalue(), media_type="image/jpeg")
