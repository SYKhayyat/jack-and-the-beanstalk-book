"""The print edition: Lulu-shaped interior and cover files (Q2, A1, A2, A11)."""

from __future__ import annotations

import io

import pytest
from pypdf import PdfReader

from app.services.pdf import (
    PageArt,
    _place_art,
    build_book_pdf,
    build_print_cover,
    build_print_interior,
    print_template,
)
from app.services.print.products import PRODUCTS, Binding, Trim, product_for
from tests.test_book_pdf import BOOK_PAGES, about, art_bytes

SQUARE_HARD = product_for("square", "hardcover")
LETTER_SOFT = product_for("letter", "paperback")


def _pages(count: int, width: int = 2400, height: int = 1800) -> list[PageArt]:
    return [PageArt(image=art_bytes(width, height), page=page) for page in BOOK_PAGES[:count]]


def _sizes(pdf: bytes) -> list[tuple[float, float]]:
    return [
        (round(float(page.mediabox.width), 2), round(float(page.mediabox.height), 2))
        for page in PdfReader(io.BytesIO(pdf)).pages
    ]


def test_six_products_with_lulu_codes_and_the_owners_prices() -> None:
    assert set(PRODUCTS) == {
        "square-hardcover",
        "square-paperback",
        "letter-hardcover",
        "letter-paperback",
        "wide-hardcover",
        "wide-paperback",
    }
    assert SQUARE_HARD.pod_package_id == "0850X0850.FC.PRE.CW.080CW444.GXX"
    assert LETTER_SOFT.pod_package_id == "0850X1100.FC.PRE.PB.080CW444.GXX"
    assert SQUARE_HARD.retail_cents == 2999 and LETTER_SOFT.retail_cents == 1799
    assert SQUARE_HARD.label == "Hardcover, 8.5 \u00d7 8.5 in square"
    wide = product_for("wide", "paperback")
    assert wide.pod_package_id == "1100X0850.FC.PRE.PB.080CW444.GXX"
    assert wide.label == "Paperback, 11 \u00d7 8.5 in landscape"
    with pytest.raises(ValueError):
        product_for("a4", "hardcover")
    with pytest.raises(ValueError):
        product_for(Trim.SQUARE, "spiral")
    assert product_for(Trim.LETTER, Binding.HARDCOVER).key == "letter-hardcover"


def test_pages_carry_an_eighth_inch_bleed_each_side() -> None:
    assert SQUARE_HARD.page_size == (630.0, 630.0)
    assert LETTER_SOFT.page_size == (630.0, 810.0)
    assert SQUARE_HARD.interior_inset == 45.0
    assert SQUARE_HARD.cover_safety == 54.0 and LETTER_SOFT.cover_safety == 18.0


def test_short_books_are_padded_to_the_minimum_and_an_even_count() -> None:
    assert SQUARE_HARD.printed_pages(7) == 24
    assert LETTER_SOFT.printed_pages(7) == 32
    assert SQUARE_HARD.printed_pages(25) == 26
    assert SQUARE_HARD.printed_pages(26) == 26
    with pytest.raises(ValueError):
        SQUARE_HARD.printed_pages(801)
    with pytest.raises(ValueError):
        SQUARE_HARD.printed_pages(0)


def test_spine_widths_follow_lulus_tables() -> None:
    assert SQUARE_HARD.spine_inches(24) == 0.25
    assert SQUARE_HARD.spine_inches(100) == 0.5
    assert LETTER_SOFT.spine_inches(444) == pytest.approx(1.06)


@pytest.mark.parametrize("product", [SQUARE_HARD, LETTER_SOFT], ids=lambda p: p.key)
def test_the_interior_is_trim_plus_bleed_padded_with_blanks(product) -> None:
    pdf = build_print_interior(about(), _pages(5), product)
    sizes = _sizes(pdf)
    # copyright + 5 story + The End + About = 8 pages of content.
    assert len(sizes) == product.min_pages
    assert set(sizes) == {product.page_size}
    reader = PdfReader(io.BytesIO(pdf))
    assert "Shai" in reader.pages[0].extract_text() or "Shai" in "".join(
        page.extract_text() for page in reader.pages[:8]
    )
    assert all(not page.extract_text().strip() for page in reader.pages[8:])


def test_print_pictures_are_embedded_at_full_resolution() -> None:
    pdf = build_print_interior(about(), _pages(1, 3000, 2250), SQUARE_HARD)
    story_page = PdfReader(io.BytesIO(pdf)).pages[1]
    widths = [image.image.size for image in story_page.images]
    assert (3000, 2250) in widths


