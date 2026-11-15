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
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from fpdf import FPDF
from PIL import Image, ImageFilter, ImageStat, UnidentifiedImageError

from app.models.book import AboutMe, AboutPhotoChoice, BookPage
from app.prompt_data import prompt_text
from app.services.book_outline import MATTER_LEAVES, Leaf, book_leaves, copyright_lines
from app.services.print.products import CoverSize, Product
from app.services.typography import book_text

__all__ = [
    "BOOK_MATTER_PAGES",
    "COVER_TITLE_BAND_FRACTION",
    "PAPER_RGB",
    "PageArt",
    "PrintGeometry",
    "TemplateGeometry",
    "TitleBandReport",
    "book_template",
    "build_book_pdf",
    "build_print_cover",
    "build_print_interior",
    "draw_about_page",
    "draw_closing_page",
    "draw_copyright_page",
    "draw_cover_page",
    "print_template",
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

#: Pages in a finished book that are not its numbered story pages: the front
#: cover, the copyright page, "The End" and the About Me back cover. The print
#: code adds this to the edition's page count to get the PDF's page count.
BOOK_MATTER_PAGES = MATTER_LEAVES

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
# The print files never downscale a picture, and encode it gently, since Lulu
# prints them. The pictures are drawn at 1152 x 864, which is about 81 ppi
# across a printed page, so a picture under `_PRINT_PPI` at its printed size
# is resampled up to it (audit 11-15 P1, the owner's call: upscale at order
# time rather than pay to draw bigger pictures). The screen PDF never is.
_PRINT_JPEG_QUALITY = 92
_PRINT_PPI = 300
_POINTS_PER_INCH = 72.0


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
    cover. This is the reading and download file; the printer gets its own
    pair, built from the same pages by `build_print_interior` and
    `build_print_cover`.

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

    # The reader and the shared link walk the same list (R1).
    stories = enumerate(pages, start=1)
    for leaf in book_leaves(len(pages)):
        if leaf is Leaf.COVER:
            if cover is not None:
                draw_cover_page(pdf, title, about, cover, subtitle=subtitle)
        elif leaf is Leaf.COPYRIGHT:
            draw_copyright_page(pdf, about, publisher, year or datetime.now(UTC).year)
        elif leaf is Leaf.STORY:
            number, entry = next(stories)
            _draw_page(pdf, entry, number, layout)
        elif leaf is Leaf.END:
            draw_closing_page(pdf)
        else:
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
    pdf.add_page()
    whole = (0.0, 0.0, pdf.w, pdf.h)
    _draw_front_cover(pdf, title, about, cover, subtitle=subtitle, box=whole, face=whole)


def _draw_front_cover(
    pdf: FPDF,
    title: str,
    about: AboutMe,
    cover: bytes,
    *,
    subtitle: str,
    box: tuple[float, float, float, float],
    face: tuple[float, float, float, float],
    safety: float = 0.0,
    for_print: bool = False,
) -> None:
    """The cover art over `box`, with the title lockup placed on `face`.

    `box` is everything the art covers (on a print cover, the front panel with
    its bleed or board wrap); `face` is the part a reader sees once the book is
    trimmed and bound, and the words keep `safety` inside it.
    """
    box_x, box_y, box_w, box_h = box
    face_x, face_y, face_w, face_h = face
    art_w, art_h = _image_size(cover)
    scale = max(box_w / art_w, box_h / art_h)
    drawn_w, drawn_h = art_w * scale, art_h * scale
    with pdf.rect_clip(box_x, box_y, box_w, box_h):
        pdf.image(
            _image_for_pdf(cover, printed_at=(drawn_w, drawn_h) if for_print else None),
            x=box_x + (box_w - drawn_w) / 2,
            y=box_y + (box_h - drawn_h) / 2,
            w=drawn_w,
            h=drawn_h,
        )
        # A soft vignette from the top edge down, so the title reads on any
        # sky. One smooth gradient image: stacked translucent strips overlap
        # at their seams and print as visible lines.
        pdf.image(
            io.BytesIO(_vignette_bytes()),
            x=box_x,
            y=box_y,
            w=box_w,
            h=(face_y - box_y) + face_h * 0.40,
        )

    usable = min(face_w * (1 - 2 * _SIDE_MARGIN), face_w - 2 * safety)
    title_size = face_w * 0.085
    while title_size > 22.0 and _text_width(_FAMILY, "B", title_size, title) > usable:
        title_size *= 0.94
    title_lines = _wrap(title, _FAMILY, "B", title_size, usable)
    top = face_y + max(face_h * 0.07, safety)
    pdf.set_font(_FAMILY, "B", title_size)
    for line in title_lines:
        _shadowed_cell(
            pdf, line, top, title_size * 1.25, offset=title_size * 0.05, x=face_x, width=face_w
        )
        top += title_size * 1.25

    # The line under the title always names the star: the story's own line
    # when it has one ("starring Sarah"), otherwise the book's credit line.
    credit = subtitle or about.cover_credit()
    credit_size = max(16.0, title_size * 0.42)
    pdf.set_font(_FAMILY, "I", credit_size)
    _shadowed_cell(
        pdf,
        credit,
        top + credit_size * 0.4,
        credit_size * 1.4,
        offset=1.2,
        x=face_x,
        width=face_w,
    )


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

    top = height * 0.44
    pdf.set_text_color(*_INK)
    # The same words the reader sets on its copyright page (`book_outline`).
    for text, size, italic in copyright_lines(about.about_name, publisher, year):
        style = "I" if italic else ""
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
    pdf: FPDF,
    text: str,
    y: float,
    line_height: float,
    *,
    offset: float,
    x: float = 0.0,
    width: float | None = None,
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
    pdf.set_xy(x, y)
    pdf.cell(pdf.w if width is None else width, line_height, text, align="C")
    pdf.text_mode = "FILL"


def _draw_page(pdf: FPDF, entry: PageArt, number: int, layout: TemplateGeometry) -> None:
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
        _place_art(pdf, image, 0, 0, layout.art_width, layout.art_height)

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

    # The folio: story page n prints "n"; the pages around the story carry
    # none (P4), and the reader prints the same number.
    pdf.set_font(_FAMILY, "I", 10.0)
    pdf.set_text_color(*_GOLD)
    pdf.set_xy(0, pdf.h - _MARGIN_PAD_BOTTOM * 0.75)
    pdf.cell(pdf.w, 14.0, str(number), align="C")


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
    pdf: FPDF,
    about: AboutMe,
    portrait: bytes | None = None,
    photo: bytes | None = None,
    *,
    inset: float = 0.0,
) -> None:
    """The back cover: heading, framed picture(s) and the About Me sentences.

    The picture is the cartoon, the real photo, or both side by side, as the
    About screen chose. A choice whose image is missing falls back to what is
    there, so the page is never left without the face it can show. "The End"
    is its own page just before this one, so it is not repeated here.

    `inset` keeps every word that far from the page edge: a print page's
    bleed plus its safety margin.
    """
    width, height = pdf.w, pdf.h
    pdf.add_page()
    pdf.set_fill_color(*PAPER_RGB)
    pdf.rect(0, 0, width, height, style="F")
    usable = min(width * (1 - 2 * _SIDE_MARGIN), width - 2 * inset)

    heading, *sentences = (book_text(line) for line in about.rendered_about_lines())
    pdf.set_text_color(*_INK)
    heading_size = width * 0.05
    while heading_size > 16.0 and _text_width(_FAMILY, "B", heading_size, heading) > usable:
        heading_size *= 0.92
    pdf.set_font(_FAMILY, "B", heading_size)
    top = max(height * 0.06, inset)
    pdf.set_xy((width - usable) / 2, top)
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
    bottom = height - max(height * 0.06, inset)
    if sentences:
        available = bottom - top
        continuation_top = max(height * 0.08, inset)
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


