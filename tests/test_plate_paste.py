"""Pasting the approved plate back over what a page edit was not asked to paint."""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from app.services.plate_paste import CAMERA_MOVED_LIMIT, paste_plate_back
from app.services.template_store import Slot

SIZE = (800, 600)
SKY = (120, 150, 190)
FIGURE = (60, 45, 35)
#: The child stands in the left third.
SLOT = Slot(cast_key="jack", rect=(0.1, 0.3, 0.25, 0.6), scale=0.6, facing="right")


def _plate() -> Image.Image:
    image = Image.new("RGB", SIZE, SKY)
    # A lamp on the right: the kind of thing an edit moves or recolours.
    ImageDraw.Draw(image).rectangle((620, 100, 660, 300), fill=(240, 220, 90))
    return image


def _drawn(
    *, drift_lamp: bool = True, extra: tuple[int, int, int, int] | None = None
) -> Image.Image:
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
    return image


def _pixel(image: Image.Image, xy: tuple[int, int]) -> tuple[int, ...]:
    return tuple(image.convert("RGB").getpixel(xy))


class TestPastingThePlateBack:
    def test_background_drift_is_undone_and_the_figure_kept(self) -> None:
        result = paste_plate_back(_drawn(), _plate(), (SLOT,))
        assert result.pasted is True
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
        drawn = elsewhere
        result = paste_plate_back(drawn, _plate(), (SLOT,))
        assert result.pasted is False
        assert result.image is drawn

    def test_a_background_changed_everywhere_is_left_as_drawn(self) -> None:
        """A relit scene: not one strong change, but different all over."""
        relit = Image.new("RGB", SIZE, tuple(c + 36 for c in SKY))
        ImageDraw.Draw(relit).ellipse((100, 200, 260, 540), fill=FIGURE)
        drawn = relit
        result = paste_plate_back(drawn, _plate(), (SLOT,))
        assert result.pasted is False
        assert result.image is drawn
        assert result.drift > CAMERA_MOVED_LIMIT

    def test_a_plate_of_another_size_is_fitted_to_the_drawing(self) -> None:
        small = _plate().resize((400, 300))
        result = paste_plate_back(_drawn(), small, (SLOT,))
        assert result.pasted is True
        assert result.image.size == SIZE


GRASS = (90, 150, 70)
#: A pale figure: close to the grass in colour, as the see-through pages were.
PALE = (118, 168, 98)


def _grass_plate() -> Image.Image:
    """A plate with texture: blades of grass, the kind a pale figure hides in."""
    image = Image.new("RGB", SIZE, GRASS)
    draw = ImageDraw.Draw(image)
    for x in range(0, SIZE[0], 12):
        draw.line((x, SIZE[1], x + 4, 380), fill=(70, 125, 55), width=2)
    return image


def _pale_figure(plate: Image.Image, box: tuple[int, int, int, int]) -> Image.Image:
    """The plate with a pale figure painted over it: little colour change, new outline."""
    image = plate.copy()
    ImageDraw.Draw(image).ellipse(box, fill=PALE, outline=(60, 60, 50), width=3)
    return image


class TestFiguresAreNeverPastedOver:
    def test_a_pale_figure_outside_every_slot_is_kept_when_located(self) -> None:
        plate = _grass_plate()
        drawn = _pale_figure(plate, (520, 260, 640, 560))
        # Without its box the plate goes back over it: the see-through page.
        ghost = paste_plate_back(drawn, plate, (SLOT,))
        assert _pixel(ghost.image, (580, 300)) != PALE
        located = paste_plate_back(drawn, plate, (SLOT,), figures=[(0.65, 0.43, 0.15, 0.5)])
        assert _pixel(located.image, (580, 300)) == PALE

    def test_a_pale_figure_touching_a_slot_is_kept_by_its_outline(self) -> None:
        """Its colour is under the strong-change bar; its outline is not."""
        plate = _grass_plate()
        drawn = _pale_figure(plate, (250, 220, 420, 560))
        result = paste_plate_back(drawn, plate, (SLOT,))
        assert _pixel(result.image, (400, 390)) == PALE

    def test_what_a_figure_encloses_is_kept(self) -> None:
        """The plate is not pasted into a hole the drawing closed in."""
        plate = _plate()
        drawn = plate.copy()
        draw = ImageDraw.Draw(drawn)
        # A ring-shaped figure (an arm on the hip) with the sky inside it, tinted.
        draw.ellipse((300, 200, 560, 460), fill=(150, 170, 200))
        draw.ellipse((300, 200, 560, 460), outline=FIGURE, width=14)
        result = paste_plate_back(drawn, plate, (SLOT,), figures=[(0.375, 0.333, 0.03, 0.43)])
        assert _pixel(result.image, (430, 330)) == (150, 170, 200)

    def test_background_drift_far_from_every_figure_is_still_undone(self) -> None:
        result = paste_plate_back(
            _drawn(), _plate(), (SLOT,), figures=[(0.12, 0.33, 0.22, 0.6)]
        )
        assert result.pasted is True
        assert _pixel(result.image, (575, 200)) == SKY
        assert _pixel(result.image, (180, 400)) == FIGURE

    def test_a_paste_that_would_touch_a_figure_is_not_made(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The final check: a figure is drawn as drawn, or the page is left alone."""
        import app.services.plate_paste as paste

        monkeypatch.setattr(paste, "_padded", lambda _rect: (0.0, 0.0, 0.0, 0.0))
        drawn = _drawn()
        result = paste.paste_plate_back(drawn, _plate(), (), figures=[(0.69, 0.1, 0.1, 0.5)])
        assert result.pasted is False
        assert result.spared is True
        assert result.image is drawn
