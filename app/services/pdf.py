"""PDF assembly for the picture book.

The one place where art and text meet. One layout template, applied
mechanically to every story page, so only the picture and the words change
(AI assessment, "Layout, typography, and print production"):

* the art fills the top of the page, edge to edge;
* the story is set beneath it, in a parchment margin of the same height on
  every page, as **real PDF text** — never over the art, so no text box can
  cover a face and no opaque rectangle is pasted onto the picture;
* one font size for the whole book, chosen so the longest page fits; the
  margin grows rather than ever cutting a story short;
* the front cover is full-bleed art with a title lockup (title, soft shadow,
  "starring <name>") over a gentle darkening of the sky, not a white banner;
* the first inside page is the copyright page: the notice, the publisher and
  the year, so the book opens like a real one and the story begins on the
  page after it;
* the story pages follow, then a composed "The End" page, and the last page
  is the back cover: the child's portrait in a frame and real sentences built
  from what the uploader typed ("About Me").

Fonts ship with the app (``app/assets/fonts``): Literata (SIL OFL), a warm
book serif, for every word in the book, with Noto Sans Hebrew as a glyph
fallback, so the same book renders identically on a laptop and on Render.
Text shaping — needed for correct right-to-left text — is enabled when
uharfbuzz is importable and skipped otherwise.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from fpdf import FPDF
from PIL import Image, ImageStat, UnidentifiedImageError

from app.models.book import AboutMe, AboutPhotoChoice, BookPage
from app.prompt_data import prompt_text
from app.services.typography import book_text

__all__ = [
    "COVER_TITLE_BAND_FRACTION",
    "PAPER_RGB",
    "PageArt",
    "TemplateGeometry",
    "TitleBandReport",
    "book_template",
    "build_book_pdf",
    "draw_about_page",
    "draw_closing_page",
    "draw_copyright_page",
    "draw_cover_page",
    "title_band_report",
]

logger = logging.getLogger(__name__)

_FAMILY = "Literata"

# Fonts bundled with the app, keyed by (family, style). One family for the
# whole book; Noto Sans Hebrew only fills in glyphs Literata lacks.
_FONT_FILES: tuple[tuple[str, str, str], ...] = (
    (_FAMILY, "", "Literata-Regular.ttf"),
    (_FAMILY, "B", "Literata-Bold.ttf"),
    (_FAMILY, "I", "Literata-Italic.ttf"),
    ("NotoHebrew", "", "NotoSansHebrew-Regular.ttf"),
)

# Geometry. The art box's short edge is 720 pt (10 in); everything else is
# relative to the page so any aspect ratio prints well.
_PAGE_SHORT_EDGE = 720.0
_SIDE_MARGIN = 0.08  # text block side inset, as a fraction of page width
_STORY_FONT_MAX = 19.0  # the size a page of story is set at when it fits
_MIN_STORY_FONT = 14.0  # nothing beneath this stays readable
_LINE_FACTOR = 1.45
_MARGIN_PAD_TOP = 26.0  # pt between the art's edge and the first line
_MARGIN_PAD_BOTTOM = 40.0  # pt below the last line, room for the folio
_MARGIN_CAP = 0.50  # the margin may take this fraction of the art height before
#                     the font shrinks; past the floor it grows instead

# The page's paper. Public because the template check samples the drawn page
# against it (tests/test_page_template.py), not because callers may change it.
PAPER_RGB = (244, 234, 210)
_INK = (64, 48, 32)
_GOLD = (184, 142, 64)
_SHADOW = (24, 16, 8)
_CREAM = (255, 249, 235)

# The cover's title lockup sits over the top of the art. A plate can only be
# approved when that band is calm enough for real text: a flat or gently graded
# sky passes, a busy scene does not. Measured as the luminance standard
# deviation of the top band, in 0-255 levels.
COVER_TITLE_BAND_FRACTION = 0.22
TITLE_BAND_MAX_STDDEV = 22.0

# The art is re-encoded before it is embedded. Providers return multi-megabyte
# PNGs; embedding nineteen of them verbatim produced a ~42 MB PDF, which blew
# Render's 512 MB instance apart while assembling and was too big to view.
# A page is ~10 in on the short edge, so 1600 px is still >150 dpi.
_EMBED_LONG_EDGE = 1600
_EMBED_JPEG_QUALITY = 85


class PageArt:
    """One finished page: the art plus the story line to set beneath it.

    `image` is the bytes, or a function that reads them. A finished book's
    pages are read from the store one at a time as the PDF is drawn, so
    assembling a nineteen-page book holds one full-size picture rather than
    all of them (each a megabyte or two, and a 512 MB server).

    A page that could not be drawn is `None`: the book is still assembled,
    with that page marked clearly on paper rather than left blank or the
    whole book refused (B2).
    """

    __slots__ = ("_image", "page")

    def __init__(
        self, image: bytes | Callable[[], bytes | None] | None, page: BookPage
    ) -> None:
        self._image = image
        self.page = page

    @property
    def image(self) -> bytes | None:
        """The art's bytes, or None when this page was never drawn; a lazy
        page is read again on every access."""
        source = self._image
        if source is None or isinstance(source, bytes):
            return source
        return source()


@dataclass(frozen=True, slots=True)
class TemplateGeometry:
    """The one template every story page is set in.

    Explicit data rather than values recomputed per page: the art box, the text
    band beneath it, and the one font size the whole book is set at. Every
    derived point (where the band starts, how much room the words get) is a
    property of this object, so a test can assert the geometry without parsing
    a PDF and the renderer cannot drift from it.
    """

    art_width: float
    art_height: float
    text_band_height: float
    font_size: float

    @property
    def page_width(self) -> float:
        return self.art_width

    @property
    def line_height(self) -> float:
        return self.font_size * _LINE_FACTOR

    @property
    def page_height(self) -> float:
        return self.art_height + self.text_band_height

    @property
    def text_width(self) -> float:
        return self.art_width * (1 - 2 * _SIDE_MARGIN)

    @property
    def text_band_top(self) -> float:
        """Where the text band begins: the art's bottom edge."""
        return self.art_height

    @property
    def text_top(self) -> float:
        """Where the first line may sit, after the band's top padding."""
        return self.art_height + _MARGIN_PAD_TOP

    @property
    def text_room(self) -> float:
        """The vertical space the words may occupy."""
        return self.text_band_height - _MARGIN_PAD_TOP - _MARGIN_PAD_BOTTOM


