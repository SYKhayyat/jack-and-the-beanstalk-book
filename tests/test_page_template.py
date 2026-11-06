"""The one page template: cream paper, art on top, words beneath, and a cover
whose title band is calm enough to print on.

These tests are deliberately independent of the story catalogue: T1 freezes the
geometry, so they drive the assembler with twenty synthetic pages whose lengths
differ. A later page-count or prose change must not be able to move the band.
"""

from __future__ import annotations

import io
import os
from dataclasses import replace
from statistics import pstdev

import pytest
from PIL import Image
from pypdf import PdfReader

from app.models.book import AboutMe, BookPage
from app.services.pdf import (
    COVER_TITLE_BAND_FRACTION,
    PAPER_RGB,
    PageArt,
    TemplateGeometry,
    TitleBandReport,
    book_template,
    build_book_pdf,
    title_band_report,
)
from tests.helpers import jack

#: Jack's tale exactly as a new book is made from it (its seed's edition).
JACK = jack()
BOOK_PAGES = JACK.pages


def get_book_page(key: str):
    """One of Jack's pages by key."""
    return next((page for page in BOOK_PAGES if page.key == key), None)


ART_WIDTH, ART_HEIGHT = 1152, 864  # 4:3, the shape the book is drawn in
PAGE_WIDTH = 720.0
ART_BOX_HEIGHT = PAGE_WIDTH * ART_HEIGHT / ART_WIDTH  # 540 pt

ABOUT = AboutMe(prompt_name="Shai", about_name="Shai")


