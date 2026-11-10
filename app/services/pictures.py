"""A book's pictures on screen: which sizes exist, and the URLs that name them.

The stored picture is the provider's full-size PNG (1 to 2 MB), which is what the
PDF embeds. The screen gets WebP copies instead (P1, P2): 800 and 1600 px wide
for the reader, chosen by the browser through ``srcset``, and 240 px for a
thumbnail. Each URL carries the picture's version — the first 16 hex digits of
its SHA-256 — so the browser may keep it for a year: a redrawn picture hashes
differently and arrives under a new address, never from an old copy.

Everything that hands a picture URL to a browser (the status document, the
book page, the library) builds it here, so the sizes and the scheme are stated
once.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass

from PIL import Image

from app.transcoding import transcoding

__all__ = [
    "DISPLAY_WIDTHS",
    "THUMB_WIDTH",
    "VARIANT_MEDIA_TYPE",
    "VARIANT_WIDTHS",
    "PictureSet",
    "encode_variant",
    "page_slot",
    "picture_set",
    "short_version",
    "slot_page",
]

logger = logging.getLogger(__name__)

#: What the reader may ask for. 800 covers a phone and a laptop's half-spread
#: at 2x; 1600 a full-width page on a large or high-density screen.
DISPLAY_WIDTHS: tuple[int, ...] = (800, 1600)
THUMB_WIDTH = 240
VARIANT_WIDTHS = frozenset((THUMB_WIDTH, *DISPLAY_WIDTHS))
VARIANT_MEDIA_TYPE = "image/webp"

_VERSION_DIGITS = 16
# Quality 80 is visually indistinguishable from the PNG at reading size, and a
# 1152x864 page lands near 100 to 150 KB instead of 1.3 MB.
_WEBP_QUALITY = 80
_THUMB_QUALITY = 70


@dataclass(frozen=True, slots=True)
class PictureSet:
    """Everything an ``<img>`` needs for one picture."""

    src: str
    srcset: str
    thumb: str

    def as_dict(self) -> dict[str, str]:
        return {"src": self.src, "srcset": self.srcset, "thumb": self.thumb}


def short_version(sha256: str) -> str:
    """The version a URL carries: enough of the hash to never collide in a book."""
    return sha256[:_VERSION_DIGITS]


def page_slot(page_number: int) -> str:
    """The variant slot of a story page ("page-7"); the cover is "cover"."""
    return "cover" if page_number == 0 else f"page-{page_number}"


def slot_page(slot: str) -> int | None:
    """The page number in a "page-<n>" slot, or None for "cover", "portrait"
    and anything that is not a slot."""
    prefix, _, number = slot.partition("-")
    if prefix != "page" or not number.isdigit():
        return None
    value = int(number)
    return value if value >= 1 else None


def picture_set(url: str, sha256: str | None) -> PictureSet:
    """The sized, versioned URLs for the picture at `url`.

    A picture with no recorded hash (none should remain after migration 0011,
    but a row is not a promise) falls back to the original URL, revalidated on
    every view, rather than to a version that would be wrong.
    """
    if not sha256:
        return PictureSet(src=url, srcset="", thumb=url)
    version = short_version(sha256)

    def sized(width: int) -> str:
        return f"{url}?w={width}&v={version}"

    return PictureSet(
        src=sized(DISPLAY_WIDTHS[-1]),
        srcset=", ".join(f"{sized(width)} {width}w" for width in DISPLAY_WIDTHS),
        thumb=sized(THUMB_WIDTH),
    )


def encode_variant(data: bytes, width: int) -> bytes:
    """A WebP copy of `data`, `width` pixels wide — never wider than the original.

    The original is never enlarged: a 1152 px page asked for at 1600 comes back
    at 1152, still as WebP, still a tenth of the PNG.
    """
    if width not in VARIANT_WIDTHS:
        raise ValueError(f"no variant is {width} px wide")
    quality = _THUMB_QUALITY if width == THUMB_WIDTH else _WEBP_QUALITY
    buffer = io.BytesIO()
    with transcoding():
        with Image.open(io.BytesIO(data)) as opened:
            image = opened.convert("RGB")
        if image.width > width:
            height = max(1, round(image.height * width / image.width))
            image = image.resize((width, height), Image.Resampling.LANCZOS)
        image.save(buffer, format="WEBP", quality=quality, method=4)
    return buffer.getvalue()
