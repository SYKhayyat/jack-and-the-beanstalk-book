"""A print provider that never leaves the process.

It is the default in development, tests and the hand-in demo: the whole flow —
review, approve, address, quote, "place order" — works, and the order page
says plainly that nothing will be printed. It also records every request, so a
test can prove the PDF URL, address and page count that a real provider would
have received.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.services.print.base import PrintJob, PrintQuote, ShippingAddress

__all__ = ["MockPrintProvider"]


@dataclass(frozen=True, slots=True)
class Submitted:
    order_id: str
    book_id: str
    pdf_url: str
    address: ShippingAddress
    quote: PrintQuote


class MockPrintProvider:
    """A test-mode provider: records the job and returns a stable id."""

    name = "mock"
    mode = "test"

    def __init__(self) -> None:
        self.submitted: list[Submitted] = []

    def submit(
        self,
        *,
        order_id: str,
        book_id: str,
        pdf_url: str,
        address: ShippingAddress,
        quote: PrintQuote,
    ) -> PrintJob:
        self.submitted.append(
            Submitted(
                order_id=order_id,
                book_id=book_id,
                pdf_url=pdf_url,
                address=address,
                quote=quote,
            )
        )
        return PrintJob(
            provider=self.name,
            provider_order_id=f"mock-{uuid.uuid4().hex[:12]}",
            state="submitted",
            detail="Test order: recorded locally, nothing will be printed or shipped.",
        )
