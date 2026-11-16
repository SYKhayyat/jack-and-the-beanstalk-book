"""One drawn picture, made ready to judge and to keep: decoded once, on the image pool.

A drawing used to be decoded four times on its way into a book — by the plate
paste, by tier 1 of the review, by the judge's copy and by each screen copy —
and the paste then wrote it out as a PNG, the costliest encode Pillow has. On
the free plan's tenth of a CPU that was about five seconds a page, and with
twenty-one pages sharing two image threads it was most of a book's minute.

Here the drawing and its plate are decoded once, the plate is pasted back, the
tier-1 checks run on the same pixels, the judge's copy is made from them, and
the picture is stored as a JPEG (`PAGE_JPEG_QUALITY`). A drawing that comes
back as a JPEG and needs no paste is kept byte for byte.
"""

from __future__ import annotations

import io
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from PIL import Image

from app.services.image_review import Tier1Review, review_image
from app.services.media import encode_page, media_type_for, remember_judge_copy
from app.services.plate_paste import paste_plate_back
from app.services.template_store import Slot

__all__ = ["PreparedPage", "prepare_page"]

_JPEG = "image/jpeg"


@dataclass(frozen=True, slots=True)
class PreparedPage:
    """A drawing ready for the review's tier 2 and for the book."""

    #: The bytes to judge and store, and what they are.
    image: bytes
    media_type: str
    #: Tier 1's verdict on exactly those pixels.
    tier1: Tier1Review
    #: What the plate paste did, for the attempt's record; empty with no plate.
    paste: dict[str, Any] = field(default_factory=dict)


def prepare_page(
    drawn: bytes,
    *,
    plate: bytes | None,
    slots: Sequence[Slot] = (),
    paint_areas: Iterable[tuple[float, float, float, float]] = (),
    figures: Sequence[tuple[float, float, float, float]] | None = (),
    expected_ratio: float | None = None,
) -> PreparedPage:
    """Paste the plate back (when there is one), run tier 1, keep it as a JPEG.

    `figures` are the boxes of the people and animals on the drawing, which
    the paste never covers; None means nobody could say where they are, and
    then the plate is not pasted at all, so no figure can be painted over.

    Never raises on a picture it cannot read: the bytes are kept as they came
    and tier 1 fails them as undecodable, which is the attempt's verdict.
    """
    try:
        with Image.open(io.BytesIO(drawn)) as opened:
            picture = opened.convert("RGB")
    except (OSError, ValueError):
        return PreparedPage(
            image=drawn,
            media_type=media_type_for(drawn),
            tier1=review_image(drawn, expected_ratio=expected_ratio),
        )

    background = _open_plate(plate, picture.size) if plate is not None else None
    paste: dict[str, Any] = {}
    if background is not None and figures is None:
        paste = {"pasted": False, "reason": "figures_unknown"}
    elif background is not None and figures is not None:
        pasted = paste_plate_back(picture, background, slots, paint_areas, figures)
        paste = {
            "pasted": pasted.pasted,
            "restored": round(pasted.restored, 3),
            "drift": round(pasted.drift, 1),
            "figures": len(figures),
            **({"reason": "would_touch_a_figure"} if pasted.spared else {}),
        }
        picture = pasted.image

    if not paste.get("pasted") and media_type_for(drawn) == _JPEG:
        image = drawn
    else:
        image = encode_page(picture)
    tier1 = review_image(picture, plate_bytes=background, expected_ratio=expected_ratio)
    if tier1.passed:
        # Only a picture tier 1 passes is shown to the judges.
        remember_judge_copy(image, picture)
    return PreparedPage(image=image, media_type=_JPEG, tier1=tier1, paste=paste)


def _open_plate(plate: bytes, size: tuple[int, int]) -> Image.Image | None:
    """The plate's pixels, decoded straight at (about) the drawing's size."""
    try:
        with Image.open(io.BytesIO(plate)) as opened:
            opened.draft("RGB", size)
            background: Image.Image = opened.convert("RGB")
            return background
    except (OSError, ValueError):
        return None