# --- the print edition (Q2, A1, A2, A11) ------------------------------------


@dataclass(frozen=True, slots=True)
class PrintGeometry:
    """The one template every story page of a print interior is set in.

    The page is the trim plus bleed, whatever shape the art is. The art runs
    across the full width at its own proportions, and the story is set in the
    band beneath it, inside the safety margin. Only when the words need more
    room than the art leaves does the art box shrink, and the picture is then
    centred on cream rather than cropped or stretched (A11).
    """

    page_width: float
    page_height: float
    art_height: float
    font_size: float
    text_width: float
    text_bottom: float
    folio_top: float

    @property
    def line_height(self) -> float:
        return self.font_size * _LINE_FACTOR

    @property
    def text_top(self) -> float:
        return self.art_height + _MARGIN_PAD_TOP

    @property
    def text_room(self) -> float:
        return self.text_bottom - self.text_top


_FOLIO_SIZE = 10.0


def print_template(
    product: Product, art_ratio: float, stories: Sequence[str]
) -> PrintGeometry:
    """One font size and one art box for the whole printed interior.

    `art_ratio` is the art's height over its width. The largest font at which
    every story fits beneath the art at its natural height wins; past the
    floor the art box gives up height instead, so no story is ever cut.
    """
    page_width, page_height = product.page_size
    inset = product.interior_inset
    text_width = page_width - 2 * max(inset, page_width * _SIDE_MARGIN)
    folio_top = page_height - inset - _FOLIO_SIZE * 1.4
    text_bottom = folio_top - _FOLIO_SIZE
    natural = min(page_width * art_ratio, page_height)
    size = _STORY_FONT_MAX
    while True:
        lines = max(
            (len(_wrap(story, _FAMILY, "", size, text_width)) for story in stories if story),
            default=0,
        )
        room = text_bottom - _MARGIN_PAD_TOP - lines * size * _LINE_FACTOR
        if room >= natural or size <= _MIN_STORY_FONT:
            return PrintGeometry(
                page_width=page_width,
                page_height=page_height,
                art_height=max(0.0, min(natural, room)),
                font_size=size,
                text_width=text_width,
                text_bottom=text_bottom,
                folio_top=folio_top,
            )
        size = max(_MIN_STORY_FONT, size - 0.5)


