"""The PDF assembler: real text over real art, About last."""

from __future__ import annotations

import io
import re

import pytest
from PIL import Image
from pypdf import PdfReader

from app.models.book import BOOK_PAGES, AboutMe, get_book_page
from app.services.pdf import PageArt, build_book_pdf

try:
    import uharfbuzz  # noqa: F401

    HAS_SHAPING = True
except ImportError:  # nixOS runs without libstdc++ by default
    HAS_SHAPING = False

# Step 7: the hand-in book is the whole tale. Drive the assembler from the
# real catalogue so this file always measures the book we ship.
PAGE_KEYS = tuple(page.key for page in BOOK_PAGES)


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
        # About page + every active story page = the hand-in 20-page book.
        assert len(reader.pages) == len(BOOK_PAGES) + 1 == 20

    def test_the_unretired_beanstalk_page_is_in_the_pdf(self) -> None:
        """Step 7 changed the constraint: page 4 is story, not retired data.

        This test used to pin that the beanstalk-at-dawn prose was *absent*
        because `retired = true` dropped the page. The page is now part of the
        hand-in book, so the same prose must be present as real PDF text. The
        retirement mechanism itself is still covered by the model tests.
        """
        data = build_book_pdf(about(), pages())
        texts = [
            (page.extract_text() or "").replace("\n", " ")
            for page in PdfReader(io.BytesIO(data)).pages
        ]
        assert any("sunrise the beanstalk had grown" in text for text in texts)

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

    def test_story_panel_is_opaque_over_the_art(self) -> None:
        """The text sits on a solid panel, never washed over busy art (UAT 7).

        The page stream must show a plain opaque fill (`re f`) for the panel —
        no graphics-state opacity (`gs`) — so the art behind the words cannot
        bleed through them.
        """
        data = build_book_pdf(about(), pages())
        for page in PdfReader(io.BytesIO(data)).pages[:-1]:  # art pages only
            stream = page.get_contents().get_data().decode("latin-1")
            assert " gs\n" not in stream, "no translucent panel may be drawn"
            assert re.search(r"0\.9804 0\.9686 0\.9333 rg", stream), (
                "the paper-tone panel fill must be present"
            )
            assert re.search(r"re\s+f", stream), "the panel is a filled rect"

    def test_empty_pages_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one page"):
            build_book_pdf(about(), [])

    def test_unreadable_art_rejected(self) -> None:
        bad = [PageArt(image=b"not an image", page=get_book_page("the-trade"))]
        with pytest.raises(ValueError, match="unreadable"):
            build_book_pdf(about(), bad)
