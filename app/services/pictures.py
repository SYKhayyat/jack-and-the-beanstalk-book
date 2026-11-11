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
from typing import TYPE_CHECKING

from PIL import Image

from app.single_flight import KeyedLocks
from app.transcoding import transcoding

if TYPE_CHECKING:
    from app.services.book_store import BookStore

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
    "variant_locks",
    "warm_variants",
]

logger = logging.getLogger(__name__)

#: What the reader may ask for. 800 covers a phone and a laptop's half-spread
#: at 2x; 1600 a full-width page on a large or high-density screen.
DISPLAY_WIDTHS: tuple[int, ...] = (800, 1600)
THUMB_WIDTH = 240
VARIANT_WIDTHS = frozenset((THUMB_WIDTH, *DISPLAY_WIDTHS))
VARIANT_MEDIA_TYPE = "image/webp"

#: The copies made the moment a picture is saved: the thumbnail the library and
#: the page map show, and the 800 px page a phone reads. The 1600 px copy stays
#: lazy — only a large screen asks for it, and making 42 of them while the
#: reader waits was the stall (S2/B12).
WARM_WIDTHS: tuple[int, ...] = (THUMB_WIDTH, DISPLAY_WIDTHS[0])

#: One screen copy at a time per picture and width, shared by the warm-up that
#: follows a save and by the lazy encode in the media route, so the two can
#: never make the same copy twice.
variant_locks = KeyedLocks()

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


def warm_variants(
    store: BookStore, book_id: str, slot: str, widths: tuple[int, ...] = WARM_WIDTHS
) -> int:
    """Make the copies a picture is normally viewed at, now that it is saved.

    Runs on the render's image pool, right after the picture is stored, so a
    reader opening the finished book finds the copies already there instead of
    paying for them one at a time while the first page loads.

    The copy is made from the picture read back out of the store and filed
    under the hash the store recorded, so it can never belong to a picture
    that has since been redrawn. Returns how many copies were made.
    """
    sha = store.picture_sha(book_id, slot)
    if sha is None:
        return 0
    picture = store.read_picture(book_id, slot)
    if picture is None:
        return 0
    made = 0
    for width in widths:
        with variant_locks.hold((book_id, slot, width, sha)):
            if store.read_variant(book_id, slot, width, sha) is not None:
                continue
            store.save_variant(book_id, slot, width, sha, encode_variant(picture, width))
            made += 1
    return made


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