def build_print_interior(
    about: AboutMe,
    pages: Sequence[PageArt],
    product: Product,
    *,
    portrait: bytes | None = None,
    photo: bytes | None = None,
    publisher: str = "Storybook Studio",
    year: int | None = None,
) -> bytes:
    """The interior file a printer binds: everything but the cover.

    The copyright page, the story, "The End" and the About Me page, each the
    trim plus 0.125 in bleed, every picture at 300 ppi or more (P1), then
    blank pages up to the binding's minimum and an even count (A2). The cover
    is its own file (`build_print_cover`).

    Raises:
        ValueError: if `pages` is empty, no page has a picture, an image is
            unreadable, or the book is too long for the binding.
    """
    if not pages:
        raise ValueError("A book needs at least one page.")
    shape_image = next((entry.image for entry in pages if entry.image is not None), None)
    if shape_image is None:
        raise ValueError("A book needs at least one picture.")
    art_w, art_h = _image_size(shape_image)
    layout = print_template(
        product, art_h / art_w, [book_text(entry.page.story) for entry in pages]
    )

    pdf = _new_pdf(layout.page_width, layout.page_height)
    # The screen book's list, less the cover, which is its own file.
    stories = enumerate(pages, start=1)
    for leaf in book_leaves(len(pages)):
        if leaf is Leaf.COPYRIGHT:
            draw_copyright_page(pdf, about, publisher, year or datetime.now(UTC).year)
        elif leaf is Leaf.STORY:
            number, entry = next(stories)
            _draw_print_page(pdf, entry, layout, number)
        elif leaf is Leaf.END:
            draw_closing_page(pdf)
        elif leaf is Leaf.ABOUT:
            draw_about_page(pdf, about, portrait, photo, inset=product.interior_inset)

    for _ in range(product.printed_pages(pdf.page) - pdf.page):
        pdf.add_page()
        pdf.set_fill_color(*PAPER_RGB)
        pdf.rect(0, 0, pdf.w, pdf.h, style="F")
    return _output(pdf)


