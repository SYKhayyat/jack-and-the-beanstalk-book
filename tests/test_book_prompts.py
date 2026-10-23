"""The book prompt builder: the identity contract and the fixed style."""

from __future__ import annotations

from app.models import CartoonSettings
from app.models.book import BOOK_PAGES
from app.prompt_data import prompt_text
from app.services.prompts import build_book_page_prompt


def test_every_page_carries_the_same_style_sentence() -> None:
    """The brief's hard rule: one style string, worded identically on every call."""
    settings = CartoonSettings()
    style = prompt_text()["book"]["style"]
    prompts = [
        build_book_page_prompt(page, settings, page_number=n, total_pages=len(BOOK_PAGES))
        for n, page in enumerate(BOOK_PAGES, start=1)
    ]
    assert len(prompts) == len(BOOK_PAGES)
    for prompt in prompts:
        assert "ART STYLE: " + style in prompt


def test_book_style_is_not_the_single_scene_style() -> None:
    """A book must not silently fall back to the scene picker's style."""
    settings = CartoonSettings()  # default scene style is western_cartoon
    page = BOOK_PAGES[0]
    prompt = build_book_page_prompt(page, settings, page_number=1, total_pages=3)
    assert "classic hand-drawn western cartoon style" not in prompt


def test_model_is_told_not_to_draw_text() -> None:
    settings = CartoonSettings()
    prompt = build_book_page_prompt(BOOK_PAGES[0], settings, page_number=1, total_pages=3)
    assert "no writing" in prompt or "Do not draw any text" in prompt


def test_both_identity_anchors_are_named_in_the_task() -> None:
    settings = CartoonSettings()
    prompt = build_book_page_prompt(BOOK_PAGES[1], settings, page_number=2, total_pages=3)
    assert "cartoon portrait" in prompt
    assert "original photograph" in prompt


def test_quiet_zone_appears_with_the_page_settling_area() -> None:
    settings = CartoonSettings()
    page = BOOK_PAGES[1]  # trade: the sky must stay quiet
    prompt = build_book_page_prompt(page, settings, page_number=2, total_pages=3)
    assert "QUIET ZONE:" in prompt
    assert "open sky" in prompt


def test_page_number_and_title_frame_the_call() -> None:
    settings = CartoonSettings()
    prompt = build_book_page_prompt(BOOK_PAGES[2], settings, page_number=3, total_pages=3)
    assert "page 3 of 3" in prompt
    assert "Jack and the Beanstalk" in prompt


def test_pronouns_follow_the_child() -> None:
    settings = CartoonSettings(photo_gender="girl", character_gender="match")
    prompt = build_book_page_prompt(BOOK_PAGES[0], settings, page_number=1, total_pages=3)
    assert "Jane" in prompt
    assert "The character's name is Jane" in prompt
