"""Pasting the approved plate back over what a page edit was not asked to paint."""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

from app.services.plate_paste import CAMERA_MOVED_LIMIT, paste_plate_back
from app.services.template_store import Slot

SIZE = (800, 600)
SKY = (120, 150, 190)
FIGURE = (60, 45, 35)
#: The child stands in the left third.
SLOT = Slot(cast_key="jack", rect=(0.1, 0.3, 0.25, 0.6), scale=0.6, facing="right")


def _png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _plate() -> bytes:
    image = Image.new("RGB", SIZE, SKY)
    # A lamp on the right: the kind of thing an edit moves or recolours.
    ImageDraw.Draw(image).rectangle((620, 100, 660, 300), fill=(240, 220, 90))
    return _png(image)


def _drawn(
    *, drift_lamp: bool = True, extra: tuple[int, int, int, int] | None = None
) -> bytes:
    image = Image.new("RGB", SIZE, SKY)
    draw = ImageDraw.Draw(image)
    # The lamp came back moved: background drift outside every slot.
    lamp_x = 560 if drift_lamp else 620
    draw.rectangle((lamp_x, 100, lamp_x + 40, 300), fill=(240, 220, 90))
    # The child, in the slot, with an arm that reaches a little past it.
    draw.ellipse((100, 200, 260, 540), fill=FIGURE)
    draw.rectangle((250, 300, 330, 330), fill=FIGURE)
    if extra is not None:
        draw.rectangle(extra, fill=(200, 40, 40))
    return _png(image)


def _pixel(data: bytes, xy: tuple[int, int]) -> tuple[int, ...]:
    return tuple(Image.open(io.BytesIO(data)).convert("RGB").getpixel(xy))


class TestPastingThePlateBack:
    def test_background_drift_is_undone_and_the_figure_kept(self) -> None:
        result = paste_plate_back(_drawn(), _plate(), (SLOT,))
        assert result.pasted is True
        assert result.media_type == "image/png"
        # The lamp is back where the plate has it, and gone from where it drifted.
        assert _pixel(result.image, (640, 200)) == (240, 220, 90)
        assert _pixel(result.image, (575, 200)) == SKY
        # The child and the arm past the slot are what was drawn.
        assert _pixel(result.image, (180, 400)) == FIGURE
        assert _pixel(result.image, (320, 315)) == FIGURE
        assert 0.0 < result.restored < 1.0

    def test_a_declared_paint_area_is_kept(self) -> None:
        """Coins on a table outside every slot are the story, not drift."""
        coins = (420, 450, 500, 500)
        drawn = _drawn(extra=coins)
        plain = paste_plate_back(drawn, _plate(), (SLOT,))
        kept = paste_plate_back(drawn, _plate(), (SLOT,), [(0.5, 0.72, 0.15, 0.14)])
        assert _pixel(plain.image, (460, 475)) == SKY
        assert _pixel(kept.image, (460, 475)) == (200, 40, 40)

    def test_strong_change_touching_a_slot_is_kept_whole(self) -> None:
        """A basket at the child's feet that spills past the slot is not cut in half."""
        drawn = _drawn(extra=(200, 520, 460, 590))
        result = paste_plate_back(drawn, _plate(), (SLOT,))
        assert _pixel(result.image, (440, 560)) == (200, 40, 40)

    def test_a_scene_drawn_somewhere_else_is_left_as_drawn(self) -> None:
        """Pasting into a different scene would cut the figures out of their world."""
        elsewhere = Image.new("RGB", SIZE, (20, 90, 30))
        ImageDraw.Draw(elsewhere).ellipse((100, 200, 260, 540), fill=FIGURE)
        drawn = _png(elsewhere)
        result = paste_plate_back(drawn, _plate(), (SLOT,))
        assert result.pasted is False
        assert result.image == drawn

    def test_a_background_changed_everywhere_is_left_as_drawn(self) -> None:
        """A relit scene: not one strong change, but different all over."""
        relit = Image.new("RGB", SIZE, tuple(c + 36 for c in SKY))
        ImageDraw.Draw(relit).ellipse((100, 200, 260, 540), fill=FIGURE)
        drawn = _png(relit)
        result = paste_plate_back(drawn, _plate(), (SLOT,))
        assert result.pasted is False
        assert result.image == drawn
        assert result.drift > CAMERA_MOVED_LIMIT

    def test_a_picture_that_cannot_be_read_is_returned_as_it_came(self) -> None:
        result = paste_plate_back(b"not a picture", _plate(), (SLOT,), media_type="image/jpeg")
        assert result.pasted is False
        assert result.image == b"not a picture"
        assert result.media_type == "image/jpeg"

    def test_a_plate_of_another_size_is_fitted_to_the_drawing(self) -> None:
        small = Image.open(io.BytesIO(_plate())).resize((400, 300))
        result = paste_plate_back(_drawn(), _png(small), (SLOT,))
        assert result.pasted is True
        assert Image.open(io.BytesIO(result.image)).size == SIZE
