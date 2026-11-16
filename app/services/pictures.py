"""A book's pictures on screen: which sizes exist, and the URLs that name them.

The stored picture is the full-size page (a JPEG of a few hundred KB, or an
older book's PNG of 1 to 2 MB), which is what the PDF embeds. The screen gets
WebP copies instead (P1, P2): 800 and 1600 px wide for the reader, chosen by
the browser through ``srcset``, and 240 px for a
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
    "encode_variants",
    "file_variants",
    "page_slot",
    "picture_set",
    "short_version",
    "slot_page",
    "variant_locks",
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


def file_variants(
    store: BookStore, book_id: str, slot: str, sha: str, copies: dict[int, bytes]
) -> int:
    """File screen copies made from the picture whose SHA-256 is `sha`.

    Called by the render right after it saves a picture, with copies it made
    from the pixels it already had (`encode_variants`) — nothing is read back
    out of the store. A copy is filed only while the slot still holds that
    very picture, so it can never belong to one that has since been redrawn.
    Returns how many copies were filed.
    """
    if store.picture_sha(book_id, slot) != sha:
        return 0
    made = 0
    for width, data in copies.items():
        with variant_locks.hold((book_id, slot, width, sha)):
            if store.read_variant(book_id, slot, width, sha) is not None:
                continue
            store.save_variant(book_id, slot, width, sha, data)
            made += 1
    return made


def encode_variant(data: bytes, width: int) -> bytes:
    """A WebP copy of `data`, `width` pixels wide — never wider than the original.

    The original is never enlarged: a 1152 px page asked for at 1600 comes back
    at 1152, still as WebP, still a tenth of the PNG.
    """
    return encode_variants(data, (width,))[width]


def encode_variants(
    source: bytes | Image.Image, widths: tuple[int, ...] = WARM_WIDTHS
) -> dict[int, bytes]:
    """WebP copies of one picture at each of `widths`, decoded once.

    `source` is the stored bytes, or the pixels a caller already decoded.
    The copies are made widest first, each shrunk from the one before (a
    240 px thumbnail from the 800 px page, not from the 1536 px original),
    which costs a fraction of shrinking the original each time and looks the
    same at those sizes.
    """
    for width in widths:
        if width not in VARIANT_WIDTHS:
            raise ValueError(f"no variant is {width} px wide")
    copies: dict[int, bytes] = {}
    with transcoding():
        if isinstance(source, Image.Image):
            image = source.convert("RGB") if source.mode != "RGB" else source
        else:
            with Image.open(io.BytesIO(source)) as opened:
                # A JPEG is decoded straight at (about) the widest size asked.
                widest = max(widths)
                opened.draft("RGB", (widest, widest * opened.height // max(1, opened.width)))
                image = opened.convert("RGB")
        for width in sorted(set(widths), reverse=True):
            if image.width > width:
                height = max(1, round(image.height * width / image.width))
                factor = image.width // (width * 2)
                if factor >= 2:
                    # Box-reduce first: LANCZOS over the whole original is the
                    # costly part, and reduce() is exact for whole factors.
                    image = image.reduce(factor)
                image = image.resize((width, height), Image.Resampling.LANCZOS)
            quality = _THUMB_QUALITY if width == THUMB_WIDTH else _WEBP_QUALITY
            buffer = io.BytesIO()
            # method=2 is a quarter of method=4's time at the same size, give
            # or take a few percent, for a picture that is already lossy.
            image.save(buffer, format="WEBP", quality=quality, method=2)
            copies[width] = buffer.getvalue()
    return copies
