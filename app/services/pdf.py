"""PDF assembly for the picture book.

The one place where art and text meet. Rules it enforces:

* the art is full-bleed on every page;
* the story line is drawn as **real PDF text** on a solid, opaque panel over
  the art — the image model never draws it, so no garbled letters are ever
  baked into the pictures, and the panel is a designed text section the art
  can never bleed through;
* the last page is the About page, built only from what the uploader typed.

Fonts ship with the app (``app/assets/fonts``): Comic Neue (SIL OFL) for Latin
text with Noto Sans Hebrew as a glyph fallback, so the same book renders
identically on a laptop and on Render. Text shaping — needed for correct
right-to-left text — is enabled when uharfbuzz is importable and skipped
otherwise, because Latin-only text reads fine without it.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

from fpdf import FPDF
from PIL import Image, UnidentifiedImageError

from app.models.book import AboutMe, BookPage, TextZone

__all__ = ["PageArt", "build_book_pdf", "draw_about_page"]

logger = logging.getLogger(__name__)

# Fonts bundled with the app, keyed by (family, style). Comic Neue is the book
# voice; Noto Sans Hebrew is the fallback so a Hebrew name or detail line still
# renders glyphs instead of blanks.
_FONT_FILES: tuple[tuple[str, str, str], ...] = (
    ("ComicNeue", "", "ComicNeue-Regular.ttf"),
    ("ComicNeue", "B", "ComicNeue-Bold.ttf"),
    ("NotoHebrew", "", "NotoSansHebrew-Regular.ttf"),
)

# Geometry, relative to page width so any aspect ratio prints well.
_PAGE_SHORT_EDGE = 720.0
_SIDE_MARGIN = 0.07  # story text panel container, side inset
_PANEL_TEXT_INSET = 8.0  # space, in pt, between text and its panel edges
_STORY_FONT_FRACTION = 26.0  # story font size = page short edge / this
_MIN_STORY_FONT = 14.0  # nothing beneath this stays readable

# Where each text zone's panel may live, as fractions of page height
# (top, bottom). Exhaustive by construction: the completeness check below
# fails at import when a TextZone member is missing here — the same
# fail-at-boot discipline the TOML data is on.
_TEXT_ZONE_BANDS: dict[TextZone, tuple[float, float]] = {
    TextZone.LOWER_THIRD: (0.72, 0.92),
    TextZone.SKY: (0.06, 0.28),
    TextZone.GRASS: (0.76, 0.92),
}
for _zone in TextZone:
    if _zone not in _TEXT_ZONE_BANDS:
        raise KeyError(f"No panel band defined for text zone {_zone!r}")

# The story panel: a solid designed text section, opaque over the art — the
# paper tone and the About page's ink, so text and page belong together and
# nothing behind the panel reads through the words.
_PANEL_FILL = (250, 247, 238)
_PANEL_TEXT = (74, 56, 38)
_ABOUT_BG = (247, 239, 222)
_ABOUT_TEXT = (74, 56, 38)


class PageArt:
    """One finished page: the art bytes plus the story line to overlay."""

    __slots__ = ("image", "page")

    def __init__(self, image: bytes, page: BookPage) -> None:
        self.image = image
        self.page = page


def build_book_pdf(about: AboutMe, pages: Sequence[PageArt]) -> bytes:
    """Assemble the book: full-bleed art pages in story order, About last.

    All pages share the aspect ratio of the first one (they come from the same
    provider and aspect setting, so they already match); any unexpected drift is
    letterboxed by `Image.open`'s placement rather than rejected.

    Raises:
        ValueError: if `pages` is empty or any image is unreadable.
    """
    if not pages:
        raise ValueError("A book needs at least one page.")

    pdf = FPDF(unit="pt", format=_page_shape(pages[0].image))
    pdf.set_auto_page_break(False)
    _register_fonts(pdf)
    if _shaping_supported():
        pdf.set_text_shaping(True)
        logger.info("pdf.text_shaping=on")
    else:
        logger.info("pdf.text_shaping=off reason=uharfbuzz_not_importable")

    for entry in pages:
        _draw_page(pdf, entry)

    draw_about_page(pdf, about)

    output = io.BytesIO()
    pdf.output(output)
    return output.getvalue()


def _draw_page(pdf: FPDF, entry: PageArt) -> None:
    """One book page: full-bleed art with the story line on a solid text panel."""
    width, height = pdf.w, pdf.h
    pdf.add_page()
    pdf.image(io.BytesIO(entry.image), x=0, y=0, w=width, h=height)

    story = entry.page.story
    if not story.strip():
        return

    panel_width = width * (1 - 2 * _SIDE_MARGIN)
    text_width = panel_width - 2 * _PANEL_TEXT_INSET
    band_top, band_bottom = _TEXT_ZONE_BANDS[entry.page.text_zone]
    available = (band_bottom - band_top) * height - 2 * _PANEL_TEXT_INSET

    # Fit the story inside its zone: shrink the font before ever leaving the
    # band, then draw the panel sized exactly to the fitted text, opaque so
    # the art behind it can never bleed through the words.
    lines, font_size, line_height = _fit_text(
        story,
        "ComicNeue",
        "",
        _PAGE_SHORT_EDGE / _STORY_FONT_FRACTION,
        max_width=text_width,
        max_height=available,
    )

    pdf.set_font("ComicNeue", "", font_size)
    pdf.set_text_color(*_PANEL_TEXT)
    panel_height = len(lines) * line_height + 2 * _PANEL_TEXT_INSET
    panel_y = band_top * height + (available - (panel_height - 2 * _PANEL_TEXT_INSET)) / 2
    pdf.set_fill_color(*_PANEL_FILL)
    pdf.rect(
        x=(width - panel_width) / 2,
        y=panel_y,
        w=panel_width,
        h=panel_height,
        style="F",
    )
    pdf.set_xy(width * _SIDE_MARGIN, panel_y + _PANEL_TEXT_INSET)
    pdf.multi_cell(panel_width, line_height, "\n".join(lines), align="C")


def draw_about_page(pdf: FPDF, about: AboutMe) -> None:
    """Render the About page from the uploader's words and nothing else."""
    width, height = pdf.w, pdf.h
    pdf.add_page()
    pdf.set_fill_color(*_ABOUT_BG)
    pdf.rect(0, 0, width, height, style="F")

    heading, *detail_lines = about.rendered_about_lines()
    pdf.set_text_color(*_ABOUT_TEXT)

    heading_size = width * 0.055
    while heading_size > 14.0 and _text_width(
        "ComicNeue", "B", heading_size, heading
    ) > width * (1 - 2 * _SIDE_MARGIN):
        heading_size *= 0.9
    pdf.set_font("ComicNeue", "B", heading_size)
    pdf.set_xy(
        width * _SIDE_MARGIN,
        height * 0.18,
    )
    pdf.cell(
        width - 2 * width * _SIDE_MARGIN,
        heading_size * 1.5,
        heading,
        align="C",
    )

    # The name is the page's centrepiece: shrink it to fit one line rather
    # than clip it — a long name is that child's name, not data to cut.
    panel_width = width * (1 - 2 * _SIDE_MARGIN)
    name_size = width * 0.09
    while (
        name_size > 20.0
        and _text_width("ComicNeue", "B", name_size, about.about_name) > panel_width
    ):
        name_size *= 0.9
    pdf.set_font("ComicNeue", "B", name_size)
    pdf.set_xy(width * _SIDE_MARGIN, height * 0.30)
    pdf.cell(
        panel_width,
        name_size * 1.6,
        about.about_name,
        align="C",
    )

    if detail_lines:
        details_top = height * 0.50
        available = height * 0.94 - details_top
        lines, body_size, line_height = _fit_text(
            "\n".join(detail_lines),
            "ComicNeue",
            "",
            max(16.0, width * 0.035),
            max_width=panel_width * 0.9,
            max_height=available,
        )
        pdf.set_font("ComicNeue", "", body_size)
        pdf.set_xy(
            width * _SIDE_MARGIN + panel_width * 0.05,
            details_top,
        )
        pdf.multi_cell(panel_width * 0.9, line_height, "\n".join(lines), align="C")


