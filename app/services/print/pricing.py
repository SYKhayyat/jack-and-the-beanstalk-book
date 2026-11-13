"""What a printed book costs us, what it sells for, and the margin between.

All amounts are integer US cents. The model has four costs and one price:

* **generation** — the measured average model spend for one book's pictures,
  reviews and decisions. The first live run's ledger gave $1.58 a book
  (`review-log.md`, Run 1 addendum); the retry budget caps have lowered the
  ceiling since, so using the measured average is conservative.
* **printing** — Peecho's published softcover base and per-page wholesale,
  converted from euro at 1.08. A 24-page A5 softcover is
  ``378 + 24 x 17 = 786`` cents.
* **shipping** — Peecho's published shipping example, €5.90, converted at the
  same rate: 637 cents. The real figure depends on the destination; the fixed
  number is the model's stated assumption.
* **payment processing** — Stripe's 2.9% + 30¢ on the retail price. This is a
  real cost of taking the order, so it belongs in the margin rather than being
  discovered afterward.

The price is a setting (default $24.99) because it is a business decision, not
a constant. The write-up (`docs/print-business-model.md`) prints the same math
the tests assert, so the document cannot drift from the code.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.pricing import (
    PRINT_BASE_COST_CENTS,
    PRINT_PER_PAGE_CENTS,
    PRINT_RETAIL_CENTS,
    PRINT_SHIPPING_CENTS,
)
from app.services.print.base import PrintQuote

__all__ = [
    "GENERATION_COST_CENTS",
    "PAYMENT_FIXED_CENTS",
    "PAYMENT_PERCENT",
    "PRINT_BASE_COST_CENTS",
    "PRINT_PER_PAGE_CENTS",
    "PRINT_RETAIL_CENTS",
    "PRINT_SHIPPING_CENTS",
    "QuoteBreakdown",
    "build_quote",
]

#: The measured average generation spend for one book (Run 1's ledger).
GENERATION_COST_CENTS = 158

#: Stripe's standard card rate, used for the processing line.
PAYMENT_PERCENT = 2.9
PAYMENT_FIXED_CENTS = 30


@dataclass(frozen=True, slots=True)
class QuoteBreakdown:
    """One printed book, line by line: cost, price and margin."""

    page_count: int
    generation_cents: int
    printing_cents: int
    shipping_cents: int
    processing_cents: int
    retail_cents: int

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
        return self.retail_cents - self.total_cost_cents

    @property
    def margin_percent(self) -> float:
        if self.retail_cents <= 0:
            return 0.0
        return 100.0 * self.margin_cents / self.retail_cents


def _processing_cents(retail_cents: int) -> int:
    """Stripe's cut, rounded up to the nearest cent."""
    return round(retail_cents * PAYMENT_PERCENT / 100.0) + PAYMENT_FIXED_CENTS


def business_model(
    page_count: int,
    *,
    retail_cents: int = PRINT_RETAIL_CENTS,
    generation_cents: int = GENERATION_COST_CENTS,
    base_cents: int = PRINT_BASE_COST_CENTS,
    per_page_cents: int = PRINT_PER_PAGE_CENTS,
    shipping_cents: int = PRINT_SHIPPING_CENTS,
) -> QuoteBreakdown:
    """The full cost and margin for a book of `page_count` printed pages."""
    if page_count < 1:
        raise ValueError("a printed book needs at least one page")
    return QuoteBreakdown(
        page_count=page_count,
        generation_cents=generation_cents,
        printing_cents=base_cents + per_page_cents * page_count,
        shipping_cents=shipping_cents,
        processing_cents=_processing_cents(retail_cents),
        retail_cents=retail_cents,
    )


def build_quote(
    page_count: int,
    *,
    retail_cents: int = PRINT_RETAIL_CENTS,
    base_cents: int = PRINT_BASE_COST_CENTS,
    per_page_cents: int = PRINT_PER_PAGE_CENTS,
    shipping_cents: int = PRINT_SHIPPING_CENTS,
    page_width_mm: float = 210.0,
    page_height_mm: float = 148.0,
) -> PrintQuote:
    """What the customer is shown and charged for one printed copy."""
    if page_count < 1:
        raise ValueError("a printed book needs at least one page")
    return PrintQuote(
        page_count=page_count,
        printing_cents=base_cents + per_page_cents * page_count,
        shipping_cents=shipping_cents,
        retail_cents=retail_cents,
        page_width_mm=page_width_mm,
        page_height_mm=page_height_mm,
    )
