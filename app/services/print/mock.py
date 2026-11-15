"""A print provider that never leaves the process.

It is the default in development, tests and the hand-in demo: the whole flow —
choose an edition, approve, address, quote, "place order" — works, and the
order page says plainly that nothing will be printed. It records every job, so
a test can prove the file URLs, address and page count a real provider would
have received. Prices are Lulu's published wholesale figures, and shipping is a
flat table by destination.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.services.print.base import PrintCost, PrintJob, ShippingAddress
from app.services.print.products import CoverSize, Product

__all__ = ["MockPrintProvider", "Submitted"]

_SHIPPING_CENTS = {"US": 499, "CA": 899, "MX": 899}
_ELSEWHERE_CENTS = 1299


@dataclass(frozen=True, slots=True)
class Submitted:
    order_id: str
    product: Product
    pages: int
    interior_url: str
    cover_url: str
    address: ShippingAddress
    title: str


class MockPrintProvider:
    """A test-mode provider: records the job and returns a stable id."""

    name = "mock"
    mode = "test"

    def __init__(self) -> None:
        self.submitted: list[Submitted] = []
        self._jobs: dict[str, PrintJob] = {}

    def cover_size(self, product: Product, pages: int) -> CoverSize:
        return product.estimated_cover(pages)

    def quote(self, product: Product, pages: int, address: ShippingAddress) -> PrintCost:
        code = address.country_code.strip().upper()
        return PrintCost(
            printing_cents=product.estimated_printing_cents(pages),
            shipping_cents=_SHIPPING_CENTS.get(code, _ELSEWHERE_CENTS),
        )

    def submit(
        self,
        *,
        order_id: str,
        product: Product,
        pages: int,
        interior_url: str,
        cover_url: str,
        address: ShippingAddress,
        title: str,
    ) -> PrintJob:
        self.submitted.append(
            Submitted(
                order_id=order_id,
                product=product,
                pages=pages,
                interior_url=interior_url,
                cover_url=cover_url,
                address=address,
                title=title,
            )
        )
        job = PrintJob(
            provider=self.name,
            provider_order_id=f"mock-{uuid.uuid4().hex[:12]}",
            state="submitted",
            detail="Test order: recorded locally, nothing will be printed or shipped.",
        )
        self._jobs[order_id] = job
        return job

    def find(self, order_id: str) -> PrintJob | None:
        return self._jobs.get(order_id)
