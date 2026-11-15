"""What a printed book costs us, what it sells for, and the margin between.

All amounts are integer US cents. The buyer pays the edition's price plus
shipping at cost (AUDIT.md §10 Q2/A6), so shipping moves no margin either way;
the model's lines are:

* **generation** — the measured average model spend for one book's pictures,
  reviews and decisions. The first live run's ledger gave $1.58 a book
  (`review-log.md`, Run 1 addendum); the retry caps have lowered the ceiling
  since, so the measured average is conservative.
* **printing** — Lulu's published wholesale price for the edition: a base per
  book plus a per-page rate (`app/services/print/products.py`). The order
  itself is priced by Lulu's quote for the address, so this is the planning
  figure.
* **shipping** — whatever Lulu quotes for the address, charged to the buyer
  unchanged.
* **payment processing** — Stripe's 2.9% + 30¢ on everything the buyer pays,
  shipping included: the one real cost shipping does carry.

The write-up (`docs/print-business-model.md`) prints the same math the tests
assert, so the document cannot drift from the code.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.print.products import Product

__all__ = [
    "GENERATION_COST_CENTS",
    "PAYMENT_FIXED_CENTS",
    "PAYMENT_PERCENT",
    "QuoteBreakdown",
    "business_model",
]

#: The measured average generation spend for one book (Run 1's ledger).
GENERATION_COST_CENTS = 158

#: Stripe's standard card rate, used for the processing line.
PAYMENT_PERCENT = 2.9
PAYMENT_FIXED_CENTS = 30


@dataclass(frozen=True, slots=True)
class QuoteBreakdown:
    """One printed book, line by line: cost, price and margin."""

    product_key: str
    page_count: int
    generation_cents: int
    printing_cents: int
    shipping_cents: int
    processing_cents: int
    retail_cents: int

    @property
    def charged_cents(self) -> int:
        """What the buyer pays: the edition's price and shipping at cost."""
        return self.retail_cents + self.shipping_cents

    @property
    def total_cost_cents(self) -> int:
        return (
            self.generation_cents
            + self.printing_cents
            + self.shipping_cents
            + self.processing_cents
        )

    @property
    def margin_cents(self) -> int:
        return self.charged_cents - self.total_cost_cents

    @property
    def margin_percent(self) -> float:
        """The margin as a share of the edition's price (shipping is passed on)."""
        if self.retail_cents <= 0:
            return 0.0
        return 100.0 * self.margin_cents / self.retail_cents


def _processing_cents(charged_cents: int) -> int:
    """Stripe's cut: 2.9% rounded to the nearest cent, plus 30¢."""
    return round(charged_cents * PAYMENT_PERCENT / 100.0) + PAYMENT_FIXED_CENTS


def business_model(
    product: Product,
    page_count: int,
    *,
    shipping_cents: int = 0,
    generation_cents: int = GENERATION_COST_CENTS,
) -> QuoteBreakdown:
    """The full cost and margin of one copy of `product` with `page_count` pages."""
    pages = product.printed_pages(page_count)
    return QuoteBreakdown(
        product_key=product.key,
        page_count=pages,
        generation_cents=generation_cents,
        printing_cents=product.estimated_printing_cents(pages),
        shipping_cents=shipping_cents,
        processing_cents=_processing_cents(product.retail_cents + shipping_cents),
        retail_cents=product.retail_cents,
    )