def art(colour: str = "#4a7fbf", size: tuple[int, int] = (ART_WIDTH, ART_HEIGHT)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def story_page(story: str) -> PageArt:
    """One page from the real catalogue, with the prose swapped for this test."""
    base: BookPage = get_book_page("the-trade")
    return PageArt(image=art(), page=replace(base, story=story))


def twenty(story: str) -> list[PageArt]:
    return [story_page(story) for _ in range(20)]


def drawn_text_y(pdf_page) -> list[float]:  # type: ignore[no-untyped-def]
    """Every glyph's y position in PDF points, top-of-page origin."""
    ys: list[float] = []

    def visit(text, cm, tm, font, size, out=ys):  # type: ignore[no-untyped-def]
        if text.strip():
            out.append(tm[5] * cm[3] + cm[5] if cm[3] else tm[5])

    pdf_page.extract_text(visitor_text=visit)
    return ys


class TestStoryPageGeometry:
    def test_no_story_text_can_be_drawn_over_the_art(self) -> None:
        """Twenty pages, one of them deliberately overlong: none may reach the art."""
        pages = twenty("A short line of story.")
        pages[7] = story_page("A very long tale indeed. " * 40)
        reader = PdfReader(io.BytesIO(build_book_pdf("Jack and the Beanstalk", ABOUT, pages)))

        for index, page in enumerate(reader.pages[:-1], start=1):
            ys = drawn_text_y(page)
            assert ys, f"page {index} has no story text"
            assert max(ys) < ART_BOX_HEIGHT, f"page {index} set words over the picture"

    def test_every_story_page_shares_one_band_and_one_font_size(self) -> None:
        pages = twenty("A short line of story.")
        pages[3] = story_page("A much longer page. " * 25)
        data = build_book_pdf("Jack and the Beanstalk", ABOUT, pages)
        reader = PdfReader(io.BytesIO(data))
        geometry = book_template(
            PAGE_WIDTH, ART_BOX_HEIGHT, [entry.page.story for entry in pages]
        )

        heights = {round(float(page.mediabox.height), 2) for page in reader.pages}
        assert heights == {round(geometry.page_height, 2)}

        sizes: set[float] = set()
        for page in reader.pages[:-1]:

            def visit(text, cm, tm, font, size):  # type: ignore[no-untyped-def]
                if len(text.strip()) > 3:
                    sizes.add(round(size * (tm[0] or 1), 1))

            page.extract_text(visitor_text=visit)
        assert sizes == {round(geometry.font_size, 1)}

    def test_a_short_page_keeps_the_books_band(self) -> None:
        """The band is a book property, not a per-page one: short pages don't shrink it."""
        short = book_template(PAGE_WIDTH, ART_BOX_HEIGHT, ["One line."])
        mixed = book_template(
            PAGE_WIDTH, ART_BOX_HEIGHT, ["One line.", "A much longer page. " * 25]
        )
        assert mixed.text_band_height > short.text_band_height
        assert mixed.font_size < short.font_size

    def test_a_page_too_long_for_the_band_grows_the_band(self) -> None:
        """Never truncate: the band grows past the cap instead."""
        long_story = "A very long tale indeed. " * 40
        geometry: TemplateGeometry = book_template(PAGE_WIDTH, ART_BOX_HEIGHT, [long_story])
        assert geometry.text_band_height > ART_BOX_HEIGHT * 0.5

        # Two pages prove the rendering; the geometry above proves the band.
        data = build_book_pdf(
            "Jack and the Beanstalk", ABOUT, [story_page(long_story), story_page(long_story)]
        )
        reader = PdfReader(io.BytesIO(data))
        for page in reader.pages[:-1]:
            text = (page.extract_text() or "").replace("\n", " ")
            assert "…" not in text, "the story was cut short to fit"

    def test_the_drawn_page_is_filled_with_cream(self) -> None:
        """The paper is the template's, and it is what the page actually paints."""
        data = build_book_pdf(
            "Jack and the Beanstalk", ABOUT, twenty("A short line of story.")
        )
        page = PdfReader(io.BytesIO(data)).pages[1]
        content = page.get_contents().get_data().decode("latin-1")
        expected = " ".join(f"{channel / 255:.4f}" for channel in PAPER_RGB)
        assert f"{expected} rg" in content
        assert PAPER_RGB == (250, 246, 236)


class TestCoverTitleBand:
    def test_a_calm_sky_or_paper_band_passes(self) -> None:
        assert title_band_report(art("#cfe3f5")).ok
        assert title_band_report(art("#faf6ec")).ok

    def test_a_gentle_sky_gradient_passes(self) -> None:
        image = Image.linear_gradient("L").resize((ART_WIDTH, ART_HEIGHT)).convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        report = title_band_report(buffer.getvalue())
        assert report.ok, report.reason

    def test_a_busy_top_band_fails_with_a_reason(self) -> None:
        """T1.5: the admin screen disables Approve and shows this reason."""
        band = int(ART_HEIGHT * COVER_TITLE_BAND_FRACTION)
        image = Image.new("RGB", (ART_WIDTH, ART_HEIGHT), "#faf6ec")
        image.paste(
            Image.frombytes("RGB", (ART_WIDTH, band), os.urandom(ART_WIDTH * band * 3)),
            (0, 0),
        )
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        report: TitleBandReport = title_band_report(buffer.getvalue())
        assert not report.ok
        assert report.stddev > 22.0
        assert "too busy" in report.reason
        assert f"{COVER_TITLE_BAND_FRACTION:.0%}" in report.reason

    def test_the_check_only_looks_at_the_top_band(self) -> None:
        """A busy picture below the band is fine; the title never reaches it."""
        start = int(ART_HEIGHT * COVER_TITLE_BAND_FRACTION) + 1
        height = ART_HEIGHT - start
        image = Image.new("RGB", (ART_WIDTH, ART_HEIGHT), "#faf6ec")
        image.paste(
            Image.frombytes("RGB", (ART_WIDTH, height), os.urandom(ART_WIDTH * height * 3)),
            (0, start),
        )
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        assert title_band_report(buffer.getvalue()).ok

    def test_the_band_measurement_is_a_real_standard_deviation(self) -> None:
        image = Image.new("RGB", (100, 100), "#808080")
        image.paste(Image.new("RGB", (50, 100), "#000000"), (0, 0))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        report = title_band_report(buffer.getvalue(), fraction=1.0)
        assert report.stddev == pytest.approx(pstdev([0] * 5000 + [128] * 5000), abs=0.5)

    def test_an_unreadable_cover_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unreadable"):
            title_band_report(b"not an image")

    def test_a_nonsense_fraction_is_refused(self) -> None:
        with pytest.raises(ValueError, match="between 0 and 1"):
            title_band_report(art(), fraction=0)
        with pytest.raises(ValueError, match="between 0 and 1"):
            title_band_report(art(), fraction=1.5)