def build_print_cover(
    title: str,
    about: AboutMe,
    cover: bytes,
    size: CoverSize,
    product: Product,
    *,
    subtitle: str = "",
    portrait: bytes | None = None,
    photo: bytes | None = None,
) -> bytes:
    """The cover file: one spread of back cover, spine and front cover.

    `size` is the spread the printer asked for (bleed or board wrap included).
    The front is the cover art with the title lockup on the visible face; the
    back is the child's picture in a frame with the title beneath; the spine
    is plain paper, because a book of 80 pages or fewer may carry no spine
    text.
    """
    pdf = _new_pdf(size.width, size.height)
    pdf.add_page()
    pdf.set_fill_color(*PAPER_RGB)
    pdf.rect(0, 0, size.width, size.height, style="F")

    spine_left = (size.width - size.spine) / 2
    front_left = spine_left + size.spine
    _draw_front_cover(
        pdf,
        title,
        about,
        cover,
        subtitle=subtitle,
        box=(front_left, 0.0, size.width - front_left, size.height),
        face=(front_left, size.wrap_y, size.trim_width, size.trim_height),
        safety=product.cover_safety,
        for_print=True,
    )

    back_left = spine_left - size.trim_width
    safety = product.cover_safety
    centre = back_left + size.trim_width / 2
    top = size.wrap_y + max(safety, size.trim_height * 0.12)
    images = _about_images(about.about_photo, portrait, photo)
    side = min(size.trim_width, size.trim_height) * (0.42 if len(images) == 1 else 0.30)
    bottoms = [
        _framed_image(pdf, image, top, max_side=side, centre_x=centre + offset)
        for image, offset in zip(
            images,
            (0.0,) if len(images) == 1 else (-size.trim_width * 0.2, size.trim_width * 0.2),
            strict=False,
        )
    ]
    top = max(bottoms, default=top) + size.trim_height * 0.05
    usable = size.trim_width - 2 * safety
    title_size = 22.0
    pdf.set_text_color(*_INK)
    pdf.set_font(_FAMILY, "I", title_size)
    for line in _wrap(title, _FAMILY, "I", title_size, usable):
        pdf.set_xy(back_left + safety, top)
        pdf.cell(usable, title_size * 1.4, line, align="C")
        top += title_size * 1.4
    return _output(pdf)


def _draw_print_page(pdf: FPDF, entry: PageArt, layout: PrintGeometry, number: int) -> None:
    """One printed story page: the art across the top, the story beneath."""
    pdf.add_page()
    pdf.set_fill_color(*PAPER_RGB)
    pdf.rect(0, 0, pdf.w, pdf.h, style="F")
    image = entry.image
    art = TemplateGeometry(layout.page_width, layout.art_height, 0.0, layout.font_size)
    if image is None:
        _draw_missing_art(pdf, art)
    else:
        _place_art(pdf, image, 0, 0, layout.page_width, layout.art_height, for_print=True)
    pdf.set_draw_color(*_GOLD)
    pdf.set_line_width(1.6)
    pdf.line(0, layout.art_height, pdf.w, layout.art_height)

    story = book_text(entry.page.story)
    if story.strip():
        lines = _wrap(story, _FAMILY, "", layout.font_size, layout.text_width)
        block = len(lines) * layout.line_height
        y = layout.text_top + max(0.0, (layout.text_room - block) / 2)
        pdf.set_font(_FAMILY, "", layout.font_size)
        pdf.set_text_color(*_INK)
        _centred_lines(pdf, lines, y, layout.line_height)

    # Story page n prints "n", as on screen and in the reading PDF (P4).
    pdf.set_font(_FAMILY, "I", _FOLIO_SIZE)
    pdf.set_text_color(*_GOLD)
    pdf.set_xy(0, layout.folio_top)
    pdf.cell(pdf.w, _FOLIO_SIZE * 1.4, str(number), align="C")