@dataclass(frozen=True, slots=True)
class TitleBandReport:
    """Whether a cover plate leaves its top band calm enough for the title."""

    ok: bool
    stddev: float
    fraction: float
    reason: str = ""


def title_band_report(
    image: bytes, *, fraction: float = COVER_TITLE_BAND_FRACTION
) -> TitleBandReport:
    """Measure the cover's title band: calm enough for real text, or not.

    The cover is the one page whose title sits over the art, so it is the one
    place "no text over a face" cannot be guaranteed by the layout alone. A
    plate whose top band is busy cannot be approved: the admin screen disables
    Approve and shows `reason`.

    Raises:
        ValueError: if `image` is unreadable or `fraction` is out of range.
    """
    if not 0 < fraction <= 1:
        raise ValueError("fraction must be between 0 and 1")
    try:
        with Image.open(io.BytesIO(image)) as opened:
            grey = opened.convert("L")
            width, height = grey.size
            band = grey.crop((0, 0, width, max(1, int(height * fraction))))
            stddev = float(ImageStat.Stat(band).stddev[0])
    except (OSError, UnidentifiedImageError) as exc:
        raise ValueError("The cover image was unreadable.") from exc

    ok = stddev <= TITLE_BAND_MAX_STDDEV
    reason = (
        ""
        if ok
        else (
            f"The top {fraction:.0%} of this plate is too busy for the title "
            f"(luminance spread {stddev:.1f}). Re-roll for a calm sky or paper band."
        )
    )
    return TitleBandReport(ok=ok, stddev=stddev, fraction=fraction, reason=reason)


