"""The book's domain models: page catalogue and About validation."""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from app.models.book import AboutMe
from app.models.settings import CartoonSettings
from app.prompt_data import prompt_text
from app.services.prompts import book_name_for, render_book_page
from tests.helpers import jack

#: Jack's tale exactly as a new book is made from it (its seed's edition).
JACK = jack()
BOOK_PAGES = JACK.pages


def get_book_page(key: str):
    """One of Jack's pages by key."""
    return next((page for page in BOOK_PAGES if page.key == key), None)


def test_every_page_tells_the_story_rather_than_captioning_it() -> None:
    """The words on a page are prose that carries the tale forward.

    The complaint was that the pages read like captions — one sentence each,
    labelling the picture. A page now carries several sentences of narration,
    and the book's own model keeps a separate one-line beat for the model, so
    the paragraph does not have to be shipped to every image call.
    """
    for page in BOOK_PAGES:
        sentences = [part for part in re.split(r"(?<=[.!?])\s+", page.story) if part.strip()]
        assert len(sentences) >= 2, f"{page.key} is a caption, not a story"
        assert len(page.story.split()) >= 40, f"{page.key} is too short to narrate"
        assert page.beat.strip(), f"{page.key} has no beat for the model"


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
        ordinary story page (page 4) and reachable by key. A seed holds exactly
        the twenty pages a book prints, so there is no retirement flag left.
        """
        page = get_book_page("beanstalk-at-dawn")
        assert page is not None and page.number == 4
        assert page.story.strip()
        assert page.scene.caption.strip()
        assert [page.key for page in BOOK_PAGES].count("beanstalk-at-dawn") == 1
        assert get_book_page("beanstalk-at-dawn") is not None

    def test_every_page_has_a_story_line(self) -> None:
        assert all(page.story.strip() for page in BOOK_PAGES)

    def test_every_page_names_its_emotional_beat(self) -> None:
        """AI assessment: expression tracks the story beat, page by page.

        The three text zones are gone: the story is set in one margin under
        the art on every page, so a page no longer says where its text goes.
        """
        for page in BOOK_PAGES:
            assert page.emotion.strip(), page.key

    def test_lookup_by_key(self) -> None:
        assert get_book_page("the-trade") is BOOK_PAGES[1]
        assert get_book_page("no-such-page") is None

    def test_render_fills_name_and_pronouns(self) -> None:
        settings = CartoonSettings(photo_gender="girl")
        rendered = render_book_page(
            BOOK_PAGES[0], settings, prompt_name=book_name_for(settings, JACK)
        )
        assert "Jane lived in a little stone cottage" in rendered.story
        assert "her mother" in rendered.story
        assert "{name}" not in rendered.story
        assert "{possessive}" not in rendered.scene.caption

    def test_boy_renders_jack(self) -> None:
        settings = CartoonSettings(photo_gender="boy")
        rendered = render_book_page(BOOK_PAGES[2], settings, prompt_name="Jack")
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
        """The About page is real sentences, not bare field values.

        AI assessment: "7 years old / Eli Belly / Eli is very cute!" read as
        authoring notes left in the book, and "24 years old Author Shaul" as an
        unpunctuated credit. Each field is now set in a sentence; the details
        stay exactly as typed.
        """
        about = AboutMe(
            prompt_name="Jack",
            about_name="Shai",
            about_age=7,
            title="an astronaut",
            details="Loves dinosaurs",
        )
        lines = about.rendered_about_lines()
        assert lines == [
            "About Shai",
            "This book stars Shai.",
            "Shai is 7 years old.",
            "In real life, Shai is an astronaut.",
            "Loves dinosaurs",
        ]
        assert "A few things you should know about" not in " ".join(lines)
        assert "details_heading" not in prompt_text()["book"]["about"], (
            "the heading is gone from the data, not just unused"
        )

    def test_a_bare_title_reads_as_a_phrase(self) -> None:
        def title_line(title: str, age: int | None = None) -> list[str]:
            return AboutMe(
                prompt_name="S", about_name="Shaul", about_age=age, title=title
            ).rendered_about_lines()

        assert title_line("Author")[-1] == "In real life, Shaul is an author."
        assert title_line("A teacher")[-1] == "In real life, Shaul is a teacher."
        assert title_line("NASA engineer")[-1] == "In real life, Shaul is a NASA engineer."
        assert title_line("Author", 1)[2] == "Shaul is 1 year old."
        assert title_line("Author", 24)[2] == "Shaul is 24 years old."

    def test_a_dedication_is_the_first_line_after_the_heading(self) -> None:
        """Idea 5: a gift line, as typed, before the facts about the child."""
        about = AboutMe(
            prompt_name="Shai",
            about_name="Shai",
            dedication="  For Grandma,\n  who tells the best stories. ",
        )
        assert about.rendered_about_lines() == [
            "About Shai",
            "For Grandma, who tells the best stories.",
            "This book stars Shai.",
        ]

    def test_the_cover_credit_names_the_reader(self) -> None:
        assert AboutMe(prompt_name="Eli", about_name="Eli").cover_credit() == "A story for Eli"

    def test_empty_optional_fields_add_no_lines(self) -> None:
        about = AboutMe(prompt_name="Shai", about_name="Shai")
        assert about.rendered_about_lines() == ["About Shai", "This book stars Shai."]