def _place_art(
    pdf: FPDF,
    image: bytes,
    x: float,
    y: float,
    width: float,
    height: float,
    *,
    for_print: bool = False,
) -> None:
    """The picture in its box at its own shape, centred on cream (A11).

    A picture whose shape differs from the box is never stretched or cropped:
    it is scaled to fit and the cream shows at the sides or above and below.
    """
    art_w, art_h = _image_size(image)
    scale = min(width / art_w, height / art_h)
    drawn_w, drawn_h = art_w * scale, art_h * scale
    if abs(drawn_w - width) > 0.5 or abs(drawn_h - height) > 0.5:
        logger.info(
            "pdf.art_letterboxed picture=%dx%d box=%.0fx%.0f", art_w, art_h, width, height
        )
        pdf.set_fill_color(*_CREAM)
        pdf.rect(x, y, width, height, style="F")
    pdf.image(
        _image_for_pdf(image, printed_at=(drawn_w, drawn_h) if for_print else None),
        x=x + (width - drawn_w) / 2,
        y=y + (height - drawn_h) / 2,
        w=drawn_w,
        h=drawn_h,
    )


def _new_pdf(width: float, height: float) -> FPDF:
    pdf = FPDF(unit="pt", format=(width, height))
    pdf.set_auto_page_break(False)
    _register_fonts(pdf)
    if _shaping_supported():
        pdf.set_text_shaping(True)
    return pdf


def _output(pdf: FPDF) -> bytes:
    output = io.BytesIO()
    pdf.output(output)
    return output.getvalue()


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


def _image_for_pdf(
    image: bytes, *, printed_at: tuple[float, float] | None = None
) -> io.BytesIO:
    """Re-encode one picture as a right-sized JPEG for embedding.

    `printed_at` is the picture's size on a print page, in points. A print
    picture keeps every pixel it has and, when that is under `_PRINT_PPI` at
    that size, is resampled up to it (P1). Without it the picture is sized
    for the screen PDF.

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
            if printed_at is None:
                frame.thumbnail((_EMBED_LONG_EDGE, _EMBED_LONG_EDGE), Image.Resampling.LANCZOS)
            else:
                frame = _upscale_for_print(frame, printed_at)
            encoded = io.BytesIO()
            # A plain baseline JPEG on purpose: with optimize= or progressive=
            # Pillow encodes the whole frame in one call that holds the GIL
            # (~270 ms a page), freezing the server for the whole assembly; a
            # baseline encode works in chunks. The size cost is about 5%.
            frame.save(
                encoded,
                format="JPEG",
                quality=_EMBED_JPEG_QUALITY if printed_at is None else _PRINT_JPEG_QUALITY,
            )
    except (OSError, UnidentifiedImageError) as exc:
        raise ValueError("A page image was unreadable; cannot assemble the book.") from exc
    encoded.seek(0)
    return encoded


def _upscale_for_print(frame: Image.Image, printed_at: tuple[float, float]) -> Image.Image:
    """`frame` resampled up to `_PRINT_PPI` at its printed size, if it is under.

    Lanczos keeps the painted edges clean, and a light unsharp mask gives
    back the crispness resampling softens. A picture already at or above the
    density is returned untouched: a print picture is never downscaled.
    """
    width_pt, height_pt = printed_at
    factor = max(
        width_pt / _POINTS_PER_INCH * _PRINT_PPI / frame.width,
        height_pt / _POINTS_PER_INCH * _PRINT_PPI / frame.height,
    )
    if factor <= 1.0:
        return frame
    size = (math.ceil(frame.width * factor), math.ceil(frame.height * factor))
    logger.info("pdf.print_upscale from=%dx%d to=%dx%d", frame.width, frame.height, *size)
    resized = frame.resize(size, Image.Resampling.LANCZOS)
    return resized.filter(ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=3))


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
