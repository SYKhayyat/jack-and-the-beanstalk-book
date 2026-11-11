"""The fix prompt, the star's hair line, and the paint areas a paste keeps."""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any

import pytest

from app.books import SeedError, seed_from_mapping
from app.models.settings import CartoonSettings, HairColour, HairStyle
from app.models.story import page_from_content
from app.prompt_data import prompt_text
from app.services.plates import slots_for
from app.services.prompts import build_fix_edit_prompt, build_plate_edit_prompt, star_hair
from app.services.reviewer import hair_sentence
from app.services.template_store import _seed_pages

from .helpers import edition_for

RED_PATH = Path("app/books/little_red_riding_hood.toml")
RED_DATA = tomllib.loads(RED_PATH.read_text(encoding="utf-8"))
SUPPER = "the-supper"


def _page(key: str) -> Any:
    return next(p for p in edition_for("little_red_riding_hood").pages if p.key == key)


def _fix(**kwargs: Any) -> str:
    settings = kwargs.pop("settings", CartoonSettings(photo_gender="girl"))
    return build_fix_edit_prompt(
        _page(SUPPER),
        slots_for(["red", "grandmother"], staging="at_the_table"),
        story=edition_for("little_red_riding_hood"),
        settings=settings,
        prompt_name="Mia",
        **kwargs,
    )


class TestTheFixPrompt:
    def test_a_fix_with_nothing_to_fix_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one fault"):
            _fix(faults=())

    def test_it_says_fix_this_picture_and_what_is_wrong(self) -> None:
        prompt = _fix(faults=("extra_person",), findings="a second girl at the right")
        assert prompt_text()["book"]["fix"]["task"] in prompt
        assert "WHAT IS WRONG:" in prompt
        assert "a second girl at the right" in prompt
        assert "image 1 is the page to fix" in prompt

    def test_it_does_not_ask_to_protect_the_plate(self) -> None:
        """Image 1 is a finished page: the task line already says to keep it."""
        slots = slots_for(["red", "grandmother"], staging="at_the_table")
        edit = build_plate_edit_prompt(
            _page(SUPPER),
            slots,
            story=edition_for("little_red_riding_hood"),
            settings=CartoonSettings(photo_gender="girl"),
            prompt_name="Mia",
        )
        fix = _fix(faults=("deformed",))
        assert prompt_text()["book"]["edit"]["preserve"] in edit
        assert prompt_text()["book"]["edit"]["preserve"] not in fix


class TestTheStarsHair:
    def test_picked_hair_is_said(self) -> None:
        settings = CartoonSettings(hair_style=HairStyle.SHORT, hair_color=HairColour.BROWN)
        line = star_hair(settings, "Mia", seen="Long blond hair.")
        assert line.startswith("Mia's hair:")
        assert "Under a hood" in line
        # Both picked: what the photo showed has no say.
        assert "blond" not in line

    def test_the_photos_hair_is_said_from_the_portrait(self) -> None:
        line = star_hair(CartoonSettings(), "Mia", seen="Short curly brown hair.")
        assert "Short curly brown hair." in line

    def test_one_pick_governs_and_the_portrait_supplies_the_rest(self) -> None:
        settings = CartoonSettings(hair_color=HairColour.BLACK)
        line = star_hair(settings, "Mia", seen="Shoulder-length wavy hair.")
        assert "Otherwise as in the portrait: Shoulder-length wavy hair." in line

    def test_nothing_known_says_nothing(self) -> None:
        assert star_hair(CartoonSettings(), "Mia") == ""

    def test_the_fix_prompt_carries_the_hair_seen(self) -> None:
        assert "Short curly brown hair." in _fix(
            faults=("likeness",), hair_seen="Short curly brown hair."
        )


class TestAHairSentence:
    def test_it_is_one_clean_line_ending_in_a_full_stop(self) -> None:
        assert hair_sentence('  "Short,\n curly `brown` hair"  ') == "Short, curly brown hair."

    def test_a_rambling_answer_is_capped(self) -> None:
        sentence = hair_sentence("very " * 100 + "long hair")
        assert len(sentence) <= 201
        assert sentence.endswith(".")

    def test_an_empty_answer_stays_empty(self) -> None:
        assert hair_sentence("   ") == ""


def _red_with(paint_areas: Any) -> dict[str, Any]:
    data = copy.deepcopy(RED_DATA)
    page = next(p for p in data["pages"] if p["key"] == SUPPER)
    page["paint_areas"] = paint_areas
    return data


class TestPaintAreas:
    def test_declared_areas_reach_the_finished_page(self) -> None:
        seed = seed_from_mapping(RED_DATA, str(RED_PATH))
        number, content = next((n, c) for n, c in _seed_pages(seed) if c["page_key"] == SUPPER)
        page = page_from_content(number, content)
        assert page.paint_areas == ((0.33, 0.40, 0.35, 0.28),)

    def test_a_page_without_areas_stores_none(self) -> None:
        seed = seed_from_mapping(RED_DATA, str(RED_PATH))
        others = [c for _, c in _seed_pages(seed) if c["page_key"] != SUPPER]
        assert all("paint_areas" not in c["scene"] for c in others)

    @pytest.mark.parametrize(
        "bad",
        [
            "0.1, 0.1, 0.2, 0.2",
            [[0.1, 0.1, 0.2]],
            [[0.1, 0.1, "wide", 0.2]],
            [[True, 0.1, 0.2, 0.2]],
            [[0.9, 0.1, 0.3, 0.2]],
            [[0.1, 0.1, 0.0, 0.2]],
        ],
    )
    def test_a_malformed_area_is_refused(self, bad: Any) -> None:
        with pytest.raises(SeedError, match="paint_areas"):
            seed_from_mapping(_red_with(bad), str(RED_PATH))
