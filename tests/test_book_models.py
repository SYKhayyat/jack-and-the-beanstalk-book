"""The book's domain models: page catalogue and About Me validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.book import BOOK_PAGES, AboutMe, TextZone, get_book_page
from app.models.settings import CartoonSettings
from app.services.prompts import render_book_page


class TestPageCatalogue:
    def test_page_numbers_run_in_story_order(self) -> None:
        assert [page.number for page in BOOK_PAGES] == [1, 2, 3, 4]
        assert [page.key for page in BOOK_PAGES] == [
            "empty-cupboard",
            "the-trade",
            "beans-out-the-window",
            "beanstalk-at-dawn",
        ]

    def test_every_page_has_a_story_line(self) -> None:
        assert all(page.story.strip() for page in BOOK_PAGES)

    def test_text_zones_are_zones_the_pdf_knows(self) -> None:
        assert BOOK_PAGES[0].text_zone is TextZone.LOWER_THIRD
        assert BOOK_PAGES[1].text_zone is TextZone.SKY
        assert BOOK_PAGES[2].text_zone is TextZone.GRASS
        assert BOOK_PAGES[3].text_zone is TextZone.SKY

    def test_lookup_by_key(self) -> None:
        assert get_book_page("the-trade") is BOOK_PAGES[1]
        assert get_book_page("no-such-page") is None

    def test_render_fills_name_and_pronouns(self) -> None:
        settings = CartoonSettings(photo_gender="girl", character_gender="match")
        rendered = render_book_page(BOOK_PAGES[0], settings)
        assert rendered.story.startswith("Jane and her mother")
        assert "{name}" not in rendered.story
        assert "{possessive}" not in rendered.scene.caption

    def test_boy_renders_jack(self) -> None:
        settings = CartoonSettings(photo_gender="boy", character_gender="match")
        rendered = render_book_page(BOOK_PAGES[2], settings)
        assert "Jack" in rendered.scene.caption or rendered.story


class TestAboutMe:
    def test_name_is_required(self) -> None:
        with pytest.raises(ValidationError):
            AboutMe(child_name="   ")

    def test_control_characters_and_newlines_in_one_line_fields(self) -> None:
        about = AboutMe(child_name="  Shai\t Ben  ", details="")
        assert about.child_name == "Shai Ben"

    def test_details_keep_line_structure(self) -> None:
        about = AboutMe(child_name="Shai", details="Loves dinosaurs\n\n  Lives in Jerusalem ")
        assert (
            about.details
            == "Loves dolphins".replace("dolphins", "dinosaurs") + "\nLives in Jerusalem"
        )

    def test_details_drop_control_characters(self) -> None:
        about = AboutMe(child_name="Shai", details="Loves \x07dinosaurs")
        assert about.details == "Loves dinosaurs"

    def test_age_bounds(self) -> None:
        assert AboutMe(child_name="A", child_age=0).child_age == 0
        assert AboutMe(child_name="A", child_age=14).child_age == 14
        with pytest.raises(ValidationError):
            AboutMe(child_name="A", child_age=15)
        with pytest.raises(ValidationError):
            AboutMe(child_name="A", child_age=-1)

    def test_rendered_lines_compose_the_page(self) -> None:
        about = AboutMe(child_name="Shai", child_age=7, details="Loves dinosaurs")
        lines = about.rendered_about_lines()
        assert lines[0] == "About Shai"
        assert lines[1] == "7 years old"
        assert lines[2] == "A few things you should know about"
        assert lines[3] == "Loves dinosaurs"

    def test_empty_optional_fields_add_no_lines(self) -> None:
        about = AboutMe(child_name="Shai")
        assert about.rendered_about_lines() == ["About Shai"]