def build_book_pdf(
    title: str,
    about: AboutMe,
    pages: Sequence[PageArt],
    cover: bytes | None = None,
    *,
    subtitle: str = "",
    portrait: bytes | None = None,
    photo: bytes | None = None,
    publisher: str = "Storybook Studio",
    year: int | None = None,
) -> bytes:
    """Assemble the book: cover, copyright page, story, The End, back cover.

    Every page has the same size: the art's shape plus one story margin, so
    the book prints and reads as one object. `portrait` (the cartoon) and
    `photo` (the original) are framed on the back cover as
    `about.about_photo` asks.

    The order is the one a printed picture book has: the front cover, the
    copyright page (the first inside page), the story starting on the page
    after it, a closing page so the sheet count is even, and the About Me back
    cover. Peecho prints page one as the front cover and the last page as the
    back cover, so this file can be sent to it unchanged.

    Raises:
        ValueError: if `pages` is empty, no page has a picture to take the
            book's shape from, or an image is unreadable.
    """
    if not pages:
        raise ValueError("A book needs at least one page.")

    # The shape comes from the first page that was actually drawn: a failed
    # page is marked in the book, not allowed to set its proportions (B2). The
    # cover is the last resort when no story page was drawn at all.
    shape_image = (
        next((entry.image for entry in pages if entry.image is not None), None) or cover
    )
    if shape_image is None:
        raise ValueError("A book needs at least one picture.")
    art_width, art_height = _page_shape(shape_image)
    layout = book_template(
        art_width, art_height, [book_text(entry.page.story) for entry in pages]
    )

    pdf = FPDF(unit="pt", format=(layout.page_width, layout.page_height))
    pdf.set_auto_page_break(False)
    _register_fonts(pdf)
    if _shaping_supported():
        pdf.set_text_shaping(True)
        logger.info("pdf.text_shaping=on")
    else:
        logger.info("pdf.text_shaping=off reason=uharfbuzz_not_importable")

    if cover is not None:
        draw_cover_page(pdf, title, about, cover, subtitle=subtitle)

    draw_copyright_page(pdf, about, publisher, year or datetime.now(UTC).year)

    for number, entry in enumerate(pages, start=1):
        _draw_page(pdf, entry, layout, number)

    draw_closing_page(pdf)
    draw_about_page(pdf, about, portrait, photo)

    output = io.BytesIO()
    pdf.output(output)
    return output.getvalue()


def book_template(
    art_width: float, art_height: float, stories: Sequence[str]
) -> TemplateGeometry:
    """One font size and one text band for the whole book.

    The largest size at which every page's story fits inside the capped band
    wins. If even the floor does not fit, the band grows to hold the longest
    page: a story is never truncated, and a short page keeps the same band as
    every other page of the book.
    """
    text_width = art_width * (1 - 2 * _SIDE_MARGIN)
    cap = art_height * _MARGIN_CAP
    size = _STORY_FONT_MAX
    while True:
        lines = max(
            (len(_wrap(story, _FAMILY, "", size, text_width)) for story in stories if story),
            default=0,
        )
        needed = _MARGIN_PAD_TOP + lines * size * _LINE_FACTOR + _MARGIN_PAD_BOTTOM
        if needed <= cap or size <= _MIN_STORY_FONT:
            return TemplateGeometry(art_width, art_height, max(needed, cap * 0.6), size)
        size = max(_MIN_STORY_FONT, size - 0.5)


def draw_cover_page(
    pdf: FPDF, title: str, about: AboutMe, cover: bytes, *, subtitle: str = ""
) -> None:
    """The front cover: full-bleed art with a designed title lockup.

    The art covers the whole page (cropped at the sides when the page is
    taller than the art). The title is real PDF text, always spelled right:
    cream lettering with a soft shadow over a gentle darkening of the sky the
    cover prompt keeps calm, and one italic line beneath it: the story's
    subtitle ("starring Ava") when it has one, otherwise the credit ("A story
    for Ava"). A tale whose title does not carry the star's name names them in
    the subtitle, so the line under the title always does.
    """
    width, height = pdf.w, pdf.h
    pdf.add_page()
    art_w, art_h = _image_size(cover)
    scale = max(width / art_w, height / art_h)
    drawn_w, drawn_h = art_w * scale, art_h * scale
    pdf.image(
        _image_for_pdf(cover),
        x=(width - drawn_w) / 2,
        y=(height - drawn_h) / 2,
        w=drawn_w,
        h=drawn_h,
    )

    # A soft vignette from the top edge down, so the title reads on any sky.
    # One smooth gradient image: stacked translucent strips overlap at their
    # seams and print as visible lines.
    pdf.image(io.BytesIO(_vignette_bytes()), x=0, y=0, w=width, h=height * 0.40)

    usable = width * (1 - 2 * _SIDE_MARGIN)
    title_size = width * 0.085
    while title_size > 22.0 and _text_width(_FAMILY, "B", title_size, title) > usable:
        title_size *= 0.94
    title_lines = _wrap(title, _FAMILY, "B", title_size, usable)
    top = height * 0.07
    pdf.set_font(_FAMILY, "B", title_size)
    for line in title_lines:
        _shadowed_cell(pdf, line, top, title_size * 1.25, offset=title_size * 0.05)
        top += title_size * 1.25

    # The line under the title always names the star: the story's own line
    # when it has one ("starring Sarah"), otherwise the book's credit line.
    credit = subtitle or about.cover_credit()
    credit_size = max(16.0, title_size * 0.42)
    pdf.set_font(_FAMILY, "I", credit_size)
    _shadowed_cell(pdf, credit, top + credit_size * 0.4, credit_size * 1.4, offset=1.2)


