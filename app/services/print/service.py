"""Review, approve, then place a test print order.

The two promises this module keeps:

1. **Nothing is sent before the owner approves the exact PDF.** An approval
   stores the PDF's SHA-256. Placing the order re-reads the PDF and refuses if
   the hash changed, so a redraw between review and order cannot print a book
   the owner never saw.
2. **A print order costs money and is refunded if the provider refuses.** The
   wallet is charged once, by order id; a failed submission writes the money
   back with a `print_refund` row, so the account is never left paying for a
   job that did not exist.

The provider is injected. In this build it is the mock or Peecho's test
environment; there is no production implementation.
"""

from __future__ import annotations

import hashlib
import io
import logging
from collections.abc import Mapping

from pypdf import PdfReader

from app.moderation import about_issues, first_issue_message
from app.services.print.base import (
    PrintError,
    PrintOrder,
    PrintProvider,
    PrintQuote,
    ShippingAddress,
)
from app.services.print.pricing import build_quote
from app.services.print.store import PrintOrderStore
from app.services.wallet import InsufficientFunds, Wallet

__all__ = ["PrintService"]

logger = logging.getLogger(__name__)


class PrintService:
    """The rules around one printed copy of one book."""

    def __init__(
        self,
        *,
        store: PrintOrderStore,
        provider: PrintProvider,
        wallet: Wallet | None,
        retail_cents: int,
        base_cost_cents: int,
        per_page_cents: int,
        shipping_cents: int,
        enabled: bool = True,
    ) -> None:
        self._store = store
        self._provider = provider
        self._wallet = wallet
        self._retail_cents = retail_cents
        self._base_cost_cents = base_cost_cents
        self._per_page_cents = per_page_cents
        self._shipping_cents = shipping_cents
        self._enabled = enabled

    @property
    def provider_name(self) -> str:
        return self._provider.name

    @property
    def mode(self) -> str:
        return self._provider.mode

    @property
    def enabled(self) -> bool:
        """Whether this deployment can take a print order at all.

        Both a configured provider and a wallet are required: a printed book is
        a purchase, and a deployment without payments should say so rather than
        quietly ship for free.
        """
        return self._enabled and self._wallet is not None

    def quote(self, page_count: int) -> PrintQuote:
        return build_quote(
            page_count,
            retail_cents=self._retail_cents,
            base_cents=self._base_cost_cents,
            per_page_cents=self._per_page_cents,
            shipping_cents=self._shipping_cents,
        )

    def quote_for_pdf(self, pdf: bytes) -> PrintQuote:
        """The price and the trim size of the file that would be printed.

        The page count and size come from the PDF itself: a provider is told
        what it is about to receive, not what the edition happened to say.
        """
        page_count, width_mm, height_mm = measure_pdf(pdf)
        return build_quote(
            page_count,
            retail_cents=self._retail_cents,
            base_cents=self._base_cost_cents,
            per_page_cents=self._per_page_cents,
            shipping_cents=self._shipping_cents,
            page_width_mm=width_mm,
            page_height_mm=height_mm,
        )

    def latest(self, book_id: str) -> PrintOrder | None:
        return self._store.latest_for_book(book_id)

    def approval(self, book_id: str) -> PrintOrder | None:
        return self._store.current_approval(book_id)

    def approve(
        self,
        *,
        book_id: str,
        user_id: str,
        pdf: bytes,
        page_count: int | None = None,
    ) -> PrintOrder:
        """Record that the owner has reviewed this exact PDF."""
        self._require_enabled()
        quote = self.quote_for_pdf(pdf)
        if page_count is not None and page_count != quote.page_count:
            logger.warning(
                "print.page_count_mismatch book=%s edition=%d pdf=%d",
                book_id,
                page_count,
                quote.page_count,
            )
        approved = self._store.create_approval(
            book_id=book_id,
            user_id=user_id,
            pdf_sha256=hashlib.sha256(pdf).hexdigest(),
            page_count=quote.page_count,
            printing_cents=quote.printing_cents,
            shipping_cents=quote.shipping_cents,
            retail_cents=quote.retail_cents,
            currency=quote.currency,
            provider=self._provider.name,
        )
        logger.info(
            "print.approved user=%s book=%s order=%s pages=%d",
            user_id,
            book_id,
            approved.id,
            quote.page_count,
        )
        return approved

    def place(
        self,
        *,
        order: PrintOrder,
        pdf: bytes,
        pdf_url: str,
        address: ShippingAddress,
        about: Mapping[str, object] | object,
    ) -> PrintOrder:
        """Charge for the copy, then ask the provider for the job.

        Raises:
            PrintError: for an unapproved/stale book, an address the provider
                would refuse, or an About page that may not be printed.
            InsufficientFunds: when the wallet cannot cover the copy.
        """
        self._require_enabled()
        if order.state != "approved":
            raise PrintError("This print order has already been placed.")
        if order.pdf_sha256 != hashlib.sha256(pdf).hexdigest():
            raise PrintError(
                "The book changed after you reviewed it. Open the print page again, "
                "read the new PDF and approve it before ordering."
            )
        problems = address.issues()
        if problems:
            raise PrintError(" ".join(problems))
        issues = about_issues(about)
        if issues:
            raise PrintError(
                "This book cannot be printed as it is: " + first_issue_message(issues)
            )

        measured = self.quote_for_pdf(pdf)

        quote = PrintQuote(
            page_count=order.page_count,
            printing_cents=order.printing_cents,
            shipping_cents=order.shipping_cents,
            retail_cents=order.retail_cents,
            page_width_mm=measured.page_width_mm,
            page_height_mm=measured.page_height_mm,
            currency=order.currency,
        )
        # One conditional UPDATE from `approved` to `placing`. A second form
        # submit for the same approval hits a row that is no longer approved
        # and stops here, before it can charge again or order a second copy.
        claimed = self._store.claim(order.id)
        if claimed is None:
            raise PrintError(
                "This print order is already being placed. Refresh the page to see "
                "where it got to."
            )

        assert self._wallet is not None  # _require_enabled has checked it
        try:
            self._wallet.charge_print(
                order.user_id,
                reference=f"print:{order.id}",
                description=(
                    f"Printed copy of your book — printing and shipping "
                    f"({quote.page_count} pages)"
                ),
                amount_cents=quote.retail_cents,
            )
        except InsufficientFunds:
            # Nothing was sent, and the approval is still good: hand it back to
            # the owner so they can fund the wallet and press the button again.
            self._store.release(order.id)
            raise

        try:
            job = self._provider.submit(
                order_id=order.id,
                book_id=order.book_id,
                pdf_url=pdf_url,
                address=address,
                quote=quote,
            )
        except PrintError as exc:
            self._fail(order, quote, str(exc))
            logger.warning("print.refused book=%s order=%s", order.book_id, order.id)
            raise
        except Exception as exc:
            # A bug or an unhandled transport error must not leave the account
            # paying for a job nobody knows about. Return the charge, mark the
            # order failed with a sentence a visitor can read, and log the bug.
            logger.exception(
                "print.unexpected_failure book=%s order=%s", order.book_id, order.id
            )
            self._fail(
                order,
                quote,
                "the print service failed unexpectedly; nothing is known to have been printed",
            )
            raise PrintError(
                "The print service failed unexpectedly. The charge has been returned "
                "and nothing is known to have been printed; please try again."
            ) from exc

        submitted = self._store.mark_submitted(
            order.id,
            provider=job.provider,
            provider_order_id=job.provider_order_id,
            address=address.as_dict(),
        )
        logger.info(
            "print.submitted book=%s order=%s provider=%s provider_order=%s",
            order.book_id,
            order.id,
            job.provider,
            job.provider_order_id,
        )
        return submitted or order

    def _fail(self, order: PrintOrder, quote: PrintQuote, error: str) -> None:
        """Give the money back and record why the job failed.

        A refund that itself fails is logged rather than raised over the
        original error: the operator needs the provider's reason more than a
        second exception, and the missing credit is visible in the ledger.
        """
        assert self._wallet is not None  # _require_enabled has checked it
        try:
            self._wallet.refund_print(
                order.user_id,
                reference=f"print:{order.id}:refund",
                description=(
                    "Printed copy — returned because the print order did not go through"
                ),
                amount_cents=quote.retail_cents,
            )
        except Exception:
            logger.exception("print.refund_failed book=%s order=%s", order.book_id, order.id)
        self._store.mark_failed(order.id, provider=self._provider.name, error=error)

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise PrintError(
                "Printed copies are not available on this deployment: printing needs "
                "a configured print service and a wallet."
            )


def measure_pdf(pdf: bytes) -> tuple[int, float, float]:
    """A PDF's page count and its first page's size, in millimetres.

    Point sizes are converted with the PDF's own 72 points to the inch. An
    unreadable file is refused rather than quoted at a guessed size: printing
    a book nobody could measure is worse than asking for a new PDF, and the
    page count is part of the price.

    Raises:
        PrintError: if the file cannot be read or has no measurable page.
    """
    try:
        reader = PdfReader(io.BytesIO(pdf))
        page_count = len(reader.pages)
        box = reader.pages[0].mediabox
        width_mm = float(box.width) * 25.4 / 72.0
        height_mm = float(box.height) * 25.4 / 72.0
    except Exception as exc:
        logger.warning("print.unreadable_pdf bytes=%d", len(pdf))
        raise PrintError(
            "This book's PDF could not be read, so it cannot be priced or printed. "
            "Open the book and download the PDF to check it."
        ) from exc
    if page_count < 1 or width_mm <= 0 or height_mm <= 0:
        raise PrintError("This book's PDF has no measurable pages, so it cannot be printed.")
    return page_count, round(width_mm, 1), round(height_mm, 1)
