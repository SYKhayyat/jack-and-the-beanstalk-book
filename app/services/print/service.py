"""Review, approve, quote, then place a sandbox print order.

The promises this module keeps:

1. **Nothing is sent before the owner approves the exact book.** An approval
   stores the web PDF's SHA-256 and builds the print interior and cover from
   the same book then, storing both files with their hashes. Placing the
   order refuses if the web PDF changed, and the printer is served the stored
   bytes only while their hash still matches (A3).
2. **The buyer pays the edition's price plus the provider's own shipping
   quote for their address** (A6), charged once from the wallet, by order id.
3. **Money follows what is known.** A provider that clearly refused gets the
   charge returned at once. A request whose answer was lost (a timeout, a
   5xx) leaves the order `unknown`, charged and not refunded: the job may
   exist. The next attempt looks the job up by our order id before sending
   anything again, and the buyer may ask for a refund (A4).

The provider is injected: the mock, or Lulu's sandbox. There is no
production host in this code.
"""

from __future__ import annotations

import hashlib
import io
import logging
from collections.abc import Callable, Mapping

from pypdf import PdfReader

from app.moderation import ContentIssue, about_issues, first_issue_message
from app.services.content_screen import ContentScreen
from app.services.image_providers.base import ProviderError
from app.services.print.base import (
    PrintError,
    PrintJob,
    PrintOrder,
    PrintProvider,
    PrintUncertain,
    ShippingAddress,
)
from app.services.print.products import CoverSize, Product, product_by_key
from app.services.print.store import PrintOrderStore
from app.services.wallet import InsufficientFunds, Wallet

__all__ = ["FileUrls", "PrintService", "count_pages"]

logger = logging.getLogger(__name__)

#: Builds the signed links the printer fetches an order's two files from.
FileUrls = Callable[[PrintOrder], tuple[str, str]]

#: Shown when the outcome of a placement is not known (A4).
UNKNOWN_MESSAGE = (
    "The print service did not confirm whether it took this order. Your wallet "
    "has been charged and nothing will be sent twice: the order will be looked "
    "up before it is tried again. Press “Try again” to look it up and finish it, "
    "or “Cancel and refund” to have it looked up and, if the printer has no such "
    "order, your money returned."
)