def draw_copyright_page(
    pdf: FPDF,
    about: AboutMe,
    publisher: str,
    year: int,
) -> None:
    """The first inside page: the copyright notice and who published it.

    A real book carries this opposite the title page. It is deliberately
    quiet — small centred text on the paper, with one gold rule — so it reads
    as front matter and never competes with the story. The story starts on
    the *next* page, which is why this page is drawn before any story page.
    """
    width, height = pdf.w, pdf.h
    pdf.add_page()
    pdf.set_fill_color(*PAPER_RGB)
    pdf.rect(0, 0, width, height, style="F")
    usable = width * (1 - 2 * _SIDE_MARGIN)

    lines = [
        (f"Copyright © {year} {publisher}", 13.0, ""),
        ("All rights reserved.", 11.0, "I"),
        ("", 11.0, ""),
        (f"Made for {about.about_name}.", 13.0, "I"),
        ("", 11.0, ""),
        (
            "No part of this book may be reproduced or distributed "
            "without written permission.",
            10.0,
            "",
        ),
        ("", 10.0, ""),
        (f"Published by {publisher}.", 11.0, ""),
        (f"First edition, {year}.", 10.0, "I"),
    ]
    top = height * 0.44
    pdf.set_text_color(*_INK)
    for text, size, style in lines:
        if not text:
            top += size * 0.7
            continue
        pdf.set_font(_FAMILY, style, size)
        for wrapped in _wrap(text, _FAMILY, style, size, usable * 0.86):
            pdf.set_xy(width * _SIDE_MARGIN, top)
            pdf.cell(usable, size * 1.45, wrapped, align="C")
            top += size * 1.45
    pdf.set_draw_color(*_GOLD)
    pdf.set_line_width(1.2)
    pdf.line(width * 0.36, top + height * 0.035, width * 0.64, top + height * 0.035)


def draw_closing_page(pdf: FPDF) -> None:
    """The page after the last story page: "The End", on its own.

    Printed books are made of folded sheets, so a book needs an even number of
    pages; this also gives the tale a quiet close before the back cover. The
    back cover keeps the About Me copy and carries no second "The End".
    """
    width, height = pdf.w, pdf.h
    pdf.add_page()
    pdf.set_fill_color(*PAPER_RGB)
    pdf.rect(0, 0, width, height, style="F")
    size = max(20.0, width * 0.045)
    pdf.set_font(_FAMILY, "I", size)
    pdf.set_text_color(*_GOLD)
    pdf.set_xy(0, height * 0.46)
    pdf.cell(width, size * 1.5, str(prompt_text()["book"]["about"]["closing"]), align="C")


@lru_cache(maxsize=1)
def _vignette_bytes() -> bytes:
    """A dark-to-clear vertical gradient, one pixel per row, for the cover."""
    rows = 256
    strip = Image.new("RGBA", (4, rows))
    for row in range(rows):
        alpha = round(255 * 0.50 * (1 - row / rows) ** 1.6)
        strip.paste((*_SHADOW, alpha), (0, row, 4, row + 1))
    out = io.BytesIO()
    strip.save(out, format="PNG")
    return out.getvalue()


