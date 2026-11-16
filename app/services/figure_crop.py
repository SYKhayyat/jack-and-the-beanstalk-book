"""Figure-crop edits: only the stand-in child is redrawn, the rest of the page is kept.

An approved *scene* is a finished picture of a page with a stand-in child in it
and the box that child stands in (`TemplateStore.add_premade` with a figure).
A book that may use it does not send the whole page to be drawn again: it cuts
out the stand-in with room around them (`cut`), the image model turns them into
the book's child — face, skin and hair — and the result is blended back into
the scene (`blend_back`) under a feathered superellipse mask, after a mean
colour shift so the crop's light matches the page around it (audit 11-15 A5,
after one of the other implementations).

Everything outside the crop is the scene's own pixels, so the background, the
supporting characters and the framing are identical in every book, and the
call is smaller than a whole-page edit.

The crop has the page's own shape, so the image model's picture, drawn at the
book's aspect ratio, maps back onto it without stretching.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass

from PIL import Image, ImageChops, ImageStat

__all__ = [
    "CROP_MARGIN",
    "SceneCrop",
    "blend_back",
    "crop_box",
    "cut",
    "valid_figure",
]

Box = tuple[float, float, float, float]

#: Room left around the figure on each side, as a share of its own size: the
#: model needs the figure's surroundings to light and shadow them, and the
#: feather needs somewhere to fade out that is not the child.
CROP_MARGIN = 0.35

#: How far past the figure's box the mask stays fully the new drawing, as a
#: share of the box: a pose that moved a hand a little is still kept whole.
_SOLID_PAST_BOX = 0.12

#: The superellipse's exponent: 2 is an ellipse, higher is squarer. A box-like
#: figure wants a squarish mask, but its corners still fade.
_SUPERELLIPSE = 4.0

#: The mask is worked out at this size and smoothed up to the crop's.
_MASK_GRID = 96

#: The largest mean-colour correction applied, per channel (of 255). A shift
#: larger than this is the model having repainted the light, which the review
#: is there to fail, not something to hide by recolouring.
_MAX_SHIFT = 24.0


@dataclass(frozen=True, slots=True)
class SceneCrop:
    """An approved scene and where its stand-in child is, as a book draws onto it."""

    image: bytes
    #: The stand-in's box: (x, y, width, height) as shares of the picture.
    figure: Box


def valid_figure(figure: Sequence[float]) -> Box | None:
    """`figure` as a box when it is one inside the picture, else None."""
    if len(figure) != 4:
        return None
    x, y, w, h = (float(v) for v in figure)
    if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > 1.0001 or y + h > 1.0001:
        return None
    return (x, y, w, h)


def crop_box(figure: Box, size: tuple[int, int]) -> tuple[int, int, int, int]:
    """The crop (left, top, right, bottom) in pixels around `figure` on a page of `size`.

    The figure plus `CROP_MARGIN` on every side, widened or heightened to the
    page's own shape, and slid (never shrunk) to stay inside the page.
    """
    width, height = size
    x, y, w, h = figure
    need_w = w * width * (1 + 2 * CROP_MARGIN)
    need_h = h * height * (1 + 2 * CROP_MARGIN)
    ratio = width / height
    if need_w / need_h < ratio:
        need_w = need_h * ratio
    else:
        need_h = need_w / ratio
    scale = min(1.0, width / need_w, height / need_h)
    crop_w = max(1, round(need_w * scale))
    crop_h = max(1, round(need_h * scale))
    centre_x = (x + w / 2) * width
    centre_y = (y + h / 2) * height
    left = min(max(0, round(centre_x - crop_w / 2)), width - crop_w)
    top = min(max(0, round(centre_y - crop_h / 2)), height - crop_h)
    return (left, top, left + crop_w, top + crop_h)


def cut(scene: bytes, figure: Box) -> bytes:
    """The part of the scene the model redraws, as a JPEG."""
    with Image.open(io.BytesIO(scene)) as opened:
        picture = opened.convert("RGB")
    crop = picture.crop(crop_box(figure, picture.size))
    out = io.BytesIO()
    crop.save(out, format="JPEG", quality=95, subsampling=0)
    return out.getvalue()


def blend_back(scene: Image.Image, drawn: Image.Image, figure: Box) -> Image.Image:
    """`drawn` (the model's crop) blended into `scene` around the figure.

    The drawing is fitted to the crop, its mean colour outside the figure is
    shifted to the scene's there, and it is laid over the scene under a mask
    that is solid over the figure and fades to nothing at the crop's edge, so
    no seam shows where the scene's own pixels take over again.
    """
    box = crop_box(figure, scene.size)
    left, top, right, bottom = box
    size = (right - left, bottom - top)
    original = scene.crop(box)
    fitted = drawn.convert("RGB").resize(size, Image.Resampling.LANCZOS)
    inner = _figure_in_crop(figure, scene.size, box)
    fitted = _match_colour(fitted, original, inner)
    mask = _feather_mask(size, inner)
    merged = Image.composite(fitted, original, mask)
    result = scene.copy()
    result.paste(merged, (left, top))
    return result


def _figure_in_crop(
    figure: Box, size: tuple[int, int], box: tuple[int, int, int, int]
) -> tuple[float, float, float, float]:
    """The figure's box in the crop's own pixels (left, top, right, bottom)."""
    width, height = size
    x, y, w, h = figure
    return (
        x * width - box[0],
        y * height - box[1],
        (x + w) * width - box[0],
        (y + h) * height - box[1],
    )


def _match_colour(
    drawn: Image.Image, original: Image.Image, inner: tuple[float, float, float, float]
) -> Image.Image:
    """`drawn` with its mean colour around the figure moved to the scene's there."""
    ring = Image.new("L", drawn.size, 255)
    ring.paste(0, tuple(round(v) for v in inner))  # type: ignore[arg-type]
    if ImageStat.Stat(ring).sum[0] == 0:
        return drawn
    want = ImageStat.Stat(original, ring).mean
    have = ImageStat.Stat(drawn, ring).mean
    bands = []
    for band, target, current in zip(drawn.split(), want, have, strict=True):
        shift = max(-_MAX_SHIFT, min(_MAX_SHIFT, target - current))
        bands.append(band.point(lambda v, s=shift: max(0, min(255, round(v + s)))))
    return Image.merge("RGB", bands)


def _feather_mask(
    size: tuple[int, int], inner: tuple[float, float, float, float]
) -> Image.Image:
    """255 over the figure, easing to 0 at the crop's edge (a superellipse)."""
    width, height = size
    left, top, right, bottom = inner
    cx, cy = (left + right) / 2, (top + bottom) / 2
    a = max(1.0, (right - left) / 2 * (1 + _SOLID_PAST_BOX))
    b = max(1.0, (bottom - top) / 2 * (1 + _SOLID_PAST_BOX))
    # The superellipse "radius" (1 on the solid edge) at which the mask is
    # gone: where it first meets the crop's edge, so the edge is all scene.
    outer = max(1.0001, min(cx / a, (width - cx) / a, cy / b, (height - cy) / b))
    grid_w = _MASK_GRID
    grid_h = max(1, round(_MASK_GRID * height / width))
    small = Image.new("L", (grid_w, grid_h))
    values = []
    for gy in range(grid_h):
        py = (gy + 0.5) * height / grid_h
        for gx in range(grid_w):
            px = (gx + 0.5) * width / grid_w
            dx = abs((px - cx) / a) ** _SUPERELLIPSE
            dy = abs((py - cy) / b) ** _SUPERELLIPSE
            r = (dx + dy) ** (1 / _SUPERELLIPSE)
            t = min(1.0, max(0.0, (outer - r) / (outer - 1)))
            values.append(round(255 * t * t * (3 - 2 * t)))
    small.putdata(values)
    mask = small.resize(size, Image.Resampling.BILINEAR)
    # The crop's own border is always the scene, whatever the smoothing did.
    border = Image.new("L", size, 0)
    border.paste(255, (1, 1, max(1, width - 1), max(1, height - 1)))
    return ImageChops.multiply(mask, border)
