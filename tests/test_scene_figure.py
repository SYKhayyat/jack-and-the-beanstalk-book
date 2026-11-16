"""Scenes: a star page drawn by redrawing only its stand-in child (audit 11-15 A5).

* **The crop.** It holds the figure with room around it, has the page's shape,
  and stays inside the page.
* **The blend.** The new drawing covers the figure; the crop's edge and
  everything outside it stay the scene's own pixels.
* **The store.** A star page's premade is a scene and needs its figure; a
  starless page's premade has none; scenes pin like any premade.
* **The importer.** A star page's picture comes with a `<stem>.json` figure.
* **The prompt.** It swaps the child, skin and hair included, and keeps the rest.
* **The runner.** A scene is used only for a fixed-costume star page with no
  visitor photo of anyone else on it and no fix asked for; the drawn crop is
  blended back into the scene before it is reviewed.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from PIL import Image, ImageChops
from sqlalchemy import Engine

from app.models.settings import CartoonSettings
from app.scripts.import_premades import ImportError_
from app.scripts.import_premades import run as import_run
from app.services.book_runner import _premade_for, _scene_for
from app.services.figure_crop import (
    SceneCrop,
    _feather_mask,
    _figure_in_crop,
    blend_back,
    crop_box,
    cut,
    valid_figure,
)
from app.services.page_picture import prepare_page
from app.services.prompts import build_figure_edit_prompt
from app.services.template_store import PinnedPremade, TemplateError

from .test_review_loop import ScriptedReviewer, attempt_loop, runner_with, said
from .test_template_portraits import _approve_every_plate
from .test_template_premades import (
    CUPBOARD,
    OPERATOR,
    RED,
    _artwork,
    _book,
    _jpeg,
    _red_page,
    _store,
)

FIGURE = (0.4, 0.3, 0.2, 0.5)
STAR_PAGE = 1


def _scene_jpeg(size: tuple[int, int] = (400, 300)) -> bytes:
    """A scene with a left-to-right gradient, so a shift or a stretch would show."""
    width, height = size
    picture = Image.new("RGB", size)
    picture.putdata([(x * 255 // width, 120, 60) for _ in range(height) for x in range(width)])
    out = io.BytesIO()
    picture.save(out, format="JPEG", quality=95)
    return out.getvalue()


def _scene_pinned(number: int = STAR_PAGE) -> PinnedPremade:
    return PinnedPremade(page_number=number, premade_id="s" * 32, figure=FIGURE)


# --- the crop -------------------------------------------------------------------------


class TestCrop:
    def test_the_crop_holds_the_figure_and_has_the_page_shape(self) -> None:
        left, top, right, bottom = crop_box(FIGURE, (400, 300))
        assert left <= 0.4 * 400 and right >= 0.6 * 400
        assert top <= 0.3 * 300 and bottom >= 0.8 * 300
        assert abs((right - left) / (bottom - top) - 400 / 300) < 0.02

    def test_a_figure_at_the_edge_slides_the_crop_inside_the_page(self) -> None:
        left, top, right, bottom = crop_box((0.85, 0.6, 0.15, 0.4), (400, 300))
        assert left >= 0 and top >= 0 and right <= 400 and bottom <= 300

    def test_a_huge_figure_is_cropped_to_the_whole_page(self) -> None:
        assert crop_box((0.0, 0.0, 1.0, 1.0), (400, 300)) == (0, 0, 400, 300)

    def test_cut_returns_the_crop_as_a_jpeg(self) -> None:
        crop = cut(_scene_jpeg(), FIGURE)
        left, top, right, bottom = crop_box(FIGURE, (400, 300))
        with Image.open(io.BytesIO(crop)) as opened:
            assert opened.format == "JPEG"
            assert opened.size == (right - left, bottom - top)

    @pytest.mark.parametrize(
        "box",
        [(0.1, 0.1, 0.0, 0.5), (-0.1, 0.1, 0.2, 0.2), (0.9, 0.1, 0.2, 0.2), (0.1, 0.2, 0.3)],
    )
    def test_a_box_outside_the_picture_is_not_a_figure(self, box: tuple[float, ...]) -> None:
        assert valid_figure(box) is None


# --- the blend ------------------------------------------------------------------------


class TestBlend:
    def test_the_mask_is_solid_over_the_figure_and_nothing_at_the_edge(self) -> None:
        size = (300, 225)
        inner = (110.0, 60.0, 190.0, 165.0)
        mask = _feather_mask(size, inner)
        assert mask.getpixel((150, 112)) == 255
        width, height = size
        edge = [mask.getpixel((x, 0)) for x in range(width)]
        edge += [mask.getpixel((x, height - 1)) for x in range(width)]
        edge += [mask.getpixel((0, y)) for y in range(height)]
        edge += [mask.getpixel((width - 1, y)) for y in range(height)]
        assert max(edge) == 0

    def test_outside_the_crop_is_the_scene_and_the_figure_is_the_drawing(self) -> None:
        with Image.open(io.BytesIO(_scene_jpeg())) as opened:
            scene = opened.convert("RGB")
        box = crop_box(FIGURE, scene.size)
        drawn = Image.new("RGB", (box[2] - box[0], box[3] - box[1]), (20, 200, 240))

        merged = blend_back(scene, drawn, FIGURE)

        outside = Image.new("L", scene.size, 255)
        outside.paste(0, box)
        diff = ImageChops.difference(merged, scene)
        assert ImageChops.multiply(diff.convert("L"), outside).getbbox() is None
        left, top, right, bottom = _figure_in_crop(FIGURE, scene.size, box)
        centre = (box[0] + round((left + right) / 2), box[1] + round((top + bottom) / 2))
        _r, g, b = merged.getpixel(centre)  # type: ignore[misc]
        # The drawing's blue, shifted at most 24 toward the scene's light.
        assert g >= 176 and b >= 216

    def test_prepare_page_blends_the_drawn_crop_into_the_scene(self) -> None:
        scene_bytes = _scene_jpeg()
        crop = cut(scene_bytes, FIGURE)
        prepared = prepare_page(
            crop,
            plate=None,
            figures=None,
            scene=SceneCrop(scene_bytes, FIGURE),
        )
        with Image.open(io.BytesIO(prepared.image)) as opened:
            assert opened.format == "JPEG"
            assert opened.size == (400, 300)


# --- the store ------------------------------------------------------------------------


class TestStore:
    def test_a_star_page_needs_its_figure(self, engine: Engine) -> None:
        store = _store(engine)
        _approve_every_plate(store, RED)
        with pytest.raises(TemplateError, match="figure box"):
            store.add_premade(RED.key, STAR_PAGE, image=_jpeg(), media_type="image/jpeg")

    def test_a_starless_page_has_no_figure(self, engine: Engine) -> None:
        store = _store(engine)
        _approve_every_plate(store, RED)
        with pytest.raises(TemplateError, match="no figure"):
            store.add_premade(
                RED.key, CUPBOARD, image=_jpeg(), media_type="image/jpeg", figure=FIGURE
            )

    def test_a_figure_outside_the_picture_is_refused(self, engine: Engine) -> None:
        store = _store(engine)
        _approve_every_plate(store, RED)
        with pytest.raises(TemplateError, match="four shares"):
            store.add_premade(
                RED.key,
                STAR_PAGE,
                image=_jpeg(),
                media_type="image/jpeg",
                figure=(0.9, 0.1, 0.5, 0.5),
            )

    def test_an_approved_scene_is_pinned_with_its_figure(self, engine: Engine) -> None:
        store = _store(engine)
        _approve_every_plate(store, RED)
        scene = store.add_premade(
            RED.key, STAR_PAGE, image=_jpeg(), media_type="image/jpeg", figure=FIGURE
        )
        store.approve_premade(scene, operator=OPERATOR)

        pinned = store.book_premades(_book(engine, store))

        assert pinned[STAR_PAGE].figure == FIGURE
        assert store.premade_art(pinned[STAR_PAGE]).figure == FIGURE
        (summary,) = store.premades(RED.key, STAR_PAGE)
        assert summary.figure == FIGURE


# --- the importer ---------------------------------------------------------------------


class TestImporter:
    def _run(self, engine: Engine, directory: Path) -> list[int]:
        store = _store(engine)
        _approve_every_plate(store, RED)
        result = import_run(
            story=RED,
            store=store,
            directory=directory,
            operator=OPERATOR,
            approve=True,
            replace=False,
            out=io.StringIO(),
        )
        return result.approved

    def test_a_star_page_imports_with_its_figure_file(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        (tmp_path / "1.jpg").write_bytes(_jpeg())
        (tmp_path / "1.json").write_text(json.dumps({"figure": list(FIGURE)}))
        assert self._run(engine, tmp_path) == [STAR_PAGE]

    def test_a_bad_figure_file_fails(self, engine: Engine, tmp_path: Path) -> None:
        (tmp_path / "1.jpg").write_bytes(_jpeg())
        (tmp_path / "1.json").write_text(json.dumps({"figure": [0.5, 0.5, 0.9, 0.9]}))
        with pytest.raises(ImportError_, match="inside the picture"):
            self._run(engine, tmp_path)

    def test_a_starless_page_with_a_figure_file_fails(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        (tmp_path / "9.jpg").write_bytes(_jpeg())
        (tmp_path / "9.json").write_text(json.dumps({"figure": list(FIGURE)}))
        with pytest.raises(ImportError_, match="has no figure"):
            self._run(engine, tmp_path)


# --- the prompt -----------------------------------------------------------------------


class TestPrompt:
    def test_it_swaps_the_child_skin_and_hair_and_keeps_the_rest(self) -> None:
        prompt = build_figure_edit_prompt(
            _red_page(STAR_PAGE), settings=CartoonSettings(), prompt_name="Ada"
        )
        assert "Image 2 is the portrait of Ada" in prompt
        assert "Recolour ALL of the stand-in's visible skin" in prompt
        assert "do not zoom, crop, pan or extend it" in prompt
        assert "ANOTHER TRY" not in prompt

    def test_a_retry_names_the_faults(self) -> None:
        prompt = build_figure_edit_prompt(
            _red_page(STAR_PAGE),
            settings=CartoonSettings(),
            prompt_name="Ada",
            retry_faults=("star_not_like_reference",),
        )
        assert "ANOTHER TRY" in prompt


# --- the runner -----------------------------------------------------------------------


class TestRunner:
    def test_a_fixed_costume_star_page_uses_its_scene(self) -> None:
        pinned = _scene_pinned()
        artwork = _artwork({STAR_PAGE: pinned})
        assert _scene_for(_red_page(STAR_PAGE), artwork, {}, fixed_costume=True) is pinned

    def test_a_scene_is_never_used_whole(self) -> None:
        artwork = _artwork({STAR_PAGE: _scene_pinned()})
        assert _premade_for(_red_page(STAR_PAGE), artwork, {}) is None

    def test_an_outfit_the_visitor_picks_draws_the_page_whole(self) -> None:
        artwork = _artwork({STAR_PAGE: _scene_pinned()})
        assert _scene_for(_red_page(STAR_PAGE), artwork, {}, fixed_costume=False) is None

    def test_a_requested_fix_draws_the_page_whole(self) -> None:
        artwork = _artwork({STAR_PAGE: _scene_pinned()})
        fixes = {STAR_PAGE: ["other"]}
        assert _scene_for(_red_page(STAR_PAGE), artwork, fixes, fixed_costume=True) is None

    def test_a_visitor_photo_of_someone_else_on_the_page_draws_it_whole(self) -> None:
        page = _red_page(STAR_PAGE)
        others = [name for name in page.cast if name != "child"]
        if not others:
            pytest.skip("page 1 has nobody but the star")
        artwork = _artwork({STAR_PAGE: _scene_pinned()}, visitor=frozenset(others[:1]))
        assert _scene_for(page, artwork, {}, fixed_costume=True) is None

    def test_a_starless_premade_is_not_a_scene(self) -> None:
        starless = PinnedPremade(page_number=CUPBOARD, premade_id="p" * 32)
        artwork = _artwork({CUPBOARD: starless})
        assert _scene_for(_red_page(CUPBOARD), artwork, {}, fixed_costume=True) is None

    def test_the_drawn_crop_is_blended_back_before_review(self) -> None:
        scene_bytes = _scene_jpeg()
        crop = cut(scene_bytes, FIGURE)
        reviewer = ScriptedReviewer(answers=[said(True, 92)])

        result, draw, _ = attempt_loop(
            runner_with(reviewer), draws=[crop], scene=SceneCrop(scene_bytes, FIGURE)
        )

        assert draw.calls == 1 and draw.bases == [None]
        with Image.open(io.BytesIO(result.image)) as opened:
            assert opened.size == (400, 300)
        assert result.verdicts[0]["passed"] is True