def _shadowed_cell(
    pdf: FPDF, text: str, y: float, line_height: float, *, offset: float
) -> None:
    """One centred line of cream text with a dark edge, drawn once.

    The edge used to be a second, offset copy of the same string. That reads
    correctly on paper, but the PDF's text layer then held every cover word
    twice: copying the title gave "Little Red Riding HoodLittle Red Riding
    Hood", and a screen reader said it twice. A fill-and-stroke glyph spells
    the line once and still keeps it legible over a sky.
    """
    del offset  # kept in the signature so the callers state where the edge sits
    pdf.set_draw_color(*_SHADOW)
    pdf.set_line_width(1.2)
    pdf.text_mode = "FILL_STROKE"
    pdf.set_text_color(*_CREAM)
    pdf.set_xy(0, y)
    pdf.cell(pdf.w, line_height, text, align="C")
    pdf.text_mode = "FILL"


def _draw_page(pdf: FPDF, entry: PageArt, layout: TemplateGeometry, number: int) -> None:
    """One story page: art on top, the story in the band beneath it.

    A page whose picture could not be drawn keeps its story and gets a marked
    frame saying so, rather than taking the rest of the book down with it.
    """
    pdf.add_page()
    pdf.set_fill_color(*PAPER_RGB)
    pdf.rect(0, 0, pdf.w, pdf.h, style="F")
    image = entry.image
    if image is None:
        _draw_missing_art(pdf, layout)
    else:
        pdf.image(_image_for_pdf(image), x=0, y=0, w=layout.art_width, h=layout.art_height)

    # A thin gold rule where the picture meets the page.
    pdf.set_draw_color(*_GOLD)
    pdf.set_line_width(1.6)
    pdf.line(0, layout.text_band_top, pdf.w, layout.text_band_top)

    story = book_text(entry.page.story)
    if story.strip():
        lines = _wrap(story, _FAMILY, "", layout.font_size, layout.text_width)
        block = len(lines) * layout.line_height
        y = layout.text_top + max(0.0, (layout.text_room - block) / 2)
        pdf.set_font(_FAMILY, "", layout.font_size)
        pdf.set_text_color(*_INK)
        _centred_lines(pdf, lines, y, layout.line_height)

    # The folio: the cover is page 1 to the reader, so story page n is n + 1.
    pdf.set_font(_FAMILY, "I", 10.0)
    pdf.set_text_color(*_GOLD)
    pdf.set_xy(0, pdf.h - _MARGIN_PAD_BOTTOM * 0.75)
    pdf.cell(pdf.w, 14.0, str(number + 1), align="C")


def _draw_missing_art(pdf: FPDF, layout: TemplateGeometry) -> None:
    """The marked but undrawn page: a gold frame and a sentence saying why.

    It has to look deliberate on paper — it is printed and bound with the
    rest — and it names the one action left: finish this book.
    """
    width, height = layout.art_width, layout.art_height
    inset = min(width, height) * 0.06
    pdf.set_draw_color(*_GOLD)
    pdf.set_line_width(1.2)
    pdf.rect(inset, inset, width - 2 * inset, height - 2 * inset)
    size = max(12.0, width * 0.025)
    pdf.set_font(_FAMILY, "I", size)
    pdf.set_text_color(*_INK)
    pdf.set_xy(inset * 2, height / 2 - size)
    pdf.cell(width - 4 * inset, size * 1.4, "This picture could not be drawn.", align="C")
    pdf.set_xy(inset * 2, height / 2 + size * 0.6)
    pdf.cell(width - 4 * inset, size * 1.4, "Finish the book to fill it in.", align="C")


