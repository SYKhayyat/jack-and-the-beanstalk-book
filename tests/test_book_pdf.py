"""The PDF assembler: real text over real art, About last."""

from __future__ import annotations

import io
import os

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


# The whole 20-page book costs about 1.9 s to assemble (fonts + one JPEG per
# page). Most assertions read it without changing it, so build it once per
# module instead of once per test.
@pytest.fixture(scope="module")
def book_bytes() -> bytes:
    return build_book_pdf(about(), pages())


@pytest.fixture(scope="module")
def book_with_cover() -> bytes:
    return build_book_pdf(about(), pages(), art_bytes())


class TestAssembly:
    def test_one_page_per_story_page_plus_about_me(self, book_bytes: bytes) -> None:
        reader = PdfReader(io.BytesIO(book_bytes))
        # About page + every active story page = 21 sheets. The cover is the
        # 22nd, added when the book has one.
        assert len(reader.pages) == len(BOOK_PAGES) + 1 == 21

    def test_the_unretired_beanstalk_page_is_in_the_pdf(self, book_bytes: bytes) -> None:
        """Step 7 changed the constraint: page 4 is story, not retired data.

        This test used to pin that the beanstalk-at-dawn prose was *absent*
        because `retired = true` dropped the page. The page is now part of the
        hand-in book, so the same prose must be present as real PDF text. The
        retirement mechanism itself is still covered by the model tests.
        """
        texts = [
            (page.extract_text() or "").replace("\n", " ")
            for page in PdfReader(io.BytesIO(book_bytes)).pages
        ]
        assert any("woke at sunrise" in text for text in texts)

    def test_story_text_is_real_pdf_text(self, book_bytes: bytes) -> None:
        reader = PdfReader(io.BytesIO(book_bytes))
        first = reader.pages[0]
        text = first.extract_text()
        assert "Milky-White" in text.replace("\n", " ")

    def test_no_page_loses_the_end_of_its_story(self, book_bytes: bytes) -> None:
        """A paragraph must fit the panel: nothing truncated, no ellipsis.

        The text is no longer a caption, so the panel has to hold several
        sentences. A page that ran out of room would end in an ellipsis — the
        reader would lose the end of the story, which is the whole point of
        rewriting it.
        """
        reader = PdfReader(io.BytesIO(book_bytes))
        for index, page in enumerate(reader.pages[:-1]):
            text = (page.extract_text() or "").replace("\n", " ")
            assert "…" not in text, f"page {index + 1} was truncated"

    def test_every_page_prints_its_own_story(self, book_bytes: bytes) -> None:
        """Each page's own paragraph is on that page, in full."""
        reader = PdfReader(io.BytesIO(book_bytes))
        texts = [(page.extract_text() or "").replace("\n", " ") for page in reader.pages]
        for page in BOOK_PAGES:
            opening = page.story.split("{name}")[0].split("{possessive}")[0].strip()
            opening = opening[:40]
            assert any(opening in text for text in texts), page.key

    def test_every_page_is_the_art_plus_one_story_margin(self, book_with_cover: bytes) -> None:
        """One template: the art's width, its height plus the same margin.

        Used to pin a 720x540 page (the art alone, text over it). The story is
        now set under the art, so the page is taller than the art, and every
        page — cover and About included — has the same size.
        """
        reader = PdfReader(io.BytesIO(book_with_cover))
        sizes = {(float(p.mediabox.width), float(p.mediabox.height)) for p in reader.pages}
        assert len(sizes) == 1
        width, height = sizes.pop()
        assert width == pytest.approx(720)
        assert height > 540

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

    def test_story_text_never_sits_on_the_art(self, book_bytes: bytes) -> None:
        """AI assessment: no white boxes over the picture, no text on a face.

        Every glyph of a story page is set in the margin below the art box
        (PDF y runs upwards, so "below" means a y under the margin's height).
        """
        reader = PdfReader(io.BytesIO(book_bytes))
        for page in reader.pages[:-1]:
            height = float(page.mediabox.height)
            margin = height - 540
            ys: list[float] = []

            def visit(text, cm, tm, font, size, ys=ys):  # type: ignore[no-untyped-def]
                if text.strip():
                    ys.append(tm[5] * cm[3] + cm[5] if cm[3] else tm[5])

            page.extract_text(visitor_text=visit)
            assert ys, "the story is on the page"
            assert max(ys) < margin, "story text was drawn over the art"

    def test_one_font_size_for_the_whole_book(self, book_bytes: bytes) -> None:
        """No font size drifting page to page."""
        sizes: set[float] = set()
        reader = PdfReader(io.BytesIO(book_bytes))
        for page in reader.pages[:-1]:

            def visit(text, cm, tm, font, size):  # type: ignore[no-untyped-def]
                if len(text.strip()) > 3:
                    sizes.add(round(size * (tm[0] or 1), 1))

            page.extract_text(visitor_text=visit)
        assert len(sizes) == 1, sizes

    def test_the_book_is_set_in_literata(self, book_bytes: bytes) -> None:
        assert b"Literata" in book_bytes
        assert b"ComicNeue" not in book_bytes

    def test_the_about_page_frames_the_portrait_and_ends_the_story(self) -> None:
        data = build_book_pdf(about(), pages(), portrait=art_bytes(512, 512))
        last = PdfReader(io.BytesIO(data)).pages[-1]
        text = (last.extract_text() or "").replace("\n", " ")
        assert "This book stars Shai." in text
        assert "Shai is 7 years old." in text
        assert "The End" in text
        assert " Do" in last.get_contents().get_data().decode("latin-1"), "portrait drawn"

    def test_empty_pages_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one page"):
            build_book_pdf(about(), [])

    def test_unreadable_art_rejected(self) -> None:
        bad = [PageArt(image=b"not an image", page=get_book_page("the-trade"))]
        with pytest.raises(ValueError, match="unreadable"):
            build_book_pdf(about(), bad)

    def test_the_art_is_shrunk_before_it_is_embedded(self) -> None:
        """A 42 MB PDF OOM-killed the 512 MB instance that built it.

        Provider PNGs are re-encoded to print-sized JPEGs, so a book's PDF is
        a small fraction of the raw art and can actually be viewed and deleted
        on a free instance.
        """
        # Incompressible noise makes a genuinely large PNG, as a provider's
        # detailed art would.
        width, height = 2048, 1536
        # Random bytes give incompressible noise without a million putpixel calls.
        noise = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
        raw = io.BytesIO()
        noise.save(raw, format="PNG")
        image = raw.getvalue()
        assert len(image) > 4_000_000, "the fixture must be big enough to matter"

        book = [
            PageArt(image=image, page=get_book_page("the-trade")),
            PageArt(image=image, page=get_book_page("beans-out-the-window")),
        ]
        pdf = build_book_pdf(about(), book)

        assert len(pdf) < len(image), "the PDF grew instead of shrinking the art"
        assert len(PdfReader(io.BytesIO(pdf)).pages) == 3