class PrintService:
    """The rules around one printed copy of one book."""

    def __init__(
        self,
        *,
        store: PrintOrderStore,
        provider: PrintProvider,
        wallet: Wallet | None,
        enabled: bool = True,
        content_screen: ContentScreen | None = None,
    ) -> None:
        self._store = store
        self._provider = provider
        self._wallet = wallet
        self._enabled = enabled
        self._content_screen = content_screen

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

    def _screened(self, about: object) -> list[ContentIssue]:
        """The decision model's look at the words, before anything is charged.

        Printing is the step that puts the words on paper, so an unreachable
        screen refuses the order rather than letting it through unchecked.
        """
        if self._content_screen is None:
            return []
        try:
            return self._content_screen.screen(about)
        except ProviderError as exc:
            logger.warning("print.content_screen_unreachable error=%s", exc)
            raise PrintError(
                "We could not check this book's words just now, so it was not "
                "ordered and nothing was charged. Please try again in a minute."
            ) from None

    def latest(self, book_id: str) -> PrintOrder | None:
        return self._store.latest_for_book(book_id)

    def approval(self, book_id: str) -> PrintOrder | None:
        return self._store.current_approval(book_id)

    def get(self, order_id: str) -> PrintOrder | None:
        return self._store.get(order_id)

    def read_file(self, order_id: str, kind: str, sha256: str) -> bytes | None:
        """An approved print file, only if it is still the one approved (A3)."""
        if kind not in {"interior", "cover"}:
            return None
        found = self._store.read_file(order_id, "interior" if kind == "interior" else "cover")
        if found is None:
            return None
        data, recorded = found
        if recorded != sha256 or hashlib.sha256(data).hexdigest() != sha256:
            logger.warning("print.file_hash_mismatch order=%s kind=%s", order_id, kind)
            return None
        return data

    def approve(
        self,
        *,
        book_id: str,
        user_id: str,
        pdf: bytes,
        product: Product,
        build_interior: Callable[[Product], bytes | None],
        build_cover: Callable[[Product, CoverSize], bytes | None],
    ) -> PrintOrder:
        """Record that the owner reviewed this book, and build its print files.

        The interior is built first so the cover can be sized for its real
        page count, which the provider answers (Lulu's cover size depends on
        the spine).

        Raises:
            PrintError: if the files cannot be built, or the provider cannot
                size the cover.
        """
        self._require_enabled()
        interior = build_interior(product)
        if not interior:
            raise PrintError("This book's print pages could not be made.")
        pages = count_pages(interior)
        if pages != product.printed_pages(pages):
            # The interior is padded by its builder; anything else is a bug.
            raise PrintError(
                "This book's print pages came out at a count the printer cannot bind."
            )
        size = self._provider.cover_size(product, pages)
        cover = build_cover(product, size)
        if not cover:
            raise PrintError("This book has no cover picture to print.")
        approved = self._store.create_approval(
            book_id=book_id,
            user_id=user_id,
            pdf_sha256=hashlib.sha256(pdf).hexdigest(),
            product_key=product.key,
            page_count=pages,
            interior=interior,
            cover=cover,
            printing_cents=product.estimated_printing_cents(pages),
            retail_cents=product.retail_cents,
            currency="USD",
            provider=self._provider.name,
        )
        logger.info(
            "print.approved user=%s book=%s order=%s product=%s pages=%d",
            user_id,
            book_id,
            approved.id,
            product.key,
            pages,
        )
        return approved

    def quote(self, order: PrintOrder, address: ShippingAddress) -> PrintOrder:
        """Ask the provider what this copy costs to this address, and keep it.

        Raises:
            PrintError: for an address the provider would refuse, or a quote
                it could not give.
        """
        self._require_enabled()
        if order.state != "approved":
            raise PrintError("This print order has already been placed.")
        problems = address.issues()
        if problems:
            raise PrintError(" ".join(problems))
        product = product_by_key(order.product_key)
        cost = self._provider.quote(product, order.page_count, address)
        quoted = self._store.record_quote(
            order.id,
            address=address.as_dict(),
            printing_cents=cost.printing_cents,
            shipping_cents=cost.shipping_cents,
        )
        if quoted is None:  # pragma: no cover - the row was just read
            raise PrintError("This print order could not be found.")
        logger.info(
            "print.quoted order=%s printing=%d shipping=%d",
            order.id,
            cost.printing_cents,
            cost.shipping_cents,
        )
        return quoted

    def place(
        self,
        *,
        order: PrintOrder,
        pdf: bytes,
        file_urls: FileUrls,
        title: str,
        about: Mapping[str, object] | object,
    ) -> PrintOrder:
        """Charge for the copy, then ask the provider for the job.

        An `unknown` order is retried here too: it is already charged, so the
        charge is a no-op, and the job is looked up before it is sent again.

        Raises:
            PrintError: for an unapproved/stale/unquoted order, an About page
                that may not be printed, or a provider refusal (refunded). A
                `PrintUncertain` means the order is now `unknown`.
            InsufficientFunds: when the wallet cannot cover the copy.
        """
        self._require_enabled()
        # `unknown`, or a `placing` claim left behind by a request that died:
        # either may have reached the provider, so it is looked up first.
        retrying = order.state in {"unknown", "placing"}
        if order.state not in {"approved", "unknown", "placing"}:
            raise PrintError("This print order has already been placed.")
        if not retrying and order.pdf_sha256 != hashlib.sha256(pdf).hexdigest():
            raise PrintError(
                "The book changed after you reviewed it. Open the print page again, "
                "read the new PDF and approve it before ordering."
            )
        if order.quoted_at is None or not order.address:
            raise PrintError("Please enter the delivery address to see the full price first.")
        address = ShippingAddress.from_dict(order.address)
        problems = address.issues()
        if problems:
            raise PrintError(" ".join(problems))
        issues = about_issues(about) or self._screened(about)
        if issues:
            raise PrintError(
                "This book cannot be printed as it is: " + first_issue_message(issues)
            )
        product = product_by_key(order.product_key)

        # One conditional UPDATE to `placing`. A second form submit for the
        # same order hits a row that is no longer claimable and stops here,
        # before it can charge again or order a second copy.
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
                    f"Printed copy of your book — {product.label}, printing and "
                    f"shipping ({order.page_count} pages)"
                ),
                amount_cents=order.total_cents,
            )
        except InsufficientFunds:
            # Nothing was sent, and the approval is still good: hand it back to
            # the owner so they can fund the wallet and press the button again.
            self._store.release(order.id)
            raise

        try:
            job = self._find_or_submit(
                order, product, file_urls, address, title, look_first=retrying
            )
        except PrintUncertain as exc:
            self._unknown(order, str(exc))
            raise PrintUncertain(UNKNOWN_MESSAGE) from exc
        except PrintError as exc:
            self._fail(order, str(exc))
            logger.warning("print.refused book=%s order=%s", order.book_id, order.id)
            raise
        except Exception as exc:
            # A bug or an unhandled transport error: the request may have been
            # sent, so this is not a refusal. Keep the charge, never resend
            # blindly, and tell the buyer where they stand.
            logger.exception(
                "print.unexpected_failure book=%s order=%s", order.book_id, order.id
            )
            self._unknown(order, "the print service failed unexpectedly")
            raise PrintUncertain(UNKNOWN_MESSAGE) from exc

        submitted = self._store.mark_submitted(
            order.id,
            provider=job.provider,
            provider_order_id=job.provider_order_id,
        )
        logger.info(
            "print.submitted book=%s order=%s provider=%s provider_order=%s",
            order.book_id,
            order.id,
            job.provider,
            job.provider_order_id,
        )
        return submitted or order

    def cancel_unknown(self, order: PrintOrder) -> PrintOrder:
        """The buyer's refund request for an `unknown` order (A4).

        The job is looked up first. If the printer has it, the order is simply
        `submitted` after all; only if it has none is the charge returned.

        Raises:
            PrintError: if the order is not `unknown`, or the lookup itself
                cannot be answered (nothing is refunded then).
        """
        self._require_enabled()
        if order.state != "unknown":
            raise PrintError("Only an order whose outcome is unknown can be cancelled here.")
        claimed = self._store.claim(order.id)
        if claimed is None:
            raise PrintError("This print order is already being checked. Refresh the page.")
        try:
            existing = self._provider.find(order.id)
        except Exception as exc:
            self._store.mark_unknown(order.id, provider=self._provider.name, error=str(exc))
            raise PrintError(
                "The print service could not be asked about this order just now, so "
                "nothing was refunded yet. Please try again in a little while."
            ) from exc
        if existing is not None:
            logger.info("print.cancel_found_existing order=%s", order.id)
            return (
                self._store.mark_submitted(
                    order.id,
                    provider=existing.provider,
                    provider_order_id=existing.provider_order_id,
                )
                or order
            )
        self._fail(order, "cancelled by the buyer; the print service had no such order")
        logger.info("print.cancel_refunded order=%s", order.id)
        return self._store.get(order.id) or order

    def _find_or_submit(
        self,
        order: PrintOrder,
        product: Product,
        file_urls: FileUrls,
        address: ShippingAddress,
        title: str,
        *,
        look_first: bool,
    ) -> PrintJob:
        if look_first:
            try:
                existing = self._provider.find(order.id)
            except PrintError as exc:
                # Still not known whether the job exists: do not send it.
                raise PrintUncertain(str(exc)) from exc
            if existing is not None:
                logger.info("print.found_existing order=%s", order.id)
                return existing
        interior_url, cover_url = file_urls(order)
        return self._provider.submit(
            order_id=order.id,
            product=product,
            pages=order.page_count,
            interior_url=interior_url,
            cover_url=cover_url,
            address=address,
            title=title,
        )

    def _unknown(self, order: PrintOrder, error: str) -> None:
        logger.error(
            "print.outcome_unknown book=%s order=%s error=%s", order.book_id, order.id, error
        )
        self._store.mark_unknown(order.id, provider=self._provider.name, error=error)

    def _fail(self, order: PrintOrder, error: str) -> None:
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
                amount_cents=order.total_cents,
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


def count_pages(pdf: bytes) -> int:
    """A PDF's page count.

    Raises:
        PrintError: if the file cannot be read or has no page.
    """
    try:
        pages = len(PdfReader(io.BytesIO(pdf)).pages)
    except Exception as exc:
        logger.warning("print.unreadable_pdf bytes=%d", len(pdf))
        raise PrintError("This book's print file could not be read.") from exc
    if pages < 1:
        raise PrintError("This book's print file has no pages.")
    return pages