def draw_about_page(
    pdf: FPDF, about: AboutMe, portrait: bytes | None = None, photo: bytes | None = None
) -> None:
    """The back cover: heading, framed picture(s) and the About Me sentences.

    The picture is the cartoon, the real photo, or both side by side, as the
    About screen chose. A choice whose image is missing falls back to what is
    there, so the page is never left without the face it can show. "The End"
    is its own page just before this one, so it is not repeated here.
    """
    width, height = pdf.w, pdf.h
    pdf.add_page()
    pdf.set_fill_color(*PAPER_RGB)
    pdf.rect(0, 0, width, height, style="F")
    usable = width * (1 - 2 * _SIDE_MARGIN)

    heading, *sentences = (book_text(line) for line in about.rendered_about_lines())
    pdf.set_text_color(*_INK)
    heading_size = width * 0.05
    while heading_size > 16.0 and _text_width(_FAMILY, "B", heading_size, heading) > usable:
        heading_size *= 0.92
    pdf.set_font(_FAMILY, "B", heading_size)
    top = height * 0.06
    pdf.set_xy(width * _SIDE_MARGIN, top)
    pdf.cell(usable, heading_size * 1.4, heading, align="C")
    top += heading_size * 1.4 + height * 0.03

    images = _about_images(about.about_photo, portrait, photo)
    if len(images) == 1:
        top = _framed_image(pdf, images[0], top, max_side=min(height * 0.40, width * 0.45))
        top += height * 0.035
    elif len(images) == 2:
        side = min(height * 0.30, width * 0.30)
        bottoms = [
            _framed_image(pdf, image, top, max_side=side, centre_x=width * share)
            for image, share in zip(images, (0.30, 0.70), strict=True)
        ]
        top = max(bottoms) + height * 0.035

    # The text may continue onto another page; only the picture and heading
    # are first-page furniture (B11). The bottom margin is left clear so the
    # back cover does not run into the trim.
    bottom = height - height * 0.06
    if sentences:
        available = bottom - top
        continuation_top = height * 0.08
        continuation_height = bottom - continuation_top
        pages, body_size, line_height = _fit_text_pages(
            "\n".join(sentences),
            _FAMILY,
            "",
            max(15.0, width * 0.028),
            max_width=usable * 0.92,
            first_height=available,
            next_height=continuation_height,
        )
        pdf.set_font(_FAMILY, "", body_size)
        pdf.set_text_color(*_INK)
        page_top = top
        for index, lines in enumerate(pages):
            if index:
                pdf.add_page()
                pdf.set_fill_color(*PAPER_RGB)
                pdf.rect(0, 0, width, height, style="F")
                page_top = continuation_top
            _centred_lines(pdf, lines, page_top, line_height)


def _centred_lines(pdf: FPDF, lines: Sequence[str], top: float, line_height: float) -> None:
    """Set pre-wrapped lines one cell each, so the wrap measured is the wrap drawn.

    `multi_cell` would wrap a second time with its own cell padding, which
    pushed single words onto lines of their own.
    """
    for index, line in enumerate(lines):
        pdf.set_xy(0, top + index * line_height)
        pdf.cell(pdf.w, line_height, line, align="C")


def _about_images(
    choice: AboutPhotoChoice, portrait: bytes | None, photo: bytes | None
) -> list[bytes]:
    """The About page's pictures, in left-to-right order: photo, then cartoon."""
    if choice == AboutPhotoChoice.BOTH:
        wanted = [photo, portrait]
    elif choice == AboutPhotoChoice.PHOTO:
        wanted = [photo or portrait]
    else:
        wanted = [portrait or photo]
    return [image for image in wanted if image is not None]


def _framed_image(
    pdf: FPDF, image: bytes, top: float, *, max_side: float, centre_x: float | None = None
) -> float:
    """Draw `image` in a gold frame with a cream mat; return its bottom.

    Centred on the page unless `centre_x` names another horizontal centre.
    """
    try:
        art_w, art_h = _image_size(image)
    except ValueError:
        logger.warning("pdf.about_portrait_unreadable")
        return top
    scale = min(max_side / art_w, max_side / art_h)
    w, h = art_w * scale, art_h * scale
    frame, mat = 7.0, 6.0
    x = (pdf.w / 2 if centre_x is None else centre_x) - w / 2
    pdf.set_fill_color(*_GOLD)
    pdf.rect(x - frame - mat, top, w + 2 * (frame + mat), h + 2 * (frame + mat), style="F")
    pdf.set_fill_color(*_CREAM)
    pdf.rect(x - mat, top + frame, w + 2 * mat, h + 2 * mat, style="F")
    pdf.image(_image_for_pdf(image), x=x, y=top + frame + mat, w=w, h=h)
    return top + h + 2 * (frame + mat)


# --- helpers -----------------------------------------------------------------


def _register_fonts(pdf: FPDF) -> None:
    """Register the bundled faces once, with Hebrew as the glyph fallback."""
    directory = _fonts_dir()
    for family, style, filename in _FONT_FILES:
        pdf.add_font(family, style, str(directory / filename))
    pdf.set_fallback_fonts(["NotoHebrew"])