def _ppi(pixels: int, points: float) -> float:
    return pixels / (points / 72)


@pytest.mark.parametrize("product", [SQUARE_HARD, LETTER_SOFT], ids=lambda p: p.key)
def test_generated_size_pictures_print_at_print_density(product) -> None:
    """P1: the 1152 x 864 pictures are resampled up for the print file only."""
    pdf = build_print_interior(about(), _pages(1, 1152, 864), product)
    story_page = PdfReader(io.BytesIO(pdf)).pages[1]
    width_px = max(image.image.size[0] for image in story_page.images)
    assert _ppi(width_px, float(story_page.mediabox.width)) >= 250


def test_the_screen_pdf_is_never_upscaled() -> None:
    pdf = build_book_pdf("Jack", about(), _pages(1, 1152, 864))
    pages = PdfReader(io.BytesIO(pdf)).pages
    sizes = {image.image.size for page in pages for image in page.images}
    assert (1152, 864) in sizes
    assert all(width <= 1600 for width, _ in sizes)


def test_story_text_stays_inside_the_safety_margin() -> None:
    layout = print_template(SQUARE_HARD, 0.75, ["Once upon a time " * 30])
    inset = SQUARE_HARD.interior_inset
    margin = (layout.page_width - layout.text_width) / 2
    assert margin >= inset
    assert layout.text_bottom <= layout.page_height - inset
    assert layout.text_top >= layout.art_height
    assert layout.art_height <= layout.page_width * 0.75


def test_a_mismatched_picture_is_letterboxed_on_cream_not_stretched() -> None:
    from app.services.pdf import _CREAM, _new_pdf

    pdf = _new_pdf(400, 400)
    pdf.add_page()
    drawn: dict[str, object] = {}
    filled: list[tuple[float, ...]] = []
    image, rect = pdf.image, pdf.rect

    def record_image(*args: object, **kwargs: object) -> object:
        drawn.update(kwargs)
        return image(*args, **kwargs)

    def record_rect(
        x: float, y: float, w: float, h: float, *args: object, **kw: object
    ) -> None:
        filled.append((x, y, w, h, *pdf.fill_color.colors255))
        rect(x, y, w, h, *args, **kw)

    pdf.image, pdf.rect = record_image, record_rect  # type: ignore[method-assign]
    # A 2:1 picture in a square box keeps its shape, centred, cream above/below.
    _place_art(pdf, art_bytes(800, 400, "#ff0000"), 0, 0, 400, 400)
    assert (drawn["x"], drawn["y"], drawn["w"], drawn["h"]) == (0, 100, 400, 200)
    assert filled == [(0, 0, 400, 400, *_CREAM)]


@pytest.mark.parametrize("product", [SQUARE_HARD, LETTER_SOFT], ids=lambda p: p.key)
def test_the_cover_is_one_spread_of_the_size_asked_for(product) -> None:
    size = product.estimated_cover(product.min_pages)
    pdf = build_print_cover(
        "Jack and the Beanstalk",
        about(),
        art_bytes(2400, 1800),
        size,
        product,
        portrait=art_bytes(600, 600, "#cc8844"),
    )
    sizes = _sizes(pdf)
    assert sizes == [(round(size.width, 2), round(size.height, 2))]
    trim_w, trim_h = product.trim_inches
    wrap = 0.75 if product.binding == Binding.HARDCOVER else 0.125
    assert size.height == pytest.approx((trim_h + 2 * wrap) * 72)
    assert size.width == pytest.approx(
        (2 * trim_w + product.spine_inches(product.min_pages) + 2 * wrap) * 72
    )
    assert size.wrap_y == pytest.approx(wrap * 72)
    text = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
    assert "Jack" in text


def test_the_cover_art_is_never_downscaled() -> None:
    size = SQUARE_HARD.estimated_cover(24)
    pdf = build_print_cover("Jack", about(), art_bytes(3000, 2250), size, SQUARE_HARD)
    widths = [image.image.size[0] for image in PdfReader(io.BytesIO(pdf)).pages[0].images]
    assert max(widths) >= 3000


def test_a_generated_size_cover_prints_at_print_density() -> None:
    size = SQUARE_HARD.estimated_cover(24)
    pdf = build_print_cover("Jack", about(), art_bytes(1152, 864), size, SQUARE_HARD)
    widths = [image.image.size[0] for image in PdfReader(io.BytesIO(pdf)).pages[0].images]
    front_points = size.width - (size.width - size.spine) / 2 - size.spine
    assert _ppi(max(widths), front_points) >= 250
