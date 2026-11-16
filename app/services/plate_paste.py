"""Put the approved plate back over everything a page edit was not asked to paint.

An edit pass is asked to paint the cast into the plate's slots and leave the
rest alone, and it does not quite: a lamp moves, the light warms, a fence post
goes missing. The reviewer then fails the page as "other" and a whole billable
redraw is spent on a background that was approved before the book began.

So after every edit draw the plate itself is pasted back over the picture,
except where something was meant to change:

* every person and animal the reviewer found on the drawing (`figures`, padded
  boxes from `ImageReviewer.locate_figures`) — never pasted over, however pale;
* each character's slot, widened for arms, hair and the shadow at the feet;
* the page's declared paint areas (`paint_areas` in the story seed), for what
  the story puts outside the slots — coins on the table, beans in the air;
* any strong change touching one of those, so a figure that spills past its
  box, or the basket at a child's feet, is kept whole rather than cut. "Strong"
  is a change of colour *or* of texture: a pale face or a body under a blanket
  differs from the plate in its outlines and folds more than in its colour;
* whatever those enclose (the plate's grass seen between a figure's arm and
  its side is part of the figure's drawing, not a hole to paste into);
* and, in a ring around the kept area, anything not near-identical to the
  plate, so a faint limb past a box is not ghosted by the plate behind it.

The seam is feathered. A final check compares the result with the drawing
inside every figure box: if the paste touched a figure at all, the drawing is
returned untouched instead. No image call is made: this is a few milliseconds
of Pillow on pixels already decoded (`page_picture` decodes a page once and
hands the same pixels to the paste, the review and the screen copies).

When the background differs everywhere (the model moved the camera or changed
the whole scene) a paste would put the figures into a world they were not drawn
in, so the picture is returned untouched and the review judges it as drawn.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from PIL import Image, ImageChops, ImageFilter, ImageStat

from app.services.template_store import Slot

__all__ = ["CAMERA_MOVED_LIMIT", "PasteResult", "paste_plate_back"]

_Rect = tuple[float, float, float, float]

#: Width of the grid the masks are worked out on. Fine enough to follow a
#: figure's outline, coarse enough that a pure-Python flood fill is instant.
_GRID_WIDTH = 160

#: A grid cell whose colour differs from the plate's by at least this (0-255)
#: is a real change — paint, not a shift in tone or re-encoding noise.
_STRONG_CHANGE = 48

#: A grid cell whose texture (the strength of its edges) differs from the
#: plate's by at least this (0-255) is a real change too, whatever its colour.
_STRONG_TEXTURE = 40

#: Below both of these a cell is near-identical to the plate: only such cells
#: are restored in the ring around what is kept.
_NEAR_CHANGE = 14
_NEAR_TEXTURE = 14

#: Mean colour difference (0-255) outside the kept area above which the
#: background was not drifted but replaced; pasting the plate back would then
#: cut the figures out of the scene they were drawn into.
CAMERA_MOVED_LIMIT = 30.0

#: How far past its slot a figure is still the figure, as a share of the slot
#: and as a floor share of the frame: sideways for arms and hair, a little above
#: for a raised hand or a hat, more below for the shadow and what rests at the feet.
_MARGIN_SIDE = (0.35, 0.05)
_MARGIN_TOP = (0.10, 0.03)
_MARGIN_BOTTOM = (0.20, 0.05)

#: Padding round a located figure's box, as a share of the box and a floor
#: share of the frame: the reviewer's boxes are generous but not exact.
_FIGURE_PAD = (0.08, 0.02)

#: Width of the near-identical-only ring round the kept area, in grid cells.
_RING = 4

#: Feather of the seam, as a share of the picture's width.
_FEATHER = 0.008

#: Largest change (0-255, any channel) the paste may make to a pixel inside a
#: figure's own box before the drawing is kept as drawn.
_FIGURE_TOUCH = 6


@dataclass(frozen=True, slots=True)
class PasteResult:
    """The picture to judge, and what the paste did (for the attempt's record)."""

    image: Image.Image
    #: True when the plate was pasted back; False when it was left untouched.
    pasted: bool
    #: Share of the frame restored from the plate, 0-1.
    restored: float
    #: Mean difference from the plate outside the kept area, before pasting.
    drift: float
    #: True when a paste was worked out but would have touched a figure, so
    #: the drawing was kept as drawn.
    spared: bool = False


def paste_plate_back(
    picture: Image.Image,
    plate: Image.Image,
    slots: Sequence[Slot],
    paint_areas: Iterable[_Rect] = (),
    figures: Iterable[_Rect] = (),
) -> PasteResult:
    """The drawn page with the plate restored outside the areas meant to change.

    Both are decoded RGB pictures; a plate of another size is fitted to the
    drawing. `figures` are the boxes (x, y, w, h, as shares of the frame) of
    every person and animal on the drawing. When nothing is pasted the drawing
    itself comes back, the same object, so a caller can tell its bytes need no
    re-encoding.
    """
    background = plate.convert("RGB") if plate.mode != "RGB" else plate
    if background.size != picture.size:
        background = background.resize(picture.size, Image.Resampling.BICUBIC)

    boxes = [_clamped(rect) for rect in figures]
    grid = (_GRID_WIDTH, max(1, round(_GRID_WIDTH * picture.height / picture.width)))
    change, texture = _differences(picture, background, grid)
    keep = _keep_mask(
        grid,
        change,
        texture,
        [
            *(_widened(slot.rect) for slot in slots),
            *paint_areas,
            *(_padded(box) for box in boxes),
        ],
    )

    outside = ImageChops.invert(keep)
    restored = _share(outside)
    drift = ImageStat.Stat(change, mask=outside).mean[0] if restored else 0.0
    if restored == 0.0 or drift > CAMERA_MOVED_LIMIT:
        return PasteResult(picture, pasted=False, restored=0.0, drift=drift)

    # The feather is worked out at a quarter of the size and then enlarged: a
    # blur this soft looks the same either way, at a sixteenth of the cost.
    quarter = (max(1, picture.width // 4), max(1, picture.height // 4))
    radius = max(0.25, _FEATHER * picture.width / 4)
    mask = (
        keep.resize(quarter, Image.Resampling.BILINEAR)
        .filter(ImageFilter.GaussianBlur(radius))
        .resize(picture.size, Image.Resampling.BILINEAR)
    )
    result = Image.composite(picture, background, mask)
    if _touches(result, picture, boxes):
        return PasteResult(picture, pasted=False, restored=0.0, drift=drift, spared=True)
    return PasteResult(result, pasted=True, restored=restored, drift=drift)


def _differences(
    picture: Image.Image, background: Image.Image, grid: tuple[int, int]
) -> tuple[Image.Image, Image.Image]:
    """Per grid cell: how far the colour moved, and how far the texture moved.

    The texture is the edge strength of a picture twice the grid's size,
    averaged down to the grid: a cell with outlines or folds the plate lacks
    (or lacking the plate's grass blades) differs in it even at the same colour.
    """
    small = [image.resize(grid, Image.Resampling.BILINEAR) for image in (picture, background)]
    change = ImageChops.difference(*small)
    colour = ImageChops.lighter(ImageChops.lighter(*change.split()[:2]), change.split()[2])
    double = (grid[0] * 2, grid[1] * 2)
    edges = [
        image.convert("L")
        .resize(double, Image.Resampling.BILINEAR)
        .filter(ImageFilter.FIND_EDGES)
        .resize(grid, Image.Resampling.BOX)
        for image in (picture, background)
    ]
    return colour, ImageChops.difference(*edges)


def _keep_mask(
    grid: tuple[int, int],
    change: Image.Image,
    texture: Image.Image,
    kept: Sequence[_Rect],
) -> Image.Image:
    """255 where the drawing is kept, 0 where the plate goes back, on the grid."""
    width, height = grid
    cells = width * height
    base = bytearray(cells)
    for rect in kept:
        x0, y0, x1, y1 = _cells(rect, grid)
        for y in range(y0, y1):
            base[y * width + x0 : y * width + x1] = b"\x01" * (x1 - x0)

    colour, edges = change.tobytes(), texture.tobytes()
    # Strong change, closed over one cell so a figure's outline is one piece.
    strong = (
        Image.frombytes(
            "L",
            grid,
            bytes(
                255 if colour[i] >= _STRONG_CHANGE or edges[i] >= _STRONG_TEXTURE else 0
                for i in range(cells)
            ),
        )
        .filter(ImageFilter.MaxFilter(3))
        .tobytes()
    )

    keep = bytearray(base)
    seen = bytearray(cells)
    for start in range(cells):
        if not base[start] or not strong[start] or seen[start]:
            continue
        # Every strong change connected to a kept area is part of what was painted.
        seen[start] = 1
        _spread(start, grid, keep, lambda cell: bool(strong[cell]) and not seen[cell], seen)

    _fill_enclosed(grid, keep)

    # In a ring round what is kept, only the near-identical goes back.
    ring = (
        Image.frombytes("L", grid, bytes(255 if cell else 0 for cell in keep))
        .filter(ImageFilter.MaxFilter(2 * _RING + 1))
        .tobytes()
    )
    for i in range(cells):
        if (
            ring[i]
            and not keep[i]
            and (colour[i] >= _NEAR_CHANGE or edges[i] >= _NEAR_TEXTURE)
        ):
            keep[i] = 1
    _fill_enclosed(grid, keep)
    return Image.frombytes("L", grid, bytes(255 if cell else 0 for cell in keep))


def _spread(
    start: int,
    grid: tuple[int, int],
    keep: bytearray,
    joins: Callable[[int], bool],
    seen: bytearray,
) -> None:
    """Mark `start` and every cell `joins` reaches from it, four-connected."""
    width, height = grid
    stack = [start]
    while stack:
        cell = stack.pop()
        keep[cell] = 1
        x, y = cell % width, cell // width
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < width and 0 <= ny < height:
                neighbour = ny * width + nx
                if joins(neighbour):
                    seen[neighbour] = 1
                    stack.append(neighbour)


def _fill_enclosed(grid: tuple[int, int], keep: bytearray) -> None:
    """Keep every restorable cell the kept area closes in, cut off from the edge."""
    width, height = grid
    outside = bytearray(len(keep))
    border = [
        *(x for x in range(width)),
        *((height - 1) * width + x for x in range(width)),
        *(y * width for y in range(height)),
        *(y * width + width - 1 for y in range(height)),
    ]
    stack = [cell for cell in border if not keep[cell]]
    for cell in stack:
        outside[cell] = 1
    while stack:
        cell = stack.pop()
        x, y = cell % width, cell // width
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < width and 0 <= ny < height:
                neighbour = ny * width + nx
                if not keep[neighbour] and not outside[neighbour]:
                    outside[neighbour] = 1
                    stack.append(neighbour)
    for i, reached in enumerate(outside):
        if not reached:
            keep[i] = 1


def _touches(result: Image.Image, picture: Image.Image, boxes: Sequence[_Rect]) -> bool:
    """Whether the paste changed any pixel inside a figure's own box."""
    for x, y, w, h in boxes:
        area = (
            int(x * picture.width),
            int(y * picture.height),
            max(int(x * picture.width) + 1, round((x + w) * picture.width)),
            max(int(y * picture.height) + 1, round((y + h) * picture.height)),
        )
        moved = ImageChops.difference(result.crop(area), picture.crop(area))
        if moved.point(lambda value: 255 if value > _FIGURE_TOUCH else 0).getbbox():
            return True
    return False


def _clamped(rect: _Rect) -> _Rect:
    x, y, w, h = rect
    x0, y0 = min(1.0, max(0.0, x)), min(1.0, max(0.0, y))
    x1, y1 = min(1.0, max(x0, x + w)), min(1.0, max(y0, y + h))
    return (x0, y0, x1 - x0, y1 - y0)


def _padded(rect: _Rect) -> _Rect:
    x, y, w, h = rect
    side = max(_FIGURE_PAD[0] * w, _FIGURE_PAD[1])
    tall = max(_FIGURE_PAD[0] * h, _FIGURE_PAD[1])
    return (x - side, y - tall, w + 2 * side, h + 2 * tall)


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