def _text_width(family: str, style: str, size: float, text: str) -> float:
    return _wrap_probe(family, style, size).get_string_width(text)


def _fit_text_pages(
    text: str,
    family: str,
    style: str,
    size: float,
    *,
    max_width: float,
    first_height: float,
    next_height: float,
) -> tuple[list[list[str]], float, float]:
    """Wrap `text`, shrinking it to fit, then flow the rest onto more pages.

    Used for the About page, whose details are free text of any length. A
    single page keeps the old behaviour: the largest size that fits the room
    under the picture is used. When even the smallest readable size cannot fit,
    the text continues on another page rather than being cut with an ellipsis,
    so the download carries every word the web reader showed (B11). Story
    pages never come through here: their margin grows instead.
    """
    current = size
    while current >= _MIN_STORY_FONT:
        lines = _wrap(text, family, style, current, max_width)
        line_height = current * _LINE_FACTOR
        if len(lines) * line_height <= first_height:
            return [lines], current, line_height
        current = current * 0.9
    current = _MIN_STORY_FONT
    line_height = current * _LINE_FACTOR
    lines = _wrap(text, family, style, current, max_width)
    first_capacity = max(1, int(first_height // line_height))
    next_capacity = max(1, int(next_height // line_height))
    pages = [lines[:first_capacity]]
    rest = lines[first_capacity:]
    for start in range(0, len(rest), next_capacity):
        pages.append(rest[start : start + next_capacity])
    return pages, current, line_height


def _shaping_supported() -> bool:
    try:
        import uharfbuzz  # noqa: F401

        return True
    except ImportError:
        return False


def _image_for_pdf(image: bytes) -> io.BytesIO:
    """Re-encode one picture as a right-sized JPEG for embedding.

    RGBA is flattened onto white, because JPEG has no alpha and a transparent
    page would otherwise come out black.

    Raises:
        ValueError: if the bytes are not a readable image.
    """
    try:
        with Image.open(io.BytesIO(image)) as opened:
            has_alpha = opened.mode in ("RGBA", "LA") or "transparency" in opened.info
            frame = opened.convert("RGBA" if has_alpha else "RGB")
            if frame.mode == "RGBA":
                background = Image.new("RGB", frame.size, (255, 255, 255))
                background.paste(frame, mask=frame.split()[-1])
                frame = background
            frame.thumbnail((_EMBED_LONG_EDGE, _EMBED_LONG_EDGE), Image.Resampling.LANCZOS)
            encoded = io.BytesIO()
            # A plain baseline JPEG on purpose: with optimize= or progressive=
            # Pillow encodes the whole frame in one call that holds the GIL
            # (~270 ms a page), freezing the server for the whole assembly; a
            # baseline encode works in chunks. The size cost is about 5%.
            frame.save(encoded, format="JPEG", quality=_EMBED_JPEG_QUALITY)
    except (OSError, UnidentifiedImageError) as exc:
        raise ValueError("A page image was unreadable; cannot assemble the book.") from exc
    encoded.seek(0)
    return encoded


def _image_size(image: bytes) -> tuple[int, int]:
    try:
        with Image.open(io.BytesIO(image)) as opened:
            width, height = opened.size
            return int(width), int(height)
    except (OSError, UnidentifiedImageError) as exc:
        raise ValueError("A page image was unreadable; cannot assemble the book.") from exc


def _page_shape(image: bytes) -> tuple[float, float]:
    """The art box in pt for the art's aspect ratio, short edge 720."""
    width, height = _image_size(image)
    if width >= height:
        return _PAGE_SHORT_EDGE, round(_PAGE_SHORT_EDGE * height / width, 2)
    return round(_PAGE_SHORT_EDGE * width / height, 2), _PAGE_SHORT_EDGE


def _wrap(text: str, family: str, style: str, size: float, max_width: float) -> list[str]:
    """Greedy word wrap measured with the real bundled font."""
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


@lru_cache(maxsize=64)
def _wrap_probe(family: str, style: str, size: float) -> FPDF:
    """A throwaway document used only to measure strings with bundled fonts."""
    probe = FPDF(unit="pt", format=(100, 100))
    probe.set_auto_page_break(False)
    _register_fonts(probe)
    probe.set_font(family, style, size)
    return probe


def _fonts_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "assets" / "fonts"