# --- helpers -----------------------------------------------------------------


def _register_fonts(pdf: FPDF) -> None:
    """Register the bundled faces once, with Hebrew as the glyph fallback."""
    directory = _fonts_dir()
    for family, style, filename in _FONT_FILES:
        pdf.add_font(family, style, str(directory / filename))
    pdf.set_fallback_fonts(["NotoHebrew"])


def _text_width(family: str, style: str, size: float, text: str) -> float:
    return _wrap_probe(family, style, size).get_string_width(text)


def _fit_text(
    text: str,
    family: str,
    style: str,
    size: float,
    *,
    max_width: float,
    max_height: float,
) -> tuple[list[str], float, float]:
    """Wrap `text`, shrinking the font and finally truncating until it fits.

    Shrink steps happen first (readable, still smaller), truncation only as
    the last resort: the final line gets a light ellipsis rather than
    silently vanishing off the page. Returns (lines, font size, line height).
    """
    line_factor = 1.5
    current = size
    while current >= _MIN_STORY_FONT:
        lines = _wrap(text, family, style, current, max_width)
        if len(lines) * current * line_factor <= max_height:
            return lines, current, current * line_factor
        current = current * 0.85
    # Even the floor does not fit: truncate, keeping the first lines whole.
    current = _MIN_STORY_FONT
    lines = _wrap(text, family, style, current, max_width)
    max_lines = max(1, int(max_height // (current * line_factor)))
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        if lines:
            lines[-1] = (lines[-1].rstrip() + "…").strip()
    return lines, current, current * line_factor


def _shaping_supported() -> bool:
    try:
        import uharfbuzz  # noqa: F401

        return True
    except ImportError:
        return False


def _page_shape(image: bytes) -> tuple[float, float]:
    """Page size in pt for the art's aspect ratio, long edge capped."""
    try:
        with Image.open(io.BytesIO(image)) as opened:
            width, height = opened.size
    except (OSError, UnidentifiedImageError) as exc:
        raise ValueError("A page image was unreadable; cannot assemble the book.") from exc
    if width >= height:
        return _PAGE_SHORT_EDGE, round(_PAGE_SHORT_EDGE * height / width, 2)
    return round(_PAGE_SHORT_EDGE * width / height, 2), _PAGE_SHORT_EDGE


def _wrap(text: str, family: str, style: str, size: float, max_width: float) -> list[str]:
    """Greedy word wrap measured with the real bundled font.

    fpdf2's multi_cell can wrap on its own, but the story panel needs the line
    count *before* drawing, so the panel's height is known upfront.
    """
    probe = _wrap_probe(family, style, size)
    lines: list[str] = []
    for paragraph in text.splitlines() or [text]:
        if not paragraph.strip():
            lines.append("")
            continue
        current = ""
        for word in paragraph.split():
            candidate = f"{current} {word}".strip()
            if not current or probe.get_string_width(candidate) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


@lru_cache(maxsize=16)
def _wrap_probe(family: str, style: str, size: float) -> FPDF:
    """A throwaway document used only to measure strings with bundled fonts."""
    probe = FPDF(unit="pt", format=(100, 100))
    probe.set_auto_page_break(False)
    _register_fonts(probe)
    probe.set_font(family, style, size)
    return probe


def _fonts_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "assets" / "fonts"
