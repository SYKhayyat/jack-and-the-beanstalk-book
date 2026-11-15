"""The printed books a buyer can choose: two trims, two bindings (Q2).

Every number here is Lulu's, from its product spec sheet and Book Creation
Guide (AUDIT.md §10 records the sources):

* trims **8.5 x 8.5 in** (square) and **8.5 x 11 in** (US Letter, portrait);
  Lulu has no 8 x 10, so Letter is the tall choice;
* bindings **hardcover casewrap** and **paperback perfect bound**, both premium
  full colour on 80# white coated paper with a gloss cover;
* every file carries a **0.125 in bleed** on each side, so an interior page is
  the trim plus 0.25 in;
* the interior keeps words **0.5 in** inside the trim; a casewrap cover keeps
  them **0.75 in** inside (the board wrap eats more), a paperback 0.25 in;
* a binding has a minimum interior page count, and short books are padded
  with blank pages to reach it (A2).

The cover's exact size is the provider's to say (Lulu's ``/cover-dimensions/``
answers for the real page count); `estimated_cover` is the published formula,
used offline and by the test provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "BLEED_IN",
    "PRODUCTS",
    "Binding",
    "CoverSize",
    "Product",
    "Trim",
    "product_for",
]

POINTS_PER_INCH = 72.0
BLEED_IN = 0.125
INTERIOR_SAFETY_IN = 0.5


class Trim(StrEnum):
    SQUARE = "square"
    LETTER = "letter"


class Binding(StrEnum):
    HARDCOVER = "hardcover"
    PAPERBACK = "paperback"


_TRIM_INCHES: dict[Trim, tuple[float, float]] = {
    Trim.SQUARE: (8.5, 8.5),
    Trim.LETTER: (8.5, 11.0),
}
_TRIM_CODE: dict[Trim, str] = {Trim.SQUARE: "0850X0850", Trim.LETTER: "0850X1100"}
_BINDING_CODE: dict[Binding, str] = {Binding.HARDCOVER: "CW", Binding.PAPERBACK: "PB"}

#: Hardcover spine width by interior page count (Lulu's table; inches).
_HARDCOVER_SPINE: tuple[tuple[int, float], ...] = (
    (84, 0.25),
    (140, 0.5),
    (168, 0.625),
    (194, 0.688),
    (222, 0.75),
)


@dataclass(frozen=True, slots=True)
class CoverSize:
    """One cover spread (back, spine, front), in points.

    `wrap_x`/`wrap_y` are what lies outside the trimmed face on each outer
    edge: the bleed on a paperback, the board wrap on a casewrap.
    """

    width: float
    height: float
    spine: float
    trim_width: float
    trim_height: float

    @property
    def wrap_x(self) -> float:
        return max(0.0, (self.width - self.spine - 2 * self.trim_width) / 2)

    @property
    def wrap_y(self) -> float:
        return max(0.0, (self.height - self.trim_height) / 2)


@dataclass(frozen=True, slots=True)
class Product:
    """One printable edition: a trim, a binding and what it sells for."""

    trim: Trim
    binding: Binding
    min_pages: int
    max_pages: int
    retail_cents: int

    @property
    def key(self) -> str:
        return f"{self.trim}-{self.binding}"

    @property
    def pod_package_id(self) -> str:
        """Lulu's product code: trim, full colour, premium, binding, 80# coated, gloss."""
        return f"{_TRIM_CODE[self.trim]}.FC.PRE.{_BINDING_CODE[self.binding]}.080CW444.GXX"

    @property
    def trim_inches(self) -> tuple[float, float]:
        return _TRIM_INCHES[self.trim]

    @property
    def label(self) -> str:
        width, height = self.trim_inches
        shape = "square" if self.trim == Trim.SQUARE else "US Letter"
        binding = "Hardcover" if self.binding == Binding.HARDCOVER else "Paperback"
        return f"{binding}, {width:g} \u00d7 {height:g} in {shape}"

    @property
    def page_size(self) -> tuple[float, float]:
        """One interior page with bleed, in points."""
        width, height = self.trim_inches
        return (
            (width + 2 * BLEED_IN) * POINTS_PER_INCH,
            (height + 2 * BLEED_IN) * POINTS_PER_INCH,
        )

    @property
    def bleed(self) -> float:
        return BLEED_IN * POINTS_PER_INCH

    @property
    def interior_inset(self) -> float:
        """From the page edge to where words may sit: bleed plus safety margin."""
        return (BLEED_IN + INTERIOR_SAFETY_IN) * POINTS_PER_INCH

    @property
    def cover_safety(self) -> float:
        """From the trimmed face's edge to where cover words may sit."""
        return (0.75 if self.binding == Binding.HARDCOVER else 0.25) * POINTS_PER_INCH

    def printed_pages(self, content_pages: int) -> int:
        """The interior page count once padded with blanks (A2).

        At least the binding's minimum, and even, because a leaf has two sides.

        Raises:
            ValueError: if the book is longer than the binding can hold.
        """
        if content_pages < 1:
            raise ValueError("a printed book needs at least one page")
        pages = max(self.min_pages, content_pages)
        pages += pages % 2
        if pages > self.max_pages:
            raise ValueError(f"{self.label} holds at most {self.max_pages} pages")
        return pages

    def spine_inches(self, pages: int) -> float:
        if self.binding == Binding.PAPERBACK:
            return pages / 444 + 0.06
        for most, width in _HARDCOVER_SPINE:
            if pages <= most:
                return width
        raise ValueError("the spine table does not reach this page count")

    def estimated_cover(self, pages: int) -> CoverSize:
        """The cover spread from Lulu's published formula.

        A casewrap cover carries a 0.75 in board wrap around the face; a
        paperback carries the ordinary bleed.
        """
        width, height = self.trim_inches
        wrap = 0.75 if self.binding == Binding.HARDCOVER else BLEED_IN
        spine = self.spine_inches(pages)
        return self.cover_from(
            width_pt=(2 * width + spine + 2 * wrap) * POINTS_PER_INCH,
            height_pt=(height + 2 * wrap) * POINTS_PER_INCH,
            pages=pages,
        )

    def cover_from(self, *, width_pt: float, height_pt: float, pages: int) -> CoverSize:
        """A cover spread of a size the provider gave, with this book's spine."""
        width, height = self.trim_inches
        return CoverSize(
            width=width_pt,
            height=height_pt,
            spine=self.spine_inches(pages) * POINTS_PER_INCH,
            trim_width=width * POINTS_PER_INCH,
            trim_height=height * POINTS_PER_INCH,
        )


#: Lulu's minimums: 24 interior pages for casewrap, 20 for perfect bound (the
#: spec sheet for these packages). Prices are the owner's (AUDIT.md §10 Q2).
PRODUCTS: dict[str, Product] = {
    product.key: product
    for product in (
        Product(Trim.SQUARE, Binding.HARDCOVER, 24, 800, 2999),
        Product(Trim.SQUARE, Binding.PAPERBACK, 20, 800, 1799),
        Product(Trim.LETTER, Binding.HARDCOVER, 24, 800, 2999),
        Product(Trim.LETTER, Binding.PAPERBACK, 20, 800, 1799),
    )
}


def product_for(trim: str, binding: str) -> Product:
    """The product a buyer chose, by its two form values.

    Raises:
        ValueError: if either value is not one this shop sells.
    """
    found = PRODUCTS.get(f"{Trim(trim)}-{Binding(binding)}")
    if found is None:  # pragma: no cover - every pair is listed
        raise ValueError("no such printed edition")
    return found
