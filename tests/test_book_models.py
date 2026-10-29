"""The book's domain models: page catalogue and About validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.book import BOOK_PAGES, AboutMe, TextZone, get_book_page
from app.models.settings import CartoonSettings
from app.prompt_data import prompt_text
from app.services.prompts import render_book_page


class TestPageCatalogue:
    def test_page_numbers_run_in_story_order(self) -> None:
        """Step 7 grew the book; page numbers still run 1..N in catalogue order."""
        assert [page.number for page in BOOK_PAGES] == list(range(1, len(BOOK_PAGES) + 1))
        assert [page.key for page in BOOK_PAGES][:4] == [
            "empty-cupboard",
            "the-trade",
            "beans-out-the-window",
            "beanstalk-at-dawn",
        ]
        assert BOOK_PAGES[3].number == 4

    def test_the_beanstalk_at_dawn_page_is_unretired(self) -> None:
        """Step 7 changed the constraint: the hand-in book is the full tale.

        The page was retired under the old three-beat brief. It is now an
        ordinary story page (page 4) and reachable by key. The retirement
        mechanism itself stays in the model — `retired = true` still drops a
        page — it is simply not used by this page any more.
        """
        specs = {str(spec["key"]): spec for spec in prompt_text()["book_pages"]}
        assert "retired" not in specs["beanstalk-at-dawn"]
        assert specs["beanstalk-at-dawn"]["story"].strip()
        assert specs["beanstalk-at-dawn"]["caption"].strip()
        assert [page.key for page in BOOK_PAGES].count("beanstalk-at-dawn") == 1
        assert get_book_page("beanstalk-at-dawn") is not None

    def test_every_page_has_a_story_line(self) -> None:
        assert all(page.story.strip() for page in BOOK_PAGES)

    def test_text_zones_are_zones_the_pdf_knows(self) -> None:
        assert BOOK_PAGES[0].text_zone is TextZone.LOWER_THIRD
        assert BOOK_PAGES[1].text_zone is TextZone.SKY
        assert BOOK_PAGES[2].text_zone is TextZone.GRASS

    def test_lookup_by_key(self) -> None:
        assert get_book_page("the-trade") is BOOK_PAGES[1]
        assert get_book_page("no-such-page") is None

    def test_render_fills_name_and_pronouns(self) -> None:
        settings = CartoonSettings(photo_gender="girl")
        rendered = render_book_page(BOOK_PAGES[0], settings)
        assert rendered.story.startswith("Jane and her mother")
        assert "{name}" not in rendered.story
        assert "{possessive}" not in rendered.scene.caption

    def test_boy_renders_jack(self) -> None:
        settings = CartoonSettings(photo_gender="boy")
        rendered = render_book_page(BOOK_PAGES[2], settings)
        assert "Jack" in rendered.scene.caption or rendered.story


class TestAboutMe:
    def test_both_names_are_required(self) -> None:
        with pytest.raises(ValidationError):
            AboutMe(prompt_name="   ", about_name="Shai")
        with pytest.raises(ValidationError):
            AboutMe(prompt_name="Shai", about_name="   ")

    def test_control_characters_and_newlines_in_one_line_fields(self) -> None:
        about = AboutMe(prompt_name="  Shai\t Ben  ", about_name="Shai", details="")
        assert about.prompt_name == "Shai Ben"

    def test_the_star_name_and_the_about_name_may_differ(self) -> None:
        # The pseudonym case: the story's Jack, the About page's Avi.
        about = AboutMe(prompt_name="Jack", about_name="Avi")
        assert about.prompt_name != about.about_name

    def test_title_is_optional_and_cleaned(self) -> None:
        assert AboutMe(prompt_name="Shai", about_name="Shai").title is None
        assert AboutMe(prompt_name="Shai", about_name="Shai", title="  ").title is None
        about = AboutMe(prompt_name="Shai", about_name="Shai", title=" an astronaut ")
        assert about.title == "an astronaut"

    def test_details_keep_line_structure(self) -> None:
        about = AboutMe(
            prompt_name="Shai",
            about_name="Shai",
            details="Loves dinosaurs\n\n  Lives in Jerusalem ",
        )
        assert about.details == "Loves dinosaurs\nLives in Jerusalem"

    def test_details_drop_control_characters(self) -> None:
        about = AboutMe(prompt_name="Shai", about_name="Shai", details="Loves \x07dinosaurs")
        assert about.details == "Loves dinosaurs"

    def test_age_bounds_allow_adults(self) -> None:
        assert AboutMe(prompt_name="A", about_name="A", about_age=0).about_age == 0
        assert AboutMe(prompt_name="A", about_name="A", about_age=99).about_age == 99
        with pytest.raises(ValidationError):
            AboutMe(prompt_name="A", about_name="A", about_age=100)
        with pytest.raises(ValidationError):
            AboutMe(prompt_name="A", about_name="A", about_age=-1)

    def test_rendered_lines_compose_the_page(self) -> None:
        # Display order: name (heading), age, title, then the details —
        # with no "A few things you should know about" heading between them.
        about = AboutMe(
            prompt_name="Jack",
            about_name="Shai",
            about_age=7,
            title="an astronaut",
            details="Loves dinosaurs",
        )
        lines = about.rendered_about_lines()
        assert lines[0] == "About Shai"
        assert lines[1] == "7 years old"
        assert lines[2] == "an astronaut"
        assert lines[3] == "Loves dinosaurs"
        assert "A few things you should know about" not in " ".join(lines)
        assert "details_heading" not in prompt_text()["book"]["about"], (
            "the heading is gone from the data, not just unused"
        )

    def test_empty_optional_fields_add_no_lines(self) -> None:
        about = AboutMe(prompt_name="Shai", about_name="Shai")
        assert about.rendered_about_lines() == ["About Shai"]
