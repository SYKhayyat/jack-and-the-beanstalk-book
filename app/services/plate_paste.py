"""Put the approved plate back over everything a page edit was not asked to paint.

An edit pass is asked to paint the cast into the plate's slots and leave the
rest alone, and it does not quite: a lamp moves, the light warms, a fence post
goes missing. The reviewer then fails the page as "other" and a whole billable
redraw is spent on a background that was approved before the book began.

So after every edit draw the plate itself is pasted back over the picture,
except where something was meant to change:

* each character's slot, widened for arms, hair and the shadow at the feet;
* the page's declared paint areas (`paint_areas` in the story seed), for what
  the story puts outside the slots — coins on the table, beans in the air;
* any strong change that touches one of those, so a figure that spills past
  its slot, or the basket at a child's feet, is kept whole rather than cut.

The seam is feathered. No image call is made: this is a few milliseconds of
Pillow on a picture already paid for.

When the background differs everywhere (the model moved the camera or changed
the whole scene) a paste would put the figures into a world they were not drawn
in, so the picture is returned untouched and the review judges it as drawn.
"""

from __future__ import annotations

import io
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from PIL import Image, ImageChops, ImageFilter, ImageStat

from app.services.media import FAST_PNG_LEVEL
from app.services.template_store import Slot

__all__ = ["CAMERA_MOVED_LIMIT", "PasteResult", "paste_plate_back"]

_Rect = tuple[float, float, float, float]

#: Width of the grid the masks are worked out on. Fine enough to follow a
#: figure's outline, coarse enough that a pure-Python flood fill is instant.
_GRID_WIDTH = 160

#: A grid cell whose difference from the plate is at least this (0-255) is a
#: real change — paint, not a shift in tone or re-encoding noise.
_STRONG_CHANGE = 48

#: Mean difference (0-255) outside the kept area above which the background
#: was not drifted but replaced; pasting the plate back would then cut the
#: figures out of the scene they were drawn into.
CAMERA_MOVED_LIMIT = 30.0

#: How far past its slot a figure is still the figure, as a share of the slot
#: and as a floor share of the frame: sideways for arms and hair, a little above
#: for a raised hand or a hat, more below for the shadow and what rests at the feet.
_MARGIN_SIDE = (0.35, 0.05)
_MARGIN_TOP = (0.10, 0.03)
_MARGIN_BOTTOM = (0.20, 0.05)

#: Feather of the seam, as a share of the picture's width.
_FEATHER = 0.008


@dataclass(frozen=True, slots=True)
class PasteResult:
    """The picture to judge, and what the paste did (for the attempt's record)."""

    image: bytes
    media_type: str
    #: True when the plate was pasted back; False when it was left untouched.
    pasted: bool
    #: Share of the frame restored from the plate, 0-1.
    restored: float
    #: Mean difference from the plate outside the kept area, before pasting.
    drift: float


def paste_plate_back(
    drawn: bytes,
    plate: bytes,
    slots: Sequence[Slot],
    paint_areas: Iterable[_Rect] = (),
    *,
    media_type: str = "image/png",
) -> PasteResult:
    """The drawn page with the plate restored outside the areas meant to change.

    Never raises on a picture it cannot read: the drawing is then returned as
    it came, and tier 1 of the review says what is wrong with it.
    """
    try:
        picture = _open(drawn)
        background = _open(plate).resize(picture.size, Image.Resampling.LANCZOS)
    except (OSError, ValueError):
        return PasteResult(drawn, media_type, pasted=False, restored=0.0, drift=0.0)

    grid = (_GRID_WIDTH, max(1, round(_GRID_WIDTH * picture.height / picture.width)))
    change = ImageChops.difference(
        picture.resize(grid, Image.Resampling.BILINEAR),
        background.resize(grid, Image.Resampling.BILINEAR),
    ).convert("L")
    keep = _keep_mask(grid, change, slots, paint_areas)

    outside = ImageChops.invert(keep)
    restored = _share(outside)
    drift = ImageStat.Stat(change, mask=outside).mean[0] if restored else 0.0
    if restored == 0.0 or drift > CAMERA_MOVED_LIMIT:
        return PasteResult(drawn, media_type, pasted=False, restored=0.0, drift=drift)

    radius = max(1.0, _FEATHER * picture.width)
    mask = keep.resize(picture.size, Image.Resampling.BILINEAR).filter(
        ImageFilter.GaussianBlur(radius)
    )
    out = io.BytesIO()
    # The fast level (see `FAST_PNG_LEVEL`): this encode, at Pillow's default,
    # was the costliest step of a render, up to three times per plate page.
    Image.composite(picture, background, mask).save(
        out, format="PNG", compress_level=FAST_PNG_LEVEL
    )
    return PasteResult(
        out.getvalue(), "image/png", pasted=True, restored=restored, drift=drift
    )


def _keep_mask(
    grid: tuple[int, int],
    change: Image.Image,
    slots: Sequence[Slot],
    paint_areas: Iterable[_Rect],
) -> Image.Image:
    """255 where the drawing is kept, 0 where the plate goes back, on the grid."""
    width, height = grid
    base = bytearray(width * height)
    for rect in [*(_widened(slot.rect) for slot in slots), *paint_areas]:
        x0, y0, x1, y1 = _cells(rect, grid)
        for y in range(y0, y1):
            base[y * width + x0 : y * width + x1] = b"\x01" * (x1 - x0)

    # Strong change, closed over one cell so a figure's outline is one piece.
    strong = change.point(lambda value: 255 if value >= _STRONG_CHANGE else 0).filter(
        ImageFilter.MaxFilter(3)
    )
    strong_cells = strong.tobytes()

    keep = bytearray(base)
    seen = bytearray(width * height)
    for start in range(width * height):
        if not base[start] or not strong_cells[start] or seen[start]:
            continue
        # Every strong change connected to a kept area is part of what was painted.
        stack = [start]
        seen[start] = 1
        while stack:
            cell = stack.pop()
            keep[cell] = 1
            x, y = cell % width, cell // width
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if 0 <= nx < width and 0 <= ny < height:
                    neighbour = ny * width + nx
                    if strong_cells[neighbour] and not seen[neighbour]:
                        seen[neighbour] = 1
                        stack.append(neighbour)
    return Image.frombytes("L", grid, bytes(255 if cell else 0 for cell in keep))


def _widened(rect: _Rect) -> _Rect:
    x, y, w, h = rect
    side = max(_MARGIN_SIDE[0] * w, _MARGIN_SIDE[1])
    top = max(_MARGIN_TOP[0] * h, _MARGIN_TOP[1])
    bottom = max(_MARGIN_BOTTOM[0] * h, _MARGIN_BOTTOM[1])
    return (x - side, y - top, w + 2 * side, h + top + bottom)


def _cells(rect: _Rect, grid: tuple[int, int]) -> tuple[int, int, int, int]:
    """A fractional rect as clamped grid bounds (x0, y0, x1, y1), end-exclusive."""
    width, height = grid
    x, y, w, h = rect
    x0 = min(width, max(0, int(x * width)))
    y0 = min(height, max(0, int(y * height)))
    x1 = min(width, max(x0, round((x + w) * width + 0.5)))
    y1 = min(height, max(y0, round((y + h) * height + 0.5)))
    return x0, y0, x1, y1


def _share(mask: Image.Image) -> float:
    return ImageStat.Stat(mask).mean[0] / 255.0


def _open(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image.convert("RGB")
