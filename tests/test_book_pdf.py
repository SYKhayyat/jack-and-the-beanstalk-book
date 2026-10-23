"""The PDF assembler: real text over real art, About last."""

from __future__ import annotations

import io

import pytest
from PIL import Image
from pypdf import PdfReader

from app.models.book import AboutMe, get_book_page
from app.services.pdf import PageArt, build_book_pdf

try:
    import uharfbuzz  # noqa: F401

    HAS_SHAPING = True
except ImportError:  # nixOS runs without libstdc++ by default
    HAS_SHAPING = False

PAGE_KEYS = ("empty-cupboard", "the-trade", "beans-out-the-window", "beanstalk-at-dawn")


def art_bytes(width: int = 1152, height: int = 864, colour: str = "#4a7fbf") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="PNG")
    return buffer.getvalue()


PDF_ABOUT_DEFAULTS: dict[str, object] = {
    "prompt_name": "Shai",
    "about_name": "Shai",
}


def about(**overrides: object) -> AboutMe:
    fields: dict[str, object] = {
        "prompt_name": "Shai",
        "about_name": "Shai",
        "about_age": 7,
        "details": "Loves dinosaurs",
    }
    fields.update(overrides)
    return AboutMe.model_validate(fields)


def pages(story: bool = True) -> list[PageArt]:
    return (
        [PageArt(image=art_bytes(), page=get_book_page(key)) for key in PAGE_KEYS]
        if story
        else []
    )


class TestAssembly:
    def test_one_page_per_story_page_plus_about_me(self) -> None:
        reader = PdfReader(io.BytesIO(build_book_pdf(about(), pages())))
        assert len(reader.pages) == 5

    def test_story_text_is_real_pdf_text(self) -> None:
        data = build_book_pdf(about(), pages())
        reader = PdfReader(io.BytesIO(data))
        first = reader.pages[0]
        text = first.extract_text()
        assert "Milky-White" in text.replace("\n", " ")

    def test_page_shape_matches_the_art(self) -> None:
        reader = PdfReader(io.BytesIO(build_book_pdf(about(), pages())))
        box = reader.pages[0].mediabox
        assert box.width == pytest.approx(720)
        assert box.height == pytest.approx(540)

    def test_about_me_carries_the_typed_words(self) -> None:
        data = build_book_pdf(
            about(about_name="Noa", details="Loves the sea\nTells jokes"),
            pages(),
        )
        reader = PdfReader(io.BytesIO(data))
        last = reader.pages[-1].extract_text().replace("\n", " ")
        assert "About Noa" in last
        assert "7 years old" in last
        assert "Loves the sea" in last
        assert "Tells jokes" in last

    @pytest.mark.skipif(not HAS_SHAPING, reason="RTL text shaping needs uharfbuzz")
    def test_hebrew_details_render_glyphs(self) -> None:
        data = build_book_pdf(
            about(about_name="Noa", details="אוהבת דינוזאורים"),
            pages(),
        )
        reader = PdfReader(io.BytesIO(data))
        text = reader.pages[-1].extract_text()
        assert "אוהבת" in text

    def test_empty_pages_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one page"):
            build_book_pdf(about(), [])

    def test_unreadable_art_rejected(self) -> None:
        bad = [PageArt(image=b"not an image", page=get_book_page("the-trade"))]
        with pytest.raises(ValueError, match="unreadable"):
            build_book_pdf(about(), bad)
